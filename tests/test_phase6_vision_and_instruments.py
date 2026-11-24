"""Tests for Phase 6.3 camera fiducial calibration and bench instrument drivers."""

from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient

import app.api as api
from app.bench_station import StationProposal, compile_station
from app.bench_station_runner import run_record
from app.camera_calibration import (
    calibrate_planar_fiducials,
    generate_calibration_board_svg,
    list_local_cameras,
    measure_normalized_box_mm,
    render_synthetic_calibration_image,
)
from app.domain import new_project
from app.instrument_drivers import (
    BarcodeScannerDriver,
    InstrumentDriverError,
    LabelPrinterDriver,
    SerialBalanceDriver,
)


def _data_url_png(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def test_fiducial_calibration_flat_and_perspective_warped_achieve_submillimeter_rmse():
    svg = generate_calibration_board_svg(160.0, 110.0, 20.0)
    assert "<svg" in svg and "50 mm scale check" in svg

    flat_png, _ = render_synthetic_calibration_image(
        800, 600, board_width_mm=160.0, board_height_mm=110.0, marker_size_mm=20.0
    )
    cal_flat = calibrate_planar_fiducials(flat_png)
    assert cal_flat["calibrated"] is True
    assert cal_flat["rmse_mm"] < 0.2
    assert cal_flat["px_per_mm_mean"] > 1.0

    # Perspective-warped board simulates real off-axis bench camera placement.
    warped_png, true_centers = render_synthetic_calibration_image(
        800,
        600,
        board_width_mm=160.0,
        board_height_mm=110.0,
        marker_size_mm=20.0,
        perspective_shift_px=(26.0, 18.0),
    )
    cal_warp = calibrate_planar_fiducials(_data_url_png(warped_png))
    assert cal_warp["calibrated"] is True
    assert cal_warp["rmse_mm"] < 0.35
    for det, truth in zip(cal_warp["detected_corners_px"], true_centers):
        assert det[0] == pytest.approx(truth[0], abs=2.5)
        assert det[1] == pytest.approx(truth[1], abs=2.5)

    # Measure a bounding box spanning between two markers -> matches 124 mm physical span closely.
    x0_norm = true_centers[0][0] / 800.0 * 1000.0
    y0_norm = true_centers[0][1] / 600.0 * 1000.0
    x1_norm = true_centers[2][0] / 800.0 * 1000.0
    y1_norm = true_centers[2][1] / 600.0 * 1000.0
    measured = measure_normalized_box_mm(
        cal_warp, {"x_min": x0_norm, "y_min": y0_norm, "x_max": x1_norm, "y_max": y1_norm}
    )
    assert measured["width_mm"] == pytest.approx(124.0, abs=1.5)
    assert measured["height_mm"] == pytest.approx(74.0, abs=1.5)


def test_camera_routes_and_manual_calibration():
    project = new_project()
    api._sessions[project["id"]] = api.ProjectSession(project=project)
    client = TestClient(api.app)

    cams = client.get("/api/cameras")
    assert cams.status_code == 200
    assert "opencv_available" in cams.json()
    assert list_local_cameras()["numpy_fiducial_available"] is True

    board = client.get("/api/cameras/calibration-board.svg")
    assert board.status_code == 200 and "<svg" in board.text

    warped_png, _ = render_synthetic_calibration_image(
        640, 480, perspective_shift_px=(15.0, 12.0)
    )
    res = client.post(
        f"/api/projects/{project['id']}/inspections/calibrate",
        json={
            "project_revision": project["revision"],
            "image_data_url": _data_url_png(warped_png),
            "board_width_mm": 160.0,
            "board_height_mm": 110.0,
            "marker_size_mm": 20.0,
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["calibrated"] is True
    assert res.json()["rmse_mm"] < 0.35


def test_serial_balance_barcode_and_label_printer_drivers(tmp_path):
    # MT-SICS stable and mg-to-g conversion
    bal = SerialBalanceDriver(line_reader=lambda: "S S      12.345 g")
    r1 = bal.read_weight()
    assert r1.mass_g == pytest.approx(12.345) and r1.protocol == "mt-sics"

    bal_mg = SerialBalanceDriver(line_reader=lambda: "+  250.0 mg")
    r2 = bal_mg.read_weight()
    assert r2.mass_g == pytest.approx(0.25)

    for bad_frame in ("S D", "S +", "S -", "-1.2 g", "ERR01", ""):
        bad_drv = SerialBalanceDriver(line_reader=lambda f=bad_frame: f)
        with pytest.raises(InstrumentDriverError):
            bad_drv.read_weight()

    # Barcode scanner AIM symbology stripping
    scanner = BarcodeScannerDriver(line_reader=lambda: "]C1SAMPLE-2026-A\r\n")
    scan = scanner.scan(expected="SAMPLE-2026-A")
    assert scan.value == "SAMPLE-2026-A"
    assert scan.symbology_prefix == "]C1"
    assert scan.matched is True

    # Label printer ZPL, ESC/POS, injection prevention, exclusive write
    printer = LabelPrinterDriver()
    zpl = printer.render_zpl("SAMPLE-2026-A")
    assert zpl.startswith("^XA") and "^FD^FS" not in zpl and "^FDSAMPLE-2026-A^FS" in zpl
    with pytest.raises(ValueError, match="ZPL"):
        printer.render_zpl("BAD^XZINJECTION")

    escpos = printer.render_escpos("SAMPLE-2026-A")
    assert escpos.startswith(b"\x1b@") and b"SAMPLE-2026-A" in escpos

    zpl_out = tmp_path / "label.zpl"
    spooled = printer.spool("SAMPLE-2026-A", fmt="zpl", output_path=zpl_out)
    assert spooled["contains_barcode"] is True and zpl_out.exists()
    with pytest.raises(FileExistsError):
        printer.spool("SAMPLE-2026-A", fmt="zpl", output_path=zpl_out)


def test_bench_station_runner_works_with_injected_drivers(tmp_path):
    plan = {
        "project_id": "bench-p6",
        "revision": 4,
        "station": compile_station(
            StationProposal(operations=["weigh", "print_label", "read_barcode"])
        ),
    }
    answers = iter(["operator-1", "VIAL-007", "yes", "yes", "yes"])
    zpl_file = tmp_path / "vial007.zpl"
    bal_stream = io.BytesIO(b"S S       4.125 g\r\n")
    scan_stream = io.BytesIO(b"]C1VIAL-007\r\n")

    record = run_record(
        plan,
        ask=lambda _: next(answers),
        label_output=zpl_file,
        balance_driver=SerialBalanceDriver(stream=bal_stream),
        barcode_driver=BarcodeScannerDriver(stream=scan_stream),
        label_format="zpl",
    )
    assert record["mode"] == "instrument_assisted"
    assert record["status"] == "operator_sequence_complete"
    assert record["events"][0]["mass"] == pytest.approx(4.125)
    assert record["events"][0]["provenance"] == "instrument_serial"
    assert record["events"][1]["provenance"] == "instrument_printer"
    assert record["events"][1]["label_artifact"]["format"] == "zpl"
    assert record["events"][2]["provenance"] == "instrument_barcode"
    assert zpl_file.read_text(encoding="ascii").startswith("^XA")
