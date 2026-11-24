"""Optional bench instrument drivers (serial balances, barcode scanners, ZPL/ESC-POS printers)."""

from __future__ import annotations

import hashlib
import math
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal


class InstrumentDriverError(RuntimeError):
    """Raised on serial/socket/protocol failures or unstable/invalid instrument frames."""


_UNIT_TO_GRAMS: dict[str, float] = {
    "g": 1.0,
    "mg": 0.001,
    "ug": 1e-6,
    "kg": 1000.0,
}

# MT-SICS: "S S      12.345 g" | stable weight; "S D ..." dynamic/unstable; "S +" / "S -" overload.
_MT_SICS_STABLE = re.compile(r"^S\s+S\s+([+-]?\d+(?:\.\d+)?)\s*(kg|mg|ug|g)\b", re.I)
_MT_SICS_ERROR = re.compile(r"^S\s+([D+\-I])\b", re.I)
# OHAUS / Sartorius generic: "+   12.345 g" or "12.345 g N" / "0.025 g".
_GENERIC_WEIGHT = re.compile(
    r"^(?:WT\s+|N\s+|G\s+)?([+-]?\s*\d+(?:\.\d+)?)\s*(kg|mg|ug|g)(?:\s+[A-Z])?$",
    re.I,
)


@dataclass
class BalanceReading:
    """Normalized balance reading converted to grams with provenance metadata."""

    mass_g: float
    raw_value: float
    unit: str
    stable: bool
    protocol: str
    raw_frame: str
    provenance: str = "instrument_serial"


def parse_balance_frame(frame: str) -> BalanceReading:
    """Parse one serial frame from MT-SICS (Mettler) or OHAUS/Sartorius balances."""
    text = str(frame).strip()
    if not text:
        raise InstrumentDriverError("Empty frame received from serial balance.")

    mt_err = _MT_SICS_ERROR.match(text)
    if mt_err:
        code = mt_err.group(1).upper()
        reasons = {
            "D": "balance reading unstable (S D)",
            "+": "balance overload (S +)",
            "-": "balance underload (S -)",
            "I": "balance command rejected (S I)",
        }
        raise InstrumentDriverError(f"Balance reported {reasons.get(code, code)}.")

    lowered = text.lower()
    if any(tok in lowered for tok in ("overload", "underload", "err", "?", " ol", " us")):
        raise InstrumentDriverError(f"Unstable or error frame from balance: {text!r}.")

    m_sics = _MT_SICS_STABLE.match(text)
    if m_sics:
        raw_val = float(m_sics.group(1))
        unit_key = m_sics.group(2).lower()
        protocol = "mt-sics"
    else:
        m_gen = _GENERIC_WEIGHT.match(text)
        if not m_gen:
            raise InstrumentDriverError(f"Unrecognized serial balance frame: {text!r}.")
        raw_val = float(m_gen.group(1).replace(" ", ""))
        unit_key = m_gen.group(2).lower()
        protocol = "ohaus-sartorius"

    if not math.isfinite(raw_val) or raw_val < 0:
        raise InstrumentDriverError(f"Balance mass must be finite and non-negative, got {raw_val}.")
    mass_g = round(raw_val * _UNIT_TO_GRAMS[unit_key], 6)
    return BalanceReading(
        mass_g=mass_g,
        raw_value=raw_val,
        unit=unit_key,
        stable=True,
        protocol=protocol,
        raw_frame=text,
    )


