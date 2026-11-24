"""Launch AutoLab's local HTTP hardware gateway matching concept_controller.py."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from app.hardware_gateway import (  # noqa: E402
    AxisLimits,
    GatewayConfig,
    create_hardware_gateway_app,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="Bind port (default 8787)")
    parser.add_argument("--device-id", default="autolab-stage-1", help="Device identifier")
    parser.add_argument("--project-id", required=True, help="Bound AutoLab project ID")
    parser.add_argument("--revision", type=int, required=True, help="Bound project revision")
    parser.add_argument(
        "--driver",
        choices=("loopback", "grbl", "klipper"),
        default="loopback",
        help="Motion driver backend",
    )
    parser.add_argument("--serial-port", default=None, help="COM/tty port for GRBL")
    parser.add_argument("--klipper-url", default=None, help="Moonraker HTTP URL for Klipper")
    parser.add_argument("--watchdog-s", type=float, default=30.0, help="Command watchdog seconds")
    parser.add_argument("--commissioning-reviewed", action="store_true", help="Mark reviewed")
    parser.add_argument("--write-config", type=Path, default=None, help="Save gateway JSON config")
    args = parser.parse_args()

    config = GatewayConfig(
        device_id=args.device_id,
        project_id=args.project_id,
        revision=args.revision,
        driver_kind=args.driver,
        commissioning_reviewed=args.commissioning_reviewed,
        watchdog_timeout_s=args.watchdog_s,
        limits_mm=AxisLimits(),
        serial_port=args.serial_port,
        klipper_url=args.klipper_url,
    )
    if args.write_config:
        client_cfg = {
            "schema_version": 1,
            "base_url": f"http://{args.host}:{args.port}",
            "device_id": config.device_id,
            "project_id": config.project_id,
            "revision": config.revision,
            "commissioning_reviewed": config.commissioning_reviewed,
            "timeout_seconds": 5,
            "actions": {},
        }
        args.write_config.write_text(json.dumps(client_cfg, indent=2), encoding="utf-8")

    app = create_hardware_gateway_app(config)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
