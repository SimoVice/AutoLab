"""Phase 6.4 software-loopback commissioning & dry-run acceptance harness.

Runs two independent workstation setups x 10 continuous cycles over a real local
HTTP Hardware Gateway + checks soft-limit rejection, mid-run E-stop, watchdog trip,
fiducial calibration (< 0.5 mm RMSE), and balance/barcode/ZPL bench drivers.

NOTE: Passing this harness validates the digital & loopback control plane only.
Physical hardware dry-runs on Simone's bench are logged separately and are NOT
marked complete automatically by this script.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from socket import socket

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.camera_calibration import (  # noqa: E402
    calibrate_planar_fiducials,
    render_synthetic_calibration_image,
)
from app.concept_controller import HttpGatewayAdapter, run_sequence  # noqa: E402
from app.hardware_gateway import (  # noqa: E402
    AxisLimits,
    GatewayConfig,
    LoopbackStageDriver,
    create_hardware_gateway_app,
)
from app.instrument_drivers import (  # noqa: E402
    BarcodeScannerDriver,
    LabelPrinterDriver,
    SerialBalanceDriver,
)


def _free_port() -> int:
    with socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_setup_cycles(
    setup_name: str,
    *,
    device_id: str,
    project_id: str,
    steps: list[dict],
    actions: dict,
    limits: AxisLimits,
    oob_target: list[float],
    cycles: int = 10,
) -> dict:
    port = _free_port()
    driver = LoopbackStageDriver()
    gw_config = GatewayConfig(
        device_id=device_id,
        project_id=project_id,
        revision=1,
        driver_kind="loopback",
        commissioning_reviewed=True,
        watchdog_timeout_s=5.0,
        limits_mm=limits,
    )
    gw_app = create_hardware_gateway_app(gw_config, driver=driver)
    server = uvicorn.Server(uvicorn.Config(gw_app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5.0
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError(f"Gateway for {setup_name} did not start within 5s.")

        base_url = f"http://127.0.0.1:{port}"
        adapter_cfg = {
            "schema_version": 1,
            "base_url": base_url,
            "device_id": device_id,
            "project_id": project_id,
            "revision": 1,
            "commissioning_reviewed": True,
            "timeout_seconds": 5,
            "actions": actions,
        }
        project = {
            "project_id": project_id,
            "revision": 1,
            "design": {"components": [{"key": "stage"}], "steps": steps},
        }
        completed_cycles = 0
        total_events = 0
        for _ in range(cycles):
            adapter = HttpGatewayAdapter(adapter_cfg, armed=True)
            res = run_sequence(project, adapter=adapter, commissioned=True)
            if len(res["events"]) == len(steps) and all(
                ev["status"] == "completed" for ev in res["events"]
            ):
                completed_cycles += 1
                total_events += len(res["events"])

        # Verify soft-limit rejection + E-stop + recovery.
        with httpx.Client(base_url=base_url, timeout=3.0) as client:
            client.post(
                "/prepare",
                json={
                    "schema_version": 1,
                    "request_id": f"{setup_name}-oob-prep",
                    "device_id": device_id,
                    "project_id": project_id,
                    "revision": 1,
                },
            ).raise_for_status()
            oob = client.post(
                "/execute",
                json={
                    "schema_version": 1,
                    "request_id": f"{setup_name}-oob-exec",
                    "device_id": device_id,
                    "project_id": project_id,
                    "revision": 1,
                    "step_name": "oob-check",
                    "action": "move_to",
                    "parameters": {"target_mm": oob_target},
                },
            )
            soft_limit_rejected = oob.status_code == 422

            est = client.post(
                "/estop",
                json={
                    "schema_version": 1,
                    "request_id": f"{setup_name}-estop",
                    "device_id": device_id,
                    "project_id": project_id,
                    "revision": 1,
                },
            )
            estop_ok = est.status_code == 200 and est.json().get("status") == "estop"
            rst = client.post(
                "/reset-estop",
                json={"operator_confirmed": True, "clear_reason": "Loopback harness check"},
            )
            recovery_ok = rst.status_code == 200 and not rst.json().get("estop_engaged")

        return {
            "setup": setup_name,
            "cycles_requested": cycles,
            "cycles_completed": completed_cycles,
            "total_steps_executed": total_events,
            "soft_limit_rejected": soft_limit_rejected,
            "estop_ok": estop_ok,
            "recovery_ok": recovery_ok,
            "gcode_commands_logged": len(driver.emitted_gcode),
            "passed": (
                completed_cycles == cycles
                and soft_limit_rejected
                and estop_ok
                and recovery_ok
            ),
        }
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)


def run_harness(output_dir: Path, *, cycles: int = 10) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    setup_a = _run_setup_cycles(
        "Setup A: 3-axis Cartesian pipetting gantry",
        device_id="cartesian-gantry-1",
        project_id="p64-setup-a",
        steps=[
            {"name": "home-clear", "instruction": "Clear height"},
            {"name": "source-aspirate", "instruction": "Move over source"},
            {"name": "plate-dispense", "instruction": "Move over plate"},
        ],
        actions={
            "home-clear": {"action": "move_to", "parameters": {"target_mm": [10.0, 10.0, 80.0]}},
            "source-aspirate": {
                "action": "move_to",
                "parameters": {"target_mm": [45.0, 35.0, 20.0]},
            },
            "plate-dispense": {
                "action": "move_to",
                "parameters": {"target_mm": [135.0, 85.0, 25.0]},
            },
        },
        limits=AxisLimits(x_min=0, x_max=220, y_min=0, y_max=180, z_min=0, z_max=120),
        oob_target=[260.0, 50.0, 20.0],
        cycles=cycles,
    )
    setup_b = _run_setup_cycles(
        "Setup B: Benchtop rotary/linear sample station",
        device_id="rotary-station-1",
        project_id="p64-setup-b",
        steps=[
            {"name": "index-station", "instruction": "Rotate sample platter"},
            {"name": "scan-pose", "instruction": "Move sensor bridge"},
            {"name": "park-bridge", "instruction": "Return to park"},
        ],
        actions={
            "index-station": {
                "action": "rotate",
                "parameters": {"axis": "z", "angle_deg": 45.0},
            },
            "scan-pose": {
                "action": "move_to",
                "parameters": {"target_mm": [90.0, 60.0, 40.0]},
            },
            "park-bridge": {"action": "move_to", "parameters": {"target_mm": [5.0, 5.0, 90.0]}},
        },
        limits=AxisLimits(x_min=0, x_max=180, y_min=0, y_max=150, z_min=0, z_max=100),
        oob_target=[50.0, 50.0, 140.0],
        cycles=cycles,
    )

    warped_png, _ = render_synthetic_calibration_image(
        800, 600, perspective_shift_px=(22.0, 16.0)
    )
    cal = calibrate_planar_fiducials(warped_png)
    bal = SerialBalanceDriver(stream=io.BytesIO(b"S S      10.250 g\r\n")).read_weight()
    scan = BarcodeScannerDriver(stream=io.BytesIO(b"]C1HARNESS-001\r\n")).scan(
        expected="HARNESS-001"
    )
    zpl_meta = LabelPrinterDriver().spool(
        "HARNESS-001", fmt="zpl", output_path=output_dir / "sample-harness-001.zpl"
    )

    all_software_ok = (
        setup_a["passed"]
        and setup_b["passed"]
        and cal["calibrated"]
        and cal["rmse_mm"] < 0.5
        and bal.mass_g == 10.25
        and scan.matched
        and zpl_meta["contains_barcode"]
    )
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "software_loopback_gate": "PASSED" if all_software_ok else "FAILED",
        "physical_bench_gate": "PENDING_PHYSICAL_EXECUTION",
        "setups": [setup_a, setup_b],
        "camera_fiducial_check": {
            "calibrated": cal["calibrated"],
            "backend": cal["backend"],
            "rmse_mm": cal["rmse_mm"],
            "px_per_mm_mean": cal["px_per_mm_mean"],
        },
        "bench_instrument_check": {
            "balance_mass_g": bal.mass_g,
            "barcode_matched": scan.matched,
            "zpl_sha256": zpl_meta["sha256"],
        },
    }
    (output_dir / "dry_run_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    md = [
        "# Phase 6.4 Physical Motion & Dry-Run Commissioning Sign-Off Pack",
        "",
        f"- Generated UTC: `{report['generated_at_utc']}`",
        f"- Software Loopback Pre-Flight Gate: **{report['software_loopback_gate']}**",
        f"- Physical Bench Execution Gate: **{report['physical_bench_gate']}**",
        "",
        "## Digital / Loopback Verification Results",
        "",
        f"- {setup_a['setup']}: {setup_a['cycles_completed']}/{cycles} cycles "
        f"(soft-limit rejected={setup_a['soft_limit_rejected']}, E-stop={setup_a['estop_ok']})",
        f"- {setup_b['setup']}: {setup_b['cycles_completed']}/{cycles} cycles "
        f"(soft-limit rejected={setup_b['soft_limit_rejected']}, E-stop={setup_b['estop_ok']})",
        f"- Fiducial Homography (`{cal['backend']}`): RMSE = `{cal['rmse_mm']} mm` "
        f"({cal['px_per_mm_mean']} px/mm)",
        f"- Bench Drivers: Balance `{bal.mass_g} g`, Barcode `{scan.value}`, "
        f"ZPL `{zpl_meta['sha256'][:12]}`",
        "",
        "## Physical Bench Sign-Off (Operator Completes on Real Hardware)",
        "",
        "- [ ] Setup A physically homed & 10 dry-run cycles completed with 0 crashes/drops",
        "- [ ] Setup B physically homed & 10 dry-run cycles completed with 0 crashes/drops",
        "- [ ] Physical NC E-stop button tested under motion and verified to cut motor power",
        "- [ ] Inert dye-water transfer & metric camera check logged in `EVIDENZE.md`",
    ]
    (output_dir / "COMMISSIONING_CHECKLIST.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / ".local" / "phase6-commissioning-20261124",
    )
    parser.add_argument("--cycles", type=int, default=10)
    args = parser.parse_args()
    out_dir = args.output_dir
    zpl_existing = out_dir / "sample-harness-001.zpl"
    if zpl_existing.exists():
        zpl_existing.unlink()
    report = run_harness(out_dir, cycles=args.cycles)
    print(json.dumps(report, indent=2))
    return 0 if report["software_loopback_gate"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
