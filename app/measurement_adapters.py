"""Registered read-only adapters for replaying instrument input in an operation plan."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from io import BytesIO
from typing import Annotated, Callable

from PIL import Image, ImageStat
from pydantic import BaseModel, ConfigDict, Field

from app.instrument_drivers import parse_balance_frame, parse_barcode_frame


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


Text = Annotated[str, Field(min_length=1, max_length=160, pattern=r"\S")]
Coordinate = Annotated[int, Field(ge=0, le=4096)]
Extent = Annotated[int, Field(gt=0, le=4096)]


class Region(Input):
    x: Coordinate
    y: Coordinate
    width: Extent
    height: Extent

    def box(self):
        return self.x, self.y, self.x + self.width, self.y + self.height


class OpticalInput(Input):
    calibration_reference: Text
    png_base64: Annotated[str, Field(min_length=1, max_length=2_000_000)]
    sample: Region
    dark: Region
    reference: Region


class SerialFrame(Input):
    frame: Annotated[str, Field(min_length=1, max_length=256)]


def read_relative_signal(capture: OpticalInput, item_id: str) -> dict:
    try:
        data = base64.b64decode(capture.png_base64, validate=True)
    except ValueError as exc:
        raise ValueError("Supply a base64-encoded PNG frame.") from exc
    # PNG decoders can expand 2- or 4-bit grayscale into L. The input contract requires 8 bits.
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR" or data[24:26] != b"\x08\x00":
        raise ValueError("Supply an 8-bit grayscale PNG frame.")
    try:
        with Image.open(BytesIO(data), formats=["PNG"]) as frame:
            if (
                frame.mode != "L"
                or max(frame.size) > 4096
                or frame.width * frame.height > 4_194_304
                or frame.n_frames != 1
                or "transparency" in frame.info
            ):
                raise ValueError("Use one opaque 8-bit grayscale frame of at most 4 megapixels.")
            frame.verify()
        with Image.open(BytesIO(data), formats=["PNG"]) as frame:
            boxes = []
            levels = {}
            for name in ("sample", "dark", "reference"):
                region = getattr(capture, name)
                x, y, right, bottom = region.box()
                if right > frame.width or bottom > frame.height:
                    raise ValueError(f"The {name} region extends beyond the frame.")
                if any(
                    x < other_right and right > left and y < other_bottom and bottom > top
                    for left, top, other_right, other_bottom in boxes
                ):
                    raise ValueError("Use separate sample, dark and reference regions.")
                boxes.append(region.box())
                with frame.crop(region.box()) as patch:
                    levels[name] = ImageStat.Stat(patch).mean[0]
            size = list(frame.size)
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("Cannot read the supplied PNG frame.") from exc
    span = levels["reference"] - levels["dark"]
    if span <= 0:
        raise ValueError("The reference mean must exceed the dark mean.")
    value = 100 * (levels["sample"] - levels["dark"]) / span
    return {
        "value": value,
        "calibration_reference": capture.calibration_reference,
        "mean_dn": levels,
        "frame_sha256": hashlib.sha256(data).hexdigest(),
        "frame_size_px": size,
        "regions_px": {k: getattr(capture, k).model_dump() for k in levels},
        "outside_reference_span": not 0 <= value <= 100,
    }


def read_identifier(capture: SerialFrame, item_id: str) -> dict:
    result = parse_barcode_frame(capture.frame, expected=item_id)
    if not result.matched:
        raise ValueError("The recorded barcode does not match the item identifier.")
    return {
        "value": result.value,
        "raw_frame": capture.frame,
        "symbology_prefix": result.symbology_prefix,
    }


def read_weight(capture: SerialFrame, item_id: str) -> dict:
    result = parse_balance_frame(capture.frame)
    return {
        "value": result.mass_g,
        "raw_frame": result.raw_frame,
        "source_unit": result.unit,
        "stable": result.stable,
        "protocol": result.protocol,
    }


@dataclass(frozen=True)
class Command:
    unit: str
    inputs: type[Input]
    calibration: str
    read: Callable
    template: dict
    native_driver: str | None = None


@dataclass(frozen=True)
class Adapter:
    label: str
    version: int
    commands: dict[str, Command]
    integration_requirements: tuple[str, ...]
    station_module: str | None = None


ROI_TEMPLATE = {"x": None, "y": None, "width": None, "height": None}
ADAPTERS = {
    "grayscale-camera": Adapter(
        "Recorded grayscale camera",
        1,
        {
            "relative_signal": Command(
                "%",
                OpticalInput,
                "Identify dark and bright reference areas in the same exposure. "
                "The result is a percentage of their pixel-level span.",
                read_relative_signal,
                {
                    "calibration_reference": "",
                    "png_base64": "",
                    **{name: ROI_TEMPLATE.copy() for name in ("sample", "dark", "reference")},
                },
            )
        },
        (
            "Identify the camera model and its control interface.",
            "Implement and commission the requested command on the bound camera.",
        ),
        station_module="inspect",
    ),
    "serial-balance": Adapter(
        "Recorded serial balance",
        1,
        {
            "weigh": Command(
                "g",
                SerialFrame,
                "Tare the balance and verify its calibration and a stable reading.",
                read_weight,
                {"frame": ""},
                native_driver="SerialBalanceDriver.read_weight",
            )
        },
        ("Verify the balance protocol and commission the requested command.",),
        station_module="weigh",
    ),
    "barcode-scanner": Adapter(
        "Recorded barcode reader",
        1,
        {
            "read_barcode": Command(
                "identifier",
                SerialFrame,
                "Check scanner framing and compare the decoded identifier with the item.",
                read_identifier,
                {"frame": ""},
                native_driver="BarcodeScannerDriver.scan",
            )
        },
        ("Verify reader framing and implement the requested device command.",),
        station_module="read_barcode",
    ),
}


def catalog(*, include_schema: bool = True):
    return {
        key: {
            "label": adapter.label,
            "version": adapter.version,
            "execution": "recorded_input_only",
            "simulation": "recording_replay",
            "physical_execution": False,
            "integration_requirements": list(adapter.integration_requirements),
            "commands": {
                name: {
                    "unit": command.unit,
                    "inputs": list(command.inputs.model_fields),
                    **(
                        {"input_schema": command.inputs.model_json_schema()}
                        if include_schema
                        else {}
                    ),
                    "calibration": command.calibration,
                }
                for name, command in adapter.commands.items()
            },
        }
        for key, adapter in ADAPTERS.items()
    }
