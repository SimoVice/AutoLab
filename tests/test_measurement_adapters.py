"""Run the same recorded optical measurement on two independent physical project models."""

import asyncio
import base64
import json
import subprocess
import sys
from copy import deepcopy
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import api
from app.agent_tools import ToolContext, execute_tool
from app.bench_station import StationProposal, compile_station
from app.bench_station_runner import run_record
from app.concept_design import compile_concept
from app.design_workflow import approve_machine
from app.domain import apply_update, new_project
from app.measurement_adapters import (
    ADAPTERS,
    Adapter,
    Command,
    Input,
    OpticalInput,
    catalog,
    read_relative_signal,
)
from app.measurement_program import MeasurementSteps, configure, native_plan
from app.measurement_replay import Recording, recording_template, replay
from app.operation_plan import project_operation_plan, simulate_sequence
from app.project_assembly import component_instance_ids
from app.project_storage import make_snapshot, restore_snapshot
from app.spatial_fit import refresh_spatial_fit


def optical_project(case):
    project = apply_update(
        new_project(),
        {
            "workflow_kind": "general",
            "brief_objective": "Identify printed coupons and measure a registration mark."
            if case == "station"
            else "Compare light levels on two sides of a backlit panel.",
            "workspace_width_mm": 800,
        },
    )
    if case == "station":
        project["bench_station"] = compile_station(
            StationProposal(operations=["read_barcode", "inspect"]), 800
        )
    else:
        design = {
            "title": "Backlight uniformity stand",
            "overview": "Compare recorded signal on opposite sides of a backlit panel.",
            "components": [
                {
                    "key": "panel_rest",
                    "name": "Panel rest",
                    "role": "Locate the backlit panel.",
                    "primitives": [
                        {
                            "shape": "box",
                            "size_mm": [160, 10, 80],
                            "center_mm": [0, 5, 0],
                            "finish": "steel",
                        }
                    ],
                    "source_id": None,
                    "assembly": "Level the panel rest.",
                    "check": "Verify the panel position.",
                },
                {
                    "key": "optic_head",
                    "name": "Grayscale camera",
                    "role": "Observe both sides in one exposure.",
                    "primitives": [
                        {
                            "shape": "box",
                            "size_mm": [32, 28, 20],
                            "center_mm": [0, 14, 0],
                            "finish": "steel",
                        }
                    ],
                    "source_id": None,
                    "assembly": "Fix the camera above the panel.",
                    "check": "Verify framing and focus.",
                },
            ],
            "steps": [
                {
                    "name": "Place the panel",
                    "instruction": "Seat the panel and its references.",
                    "component_keys": ["panel_rest", "optic_head"],
                    "moving_key": None,
                    "translation_mm": [0, 0, 0],
                }
            ],
            "software": {
                "controller": "Recorded image replay.",
                "interfaces": ["Grayscale PNG file."],
                "setup": ["Identify the reference pair."],
                "checks": ["Verify both regions fit the frame."],
            },
            "open_questions": ["Which exposure is appropriate for the installed panel?"],
        }
        project["concept_design"] = compile_concept(design, 800)
        project["revision"] += 1
        from app.workflows import describe_workflow

        project["workflow"] = describe_workflow(project)
    refresh_spatial_fit(project)
    return approve_machine(project)


def steps_for(project, case, command="relative_signal"):
    ids = component_instance_ids(project)
    labels = (
        ["Read the registration mark"]
        if case == "station"
        else ["Measure the left region", "Measure the right region"]
    )
    return [
        {
            "key": f"reading-{i}",
            "label": label,
            "component_id": ids["inspect" if case == "station" else "optic_head"],
            "after_operation": "op-001",
            "adapter_id": "grayscale-camera",
            "command": command,
        }
        for i, label in enumerate(labels, 1)
    ]