class SerialBalanceDriver:
    """Serial laboratory balance driver (pyserial optional or injectable line reader/stream)."""

    def __init__(
        self,
        port: str | None = None,
        *,
        baudrate: int = 9600,
        timeout_s: float = 2.0,
        stream: Any = None,
        line_reader: Callable[[], str] | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self._stream = stream
        self._line_reader = line_reader

    def _read_line(self) -> str:
        if self._line_reader is not None:
            return self._line_reader()
        if self._stream is not None:
            if hasattr(self._stream, "write"):
                self._stream.write(b"S\r\n")
            raw = self._stream.readline()
            return raw.decode("ascii", errors="replace") if isinstance(raw, bytes) else str(raw)
        if not self.port:
            raise InstrumentDriverError("No serial port/stream configured for SerialBalanceDriver.")
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError as exc:
            raise InstrumentDriverError(
                "pyserial is not installed; pass stream/line_reader or install pyserial."
            ) from exc
        with serial.Serial(self.port, self.baudrate, timeout=self.timeout_s) as ser:
            ser.write(b"S\r\n")
            ser.flush()
            raw = ser.readline()
        return raw.decode("ascii", errors="replace")

    def read_weight(self) -> BalanceReading:
        return parse_balance_frame(self._read_line())


@dataclass
class BarcodeReading:
    """Validated barcode scan result."""

    value: str
    symbology_prefix: str | None
    raw_frame: str
    matched: bool
    provenance: str = "instrument_barcode"


_AIM_PREFIX = re.compile(r"^(\][A-Za-z0-9]{2})(.*)$")


def parse_barcode_frame(frame: str, *, expected: str | None = None) -> BarcodeReading:
    """Strip CR/LF and optional AIM symbology identifier and validate safe ASCII."""
    raw = str(frame).rstrip("\r\n")
    symbology = None
    value = raw
    m = _AIM_PREFIX.match(raw)
    if m:
        symbology, value = m.group(1), m.group(2)
    value = value.strip()
    if not value or len(value) > 80 or not all(32 <= ord(ch) < 127 for ch in value):
        raise InstrumentDriverError(f"Invalid barcode payload {raw!r}.")
    matched = True if expected is None else (value == expected)
    return BarcodeReading(
        value=value,
        symbology_prefix=symbology,
        raw_frame=raw,
        matched=matched,
    )


class BarcodeScannerDriver:
    """Serial or HID-wedge barcode scanner driver."""

    def __init__(
        self,
        port: str | None = None,
        *,
        baudrate: int = 9600,
        timeout_s: float = 5.0,
        stream: Any = None,
        line_reader: Callable[[], str] | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self._stream = stream
        self._line_reader = line_reader

    def _read_line(self) -> str:
        if self._line_reader is not None:
            return self._line_reader()
        if self._stream is not None:
            raw = self._stream.readline()
            return raw.decode("ascii", errors="replace") if isinstance(raw, bytes) else str(raw)
        if not self.port:
            raise InstrumentDriverError(
                "No serial port or stream configured for BarcodeScannerDriver."
            )
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError as exc:
            raise InstrumentDriverError(
                "pyserial is not installed; use keyboard-wedge input or install pyserial."
            ) from exc
        with serial.Serial(self.port, self.baudrate, timeout=self.timeout_s) as ser:
            raw = ser.readline()
        return raw.decode("ascii", errors="replace")

    def scan(self, *, expected: str | None = None) -> BarcodeReading:
        return parse_barcode_frame(self._read_line(), expected=expected)


def _validate_label_text(text: str, *, field_name: str, allow_caret_tilde: bool = False) -> str:
    if not isinstance(text, str) or not text or len(text) > 80:
        raise ValueError(f"{field_name} must be 1 to 80 ASCII characters.")
    if any(ord(ch) < 32 or ord(ch) >= 127 for ch in text):
        raise ValueError(f"{field_name} contains unsupported control or non-ASCII characters.")
    if not allow_caret_tilde and ("^" in text or "~" in text):
        raise ValueError(f"{field_name} must not contain ZPL command characters ('^' or '~').")
    return text


class LabelPrinterDriver:
    """Render and spool laboratory labels in ZPL II, ESC/POS, or standalone HTML."""

    @staticmethod
    def render_zpl(
        identifier: str,
        *,
        label_width_mm: float = 60.0,
        label_height_mm: float = 30.0,
        dpi: int = 203,
        subtitle: str = "AutoLab Sample",
    ) -> str:
        clean_id = _validate_label_text(identifier, field_name="identifier")
        clean_sub = _validate_label_text(subtitle, field_name="subtitle")
        dots_per_mm = dpi / 25.4
        pw = max(120, int(round(label_width_mm * dots_per_mm)))
        ll = max(80, int(round(label_height_mm * dots_per_mm)))
        return (
            "^XA\n"
            f"^PW{pw}\n"
            f"^LL{ll}\n"
            "^CI28\n"
            f"^FO24,20^A0N,26,26^FD{clean_sub}^FS\n"
            f"^FO24,56^BY2,3,70^BCN,70,Y,N,N^FD{clean_id}^FS\n"
            "^XZ\n"
        )

    @staticmethod
    def render_escpos(identifier: str, *, subtitle: str = "AutoLab Sample") -> bytes:
        clean_id = _validate_label_text(identifier, field_name="identifier")
        clean_sub = _validate_label_text(subtitle, field_name="subtitle")
        payload = bytearray()
        payload.extend(b"\x1b@")  # ESC @ Initialize
        payload.extend(b"\x1ba\x01")  # Center align
        payload.extend((clean_sub + "\n").encode("ascii"))
        payload.extend(b"\x1dh\x40\x1dw\x02\x1dH\x02")  # Height 64, width 2, HRI below
        id_bytes = clean_id.encode("ascii")
        # GS k 73 n {B <data> (Code 128 Subset B)
        code128 = b"{B" + id_bytes
        payload.extend(bytes([0x1D, 0x6B, 73, len(code128)]) + code128)
        payload.extend(b"\n\n\x1dV\x01")  # Feed + partial cut
        return bytes(payload)

    def spool(
        self,
        identifier: str,
        *,
        fmt: Literal["html", "zpl", "escpos"] = "zpl",
        output_path: str | Path | None = None,
        tcp_host: str | None = None,
        tcp_port: int = 9100,
        stream: Any = None,
    ) -> dict[str, Any]:
        if fmt == "zpl":
            raw = self.render_zpl(identifier).encode("ascii")
        elif fmt == "escpos":
            raw = self.render_escpos(identifier)
        elif fmt == "html":
            from app.bench_station_runner import label_html

            raw = label_html(identifier).encode("utf-8")
        else:
            raise ValueError(f"Unsupported label format {fmt!r}.")

        digest = hashlib.sha256(raw).hexdigest()
        target = "memory"

        if output_path is not None:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(raw)
            target = str(path.resolve())
        if stream is not None:
            stream.write(raw)
            if hasattr(stream, "flush"):
                stream.flush()
            target = "stream"
        if tcp_host is not None:
            if not 1 <= int(tcp_port) <= 65535:
                raise ValueError("tcp_port must be in 1..65535.")
            with socket.create_connection((tcp_host, int(tcp_port)), timeout=5.0) as sock:
                sock.sendall(raw)
            target = f"tcp://{tcp_host}:{int(tcp_port)}"

        return {
            "format": fmt,
            "sha256": digest,
            "bytes_written": len(raw),
            "target": target,
            "contains_barcode": fmt != "html",
            "provenance": "instrument_printer" if fmt != "html" else "operator_entered",
        }
