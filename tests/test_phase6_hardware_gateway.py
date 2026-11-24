"""Tests for Phase 6.2 real HTTP hardware gateway, limits, watchdog, E-stop, and drivers."""

from __future__ import annotations

import io
import threading
import time
from socket import socket

import httpx
import uvicorn
from fastapi.testclient import TestClient

from app import api
from app.concept_controller import HttpGatewayAdapter, run_sequence
from app.domain import new_project
from app.hardware_gateway import (
    AxisLimits,
    GatewayConfig,
    GrblSerialDriver,
    KlipperMoonrakerDriver,
    LoopbackStageDriver,
    build_gateway_commissioning_checklist,
    create_hardware_gateway_app,
)


def _sample_project(project_id: str = "gw-test", revision: int = 2) -> dict:
    return {
        "project_id": project_id,
        "revision": revision,
        "design": {
            "components": [{"key": "stage"}],
            "steps": [
                {"name": "move-to-source", "instruction": "Move to source"},
                {"name": "move-to-dest", "instruction": "Move to destination"},
            ],
        },
    }


def _sample_config(*, reviewed: bool = True, watchdog_s: float = 15.0) -> GatewayConfig:
    return GatewayConfig(
        device_id="stage-1",
        project_id="gw-test",
        revision=2,
        driver_kind="loopback",
        commissioning_reviewed=reviewed,
        watchdog_timeout_s=watchdog_s,
        limits_mm=AxisLimits(x_min=0, x_max=200, y_min=0, y_max=200, z_min=0, z_max=100),
    )


def test_gateway_enforces_commissioning_homing_soft_limits_idempotency_and_estop():
    unreviewed = create_hardware_gateway_app(_sample_config(reviewed=False))
    c_unrev = TestClient(unreviewed)
    res_unrev = c_unrev.post(
        "/prepare",
        json={
            "schema_version": 1,
            "request_id": "req-1",
            "device_id": "stage-1",
            "project_id": "gw-test",
            "revision": 2,
        },
    )
    assert res_unrev.status_code == 403

    driver = LoopbackStageDriver()
    app_gw = create_hardware_gateway_app(_sample_config(reviewed=True), driver=driver)
    client = TestClient(app_gw)

    # Execute before prepare/homing is blocked cleanly with 409.
    not_ready = client.post(
        "/execute",
        json={
            "schema_version": 1,
            "request_id": "req-early",
            "device_id": "stage-1",
            "project_id": "gw-test",
            "revision": 2,
            "step_name": "s1",
            "action": "move_to",
            "parameters": {"target_mm": [10, 10, 5]},
        },
    )
    assert not_ready.status_code == 409
    assert driver.emitted_gcode == []

    # Prepare succeeds and homes stage.
    prep = client.post(
        "/prepare",
        json={
            "schema_version": 1,
            "request_id": "prep-1",
            "device_id": "stage-1",
            "project_id": "gw-test",
            "revision": 2,
        },
    )
    assert prep.status_code == 200
    assert prep.json()["status"] == "ready"
    assert prep.json()["homed"] is True
    assert "G28" in driver.emitted_gcode

    # Valid move executes and records position + idempotency by request_id.
    move_body = {
        "schema_version": 1,
        "request_id": "exec-1",
        "device_id": "stage-1",
        "project_id": "gw-test",
        "revision": 2,
        "step_name": "move-1",
        "action": "move_to",
        "parameters": {"target_mm": [50.0, 40.0, 20.0], "feedrate_mm_min": 1200.0},
    }
    r1 = client.post("/execute", json=move_body)
    assert r1.status_code == 200
    gcode_count_after_first = len(driver.emitted_gcode)
    r2 = client.post("/execute", json=move_body)
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert len(driver.emitted_gcode) == gcode_count_after_first

    # Software travel limit Violation MUST reject BEFORE emitting G-code!
    oob = client.post(
        "/execute",
        json={
            "schema_version": 1,
            "request_id": "exec-oob",
            "device_id": "stage-1",
            "project_id": "gw-test",
            "revision": 2,
            "step_name": "move-oob",
            "action": "move_to",
            "parameters": {"target_mm": [260.0, 40.0, 20.0]},
        },
    )
    assert oob.status_code == 422
    assert len(driver.emitted_gcode) == gcode_count_after_first

    # E-stop latches until explicit /reset-estop.
    est = client.post(
        "/estop",
        json={
            "schema_version": 1,
            "request_id": "estop-1",
            "device_id": "stage-1",
            "project_id": "gw-test",
            "revision": 2,
        },
    )
    assert est.status_code == 200 and est.json()["status"] == "estop"
    assert driver.estop_active is True
    assert client.post(
        "/prepare",
        json={
            "schema_version": 1,
            "request_id": "prep-blocked",
            "device_id": "stage-1",
            "project_id": "gw-test",
            "revision": 2,
        },
    ).status_code == 409

    reset = client.post(
        "/reset-estop",
        json={"operator_confirmed": True, "clear_reason": "Area verified clear"},
    )
    assert reset.status_code == 200 and reset.json()["estop_engaged"] is False