def add_steps(project, case, command="relative_signal"):
    ctx = ToolContext(project, "Use the recorded camera for the indicated measurement.")
    result = asyncio.run(
        execute_tool(
            "configure_measurement_steps", {"steps": steps_for(project, case, command)}, ctx
        )
    )
    assert result.ok, result.summary
    return ctx.project, result


def optical_frame(sample_x=4, pixels=(20, 20, 220, 220, 70, 70, 170, 170)):
    buffer = BytesIO()
    Image.frombytes("L", (8, 2), bytes(pixels) * 2).save(buffer, format="PNG")

    def region(x):
        return {"x": x, "y": 0, "width": 2, "height": 2}

    return {
        "png_base64": base64.b64encode(buffer.getvalue()).decode(),
        "calibration_reference": "test-reference-pair",
        "dark": region(0),
        "reference": region(2),
        "sample": region(sample_x),
    }


def filled_recording(plan):
    rec = recording_template(plan)
    rec["item_id"] = "REF-42"
    for r in plan["operations"]:
        if r["mode"] == "manual":
            value = {"note": "Operator recorded placement or inspection."}
            if plan["bindings"][r["binding"]]["limits"].get("outcomes"):
                value["outcome"] = "pass"
        elif r["binding"] == "read_barcode":
            value = {"frame": "]C1REF-42"}
        else:
            value = optical_frame(6 if r["request_id"] == "reading-2" else 4)
        rec["inputs"][r["id"]] = value
    return rec


@pytest.mark.parametrize("case,expected", [("station", [25]), ("panel", [25, 75])])
def test_reuse_from_agent_through_download_reopen_and_offline_run(
    case, expected, monkeypatch, tmp_path
):
    project = optical_project(case)
    old = project["revision"]
    p, result = add_steps(project, case)
    assert result.mutated and p["revision"] == old + 1
    duplicate, same = add_steps(p, case)
    assert not same.mutated and duplicate["revision"] == p["revision"]
    monkeypatch.setitem(api._sessions, p["id"], api.ProjectSession(project=p))
    client = TestClient(api.app)
    root = f"/api/projects/{p['id']}/software/"
    query = {"revision": p["revision"]}
    seq = client.get(root + "operations", params=query).json()
    assert seq["scope"] == "approved" and sum(
        r["mode"] == "recorded_input" for r in seq["operations"]
    ) == len(expected)
    reopened, _ = restore_snapshot(make_snapshot(p, []))
    assert project_operation_plan(reopened) == seq
    dry = simulate_sequence(p)
    assert dry["ok"] and dry["operation_ids"] == [r["id"] for r in seq["operations"]]
    assert dry["steps_executed"] == 0
    data = filled_recording(seq)
    template = recording_template(seq)
    assert client.get(root + "recording-template", params=query).json() == template
    first_camera = next(r["id"] for r in seq["operations"] if r["mode"] == "recorded_input")
    regions = template["inputs"][first_camera]
    regions["sample"]["x"] = 4
    assert regions["dark"]["x"] is None and regions["reference"]["x"] is None
    response = client.post(root + "replay", params=query, json=data)
    assert response.status_code == 200, response.text
    replayed = response.json()
    assert [
        r["value"] for r in replayed["events"] if r["capability"] == "relative_signal"
    ] == expected
    assert [r["operation_id"] for r in replayed["events"]] == [r["id"] for r in seq["operations"]]
    assert replayed["physical_execution"] is False and replayed["status"] == "replayed"
    if case == "station":
        assert replayed["events"][0]["value"] == "REF-42"
        assert replayed["events"][-1]["provenance"] == "recorded_operator_note"
        with pytest.raises(ValueError, match="replay"):
            run_record(
                client.get(f"/api/projects/{p['id']}/bench-station/plan.json", params=query).json()
            )
    archive = client.get(root + "replay-kit.zip", params=query)
    with ZipFile(BytesIO(archive.content)) as kit:
        kit.extractall(tmp_path)
    (tmp_path / "recording.json").write_text(json.dumps(data), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "app.measurement_replay", "operation-plan.json", "recording.json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(proc.stdout) == replayed
    assert client.post(root + "replay", params={"revision": old}, json=data).status_code == 409
    assert (
        client.post(
            root + "replay", params=query, json={**data, "plan_sha256": "0" * 64}
        ).status_code
        == 409
    )
    if case == "station":
        data["inputs"]["op-001"]["frame"] = "WRONG"
        assert client.post(root + "replay", params=query, json=data).status_code == 422


def test_missing_command_reports_implementation_requirements_before_any_read():
    p, result = add_steps(optical_project("station"), "station", "set_exposure")
    seq = project_operation_plan(p)
    gap = result.data["integration_gaps"][0]
    assert gap["missing_capability"] == "set_exposure" and gap["supported_commands"] == [
        "relative_signal"
    ]
    data = filled_recording(seq)
    data["inputs"]["op-001"] = {"frame": "A mismatch would fail if a reader were invoked."}
    outcome = replay(seq, Recording.model_validate(data))
    assert outcome["status"] == "blocked" and not outcome["events"]
    issue = outcome["issues"][0]
    assert issue["component_id"] == component_instance_ids(p)["inspect"]
    assert issue["missing_capability"] == "set_exposure" and issue["integration_requirements"]


def test_changes_to_the_base_plan_adapter_version_or_component_require_review():
    p = optical_project("station")
    params = steps_for(p, "station")
    configured = configure(p, MeasurementSteps(steps=params))
    changed = deepcopy(configured)
    changed["bench_station"] = compile_station(
        StationProposal(operations=["inspect", "read_barcode"]), 800
    )
    changed = approve_machine(changed)
    assert (
        "Review the component"
        in project_operation_plan(changed)["bindings"]["measurement/reading-1"]["adapter"][
            "integration_requirements"
        ][0]
    )
    altered = deepcopy(configured)
    altered["measurement_program"]["steps"][0]["adapter_version"] = 10
    assert (
        "version"
        in project_operation_plan(altered)["bindings"]["measurement/reading-1"]["adapter"][
            "integration_requirements"
        ][0]
    )
    params[0]["component_id"] = "missing"
    with pytest.raises(ValueError, match="existing"):
        configure(p, MeasurementSteps(steps=params))
    params[0]["component_id"] = component_instance_ids(p)["read_barcode"]
    wrong = configure(p, MeasurementSteps(steps=params))
    assert project_operation_plan(wrong)["operations"][1]["mode"] == "missing_driver"
    cleared = configure(configured, MeasurementSteps(steps=[]))
    assert cleared["revision"] == configured["revision"] + 1
    assert "measurement_program" not in cleared
    assert project_operation_plan(cleared)["operations"] == native_plan(configured)["operations"]


@pytest.mark.parametrize(
    "bad",
    [
        "png",
        "mode",
        "truncated",
        "monochrome",
        "transparent",
        "overlap",
        "outside",
        "references",
        "missing",
    ],
)
def test_invalid_images_and_unusable_references_are_rejected(bad):
    data = optical_frame()
    if bad == "png":
        data["png_base64"] = "invalid data"
    elif bad == "truncated":
        raw = base64.b64decode(data["png_base64"])
        data["png_base64"] = base64.b64encode(raw[:39]).decode()
    elif bad in ("mode", "monochrome", "transparent"):
        out = BytesIO()
        mode = {"mode": "RGB", "monochrome": "1", "transparent": "L"}[bad]
        options = {"transparency": 0} if bad == "transparent" else {}
        Image.new(mode, (8, 2)).save(out, format="PNG", **options)
        data["png_base64"] = base64.b64encode(out.getvalue()).decode()
    elif bad == "overlap":
        data["sample"] = data["dark"]
    elif bad == "outside":
        data["sample"]["width"] = 6
    elif bad == "references":
        data = optical_frame(pixels=(20,) * 8)
    else:
        del data["calibration_reference"]
    with pytest.raises(ValueError):
        read_relative_signal(OpticalInput.model_validate(data), "TEST")


def test_new_registry_entry_declares_its_own_input_unit_and_operation(monkeypatch):
    class CounterReading(Input):
        count: int

    measure = Command(
        "counts",
        CounterReading,
        "Verify the counter zero.",
        lambda recorded, item_id: {"value": recorded.count},
        {"count": None},
    )
    instrument = Adapter(
        "Recorded counter", 1, {"read_count": measure}, ("Verify the counter interface.",)
    )
    monkeypatch.setitem(ADAPTERS, "test-counter", instrument)
    p = optical_project("panel")
    step = {**steps_for(p, "panel")[0], "adapter_id": "test-counter", "command": "read_count"}
    plan = project_operation_plan(configure(p, MeasurementSteps(steps=[step])))
    entry = catalog()["test-counter"]
    assert entry["commands"]["read_count"]["input_schema"]["required"] == ["count"]
    assert entry["simulation"] == "recording_replay" and not entry["physical_execution"]
    rec = recording_template(plan)
    assert rec["inputs"]["op-002"] == {"count": None}
    rec["item_id"] = "PANEL-42"
    rec["inputs"] = {"op-001": {"note": "Panel positioned."}, "op-002": {"count": 33}}
    event = replay(plan, Recording.model_validate(rec))["events"][1]
    assert (event["value"], event["unit"], event["component_id"]) == (
        33,
        "counts",
        step["component_id"],
    )


def test_out_of_span_reading_records_reference_and_is_not_clipped():
    data = optical_frame(pixels=(40, 40, 140, 140, 190, 190, 20, 20))
    result = read_relative_signal(OpticalInput.model_validate(data), "SAMPLE")
    assert result["value"] == 150 and result["outside_reference_span"]
    assert result["mean_dn"] == {"sample": 190, "dark": 40, "reference": 140}
    assert result["calibration_reference"] == data["calibration_reference"]
    assert result["regions_px"]["sample"] == data["sample"]
    assert len(result["frame_sha256"]) == 64


def test_existing_balance_and_failed_operator_checkpoint_keep_their_meanings():
    project = optical_project("station")
    project["bench_station"] = compile_station(
        StationProposal(operations=["weigh", "inspect"]), 800
    )
    project = approve_machine(project)
    params = steps_for(project, "station")
    params[0]["after_operation"] = "op-002"
    plan = project_operation_plan(configure(project, MeasurementSteps(steps=params)))
    data = recording_template(plan)
    data["item_id"] = "COUPON-9"
    data["inputs"]["op-001"] = {"frame": "S S 0.042 kg"}
    data["inputs"]["op-002"] = {"note": "Reference marking unreadable.", "outcome": "fail"}
    result = replay(plan, Recording.model_validate(data))
    assert result["status"] == "stopped"
    mass, review = result["events"]
    assert (mass["value"], mass["unit"], review["outcome"]) == (42, "g", "fail")
    data["inputs"]["op-002"]["outcome"] = "unspecified"
    with pytest.raises(ValueError, match="operator outcome"):
        replay(plan, Recording.model_validate(data))


def test_unregistered_adapter_and_unrelated_recording():
    p = optical_project("panel")
    params = steps_for(p, "panel")
    params[0]["adapter_id"] = "future-camera"
    plan = project_operation_plan(configure(p, MeasurementSteps(steps=params)))
    data = filled_recording(plan)
    blocked = replay(plan, Recording.model_validate(data))
    assert blocked["status"] == "blocked" and blocked["events"] == []
    assert blocked["issues"][0]["missing_capability"] == "relative_signal"
    with pytest.raises(ValueError, match="project, revision"):
        replay(plan, Recording.model_validate({**data, "project_id": "other"}))
    with pytest.raises(ValueError, match="digest"):
        replay({**plan, "revision": plan["revision"] + 1}, Recording.model_validate(data))