def test_gateway_watchdog_trips_emergency_stop_automatically():
    driver = LoopbackStageDriver()
    gw = create_hardware_gateway_app(_sample_config(reviewed=True, watchdog_s=0.15), driver=driver)
    client = TestClient(gw)
    client.post(
        "/prepare",
        json={
            "schema_version": 1,
            "request_id": "wd-prep",
            "device_id": "stage-1",
            "project_id": "gw-test",
            "revision": 2,
        },
    )
    time.sleep(0.35)
    status = client.get("/status").json()
    assert status["state"] == "estop"
    assert status["watchdog_tripped"] is True
    assert status["estop_engaged"] is True
    assert driver.estop_active is True


def test_grbl_and_klipper_drivers_with_injected_transports():
    class _FakeSerial:
        def __init__(self) -> None:
            self.written: list[bytes] = []
            self._reads = io.BytesIO(b"ok\nok\nok\nok\nok\nok\n")

        def write(self, data: bytes) -> int:
            self.written.append(data)
            return len(data)

        def readline(self) -> bytes:
            return self._reads.readline() or b"ok\n"

    stream = _FakeSerial()
    grbl = GrblSerialDriver(stream=stream)
    grbl.home()
    grbl.move_to(25.0, 30.0, 10.0, 1000.0)
    grbl.emergency_stop()
    sent = b"".join(stream.written)
    assert b"$H\n" in sent
    assert b"G1 X25.000 Y30.000 Z10.000 F1000.0\n" in sent
    assert b"!\x18" in sent

    klipper_calls: list[tuple[str, dict]] = []

    def _klipper_handler(request: httpx.Request) -> httpx.Response:
        import json

        klipper_calls.append((request.url.path, json.loads(request.content.decode())))
        return httpx.Response(200, json={"result": "ok"})

    klipper = KlipperMoonrakerDriver(
        "http://127.0.0.1:7125", transport=httpx.MockTransport(_klipper_handler)
    )
    klipper.home()
    klipper.move_to(10.0, 20.0, 5.0, 900.0)
    klipper.emergency_stop()
    assert klipper_calls[0][0] == "/printer/gcode/script"
    assert "G28" in klipper_calls[0][1]["script"]
    assert "G1 X10.000 Y20.000 Z5.000" in klipper_calls[1][1]["script"]
    assert klipper_calls[2][0] == "/printer/emergency_stop"


def test_real_http_loopback_with_concept_controller_adapter():
    with socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    driver = LoopbackStageDriver()
    cfg_obj = _sample_config(reviewed=True, watchdog_s=10.0)
    gw_app = create_hardware_gateway_app(cfg_obj, driver=driver)
    server = uvicorn.Server(uvicorn.Config(gw_app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5.0
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started

        cfg = {
            "schema_version": 1,
            "base_url": f"http://127.0.0.1:{port}",
            "device_id": "stage-1",
            "project_id": "gw-test",
            "revision": 2,
            "commissioning_reviewed": True,
            "timeout_seconds": 5,
            "actions": {
                "move-to-source": {
                    "action": "move_to",
                    "parameters": {"target_mm": [20.0, 15.0, 5.0], "feedrate_mm_min": 1200.0},
                },
                "move-to-dest": {
                    "action": "move_to",
                    "parameters": {"target_mm": [80.0, 60.0, 5.0], "feedrate_mm_min": 1200.0},
                },
            },
        }
        adapter = HttpGatewayAdapter(cfg, armed=True)
        project = _sample_project()
        out = run_sequence(project, adapter=adapter, commissioned=True)
        assert out["mode"] == "external_device_adapter"
        assert len(out["events"]) == 2
        assert all(ev["status"] == "completed" for ev in out["events"])
        assert driver.position_mm == [80.0, 60.0, 5.0]
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)


def test_gateway_checklist_and_ssrf_guarded_probe_route():
    project = new_project()
    api._sessions[project["id"]] = api.ProjectSession(project=project)
    client = TestClient(api.app)

    chk = client.get(
        f"/api/projects/{project['id']}/software/gateway/checklist",
        params={"revision": project["revision"], "driver_kind": "grbl"},
    )
    assert chk.status_code == 200
    body = chk.json()
    assert body["commissioning_reviewed"] is False
    assert body["config_template"]["commissioning_reviewed"] is False
    assert len(body["checklist"]) >= 5

    built = build_gateway_commissioning_checklist(project)
    assert "Physical NC emergency-stop" in built["markdown"] or "NC" in built["markdown"]

    blocked = client.post(
        f"/api/projects/{project['id']}/software/gateway/probe",
        json={"base_url": "https://example.com:8787"},
    )
    assert blocked.status_code == 422
