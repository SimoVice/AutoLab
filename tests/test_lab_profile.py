"""Tests for persistent laboratory profile storage, API routes, and dimension provenance."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient
from test_model_client import TEST_SETTINGS, valid_response

import app.api as api
import app.lab_profile as lab_profile
import app.model_client as model_client
from app.domain import apply_update, new_project
from app.labware import LabwareUpdate, develop_labware, update_labware_brief
from app.microplates import PROFILE_ID, LabwareSelection, select_plate
from app.project_storage import decode_snapshot, make_snapshot, restore_snapshot


@pytest.fixture
def isolated_profile_env(monkeypatch, tmp_path):
    profile_file = tmp_path / "lab-profile.json"
    monkeypatch.setenv("AUTOLAB_LAB_PROFILE_PATH", str(profile_file))
    monkeypatch.delenv("AUTOLAB_DATABASE_PATH", raising=False)
    monkeypatch.delenv("AUTOLAB_LIBRARY_DIRECTORY", raising=False)
    return profile_file


def test_load_default_does_not_write_and_json_roundtrip(isolated_profile_env):
    assert not isolated_profile_env.exists()
    empty = lab_profile.load_lab_profile()
    assert not lab_profile.has_lab_profile_data(empty)
    assert not isolated_profile_env.exists()

    saved = lab_profile.save_lab_profile(
        {
            "lab_name": " Wet Lab B ",
            "printer": {
                "name": "Bambu Lab A1 mini",
                "build_volume_mm": [180, 180, 180],
                "nozzle_mm": 0.4,
                "materials": ["PLA", "pla", "PETG"],
            },
            "bench": {"width_mm": 650, "depth_mm": 500, "height_mm": 750},
            "measuring_tools": ["Digital caliper"],
            "owned_equipment": ["Ohaus balance"],
        }
    )
    assert isolated_profile_env.exists()
    assert saved["lab_name"] == "Wet Lab B"
    assert saved["printer"]["materials"] == ["PLA", "PETG"]
    reloaded = lab_profile.load_lab_profile()
    assert reloaded == saved
    assert lab_profile.has_lab_profile_data(reloaded)


def test_sqlite_lab_profile_persistence(monkeypatch, tmp_path):
    db_file = tmp_path / "autolab.db"
    monkeypatch.delenv("AUTOLAB_LAB_PROFILE_PATH", raising=False)
    monkeypatch.delenv("AUTOLAB_LIBRARY_DIRECTORY", raising=False)
    monkeypatch.setenv("AUTOLAB_DATABASE_PATH", str(db_file))

    assert not lab_profile.has_lab_profile_data(lab_profile.load_lab_profile())
    saved = lab_profile.save_lab_profile(
        {
            "lab_name": "Biofoundry Alpha",
            "printer": {"name": "Prusa MK4S", "build_volume_mm": [250, 210, 220]},
        }
    )
    assert db_file.exists()
    assert lab_profile.load_lab_profile() == saved


def test_invalid_printer_or_bench_dimensions_rejected(isolated_profile_env):
    with pytest.raises(ValueError):
        lab_profile.save_lab_profile({"printer": {"build_volume_mm": [180, -10, 180]}})
    with pytest.raises(ValueError):
        lab_profile.save_lab_profile({"bench": {"width_mm": 0}})


def test_reference_vs_user_measured_provenance_and_snapshot_roundtrip(isolated_profile_env):
    from test_foundation_storage import source_project

    project = source_project()
    # User-measured tube rack dimensions
    rack_msg = (
        "Use 12 tubes in 4 columns with body diameter 10.8 mm, rim diameter 13.2 mm, "
        "closed cap diameter 13.6 mm, length below the rim 38.0 mm."
    )
    project = update_labware_brief(
        project,
        LabwareUpdate(
            count=12,
            columns=4,
            body_diameter_max_mm=10.8,
            rim_diameter_min_mm=13.2,
            cap_diameter_max_mm=13.6,
            below_rim_height_mm=38.0,
            source_count="12 tubes",
            source_columns="4 columns",
            source_body_diameter_max_mm="body diameter 10.8 mm",
            source_rim_diameter_min_mm="rim diameter 13.2 mm",
            source_cap_diameter_max_mm="closed cap diameter 13.6 mm",
            source_below_rim_height_mm="length below the rim 38.0 mm",
        ),
        rack_msg,
    )
    project, _ = develop_labware(project)
    prov = project["dimension_provenance"]
    assert prov["tube_body_diameter_max_mm"]["provenance"] == "user_measured"
    assert prov["tube_body_diameter_max_mm"]["value_mm"] == 10.8

    # Switch to reference Corning plate
    plate_msg = "Switch to the Corning 96 well plate."
    project, _ = select_plate(
        project,
        LabwareSelection(profile_id=PROFILE_ID, source="Corning 96 well plate"),
        plate_msg,
    )
    prov = project["dimension_provenance"]
    assert prov["plate_holder_width_mm"]["provenance"] == "reference"
    assert prov["plate_holder_width_mm"]["source"] == PROFILE_ID

    snapshot = make_snapshot(project, [])
    restored, _ = restore_snapshot(decode_snapshot(json.dumps(snapshot).encode()))
    assert restored["dimension_provenance"] == project["dimension_provenance"]

    tampered = deepcopy(snapshot)
    tampered["dimension_provenance"]["plate_holder_width_mm"]["provenance"] = "guessed"
    with pytest.raises(ValueError):
        restore_snapshot(tampered)


def test_lab_profile_api_and_apply_to_project(isolated_profile_env):
    api._sessions.clear()
    with TestClient(api.app) as client:
        get_resp = client.get("/api/lab-profile")
        assert get_resp.status_code == 200
        body = get_resp.json()
        assert len(body["presets"]) >= 3
        assert body["presets"][0]["build_volume_mm"] == [180.0, 180.0, 180.0]

        created = client.post("/api/projects").json()["project"]
        pid = created["id"]

        empty_apply = client.post(
            f"/api/projects/{pid}/apply-lab-profile",
            json={"project_revision": created["revision"]},
        )
        assert empty_apply.status_code == 409

        put_resp = client.put(
            "/api/lab-profile",
            json={
                "lab_name": "Bench 4",
                "printer": {
                    "name": "Bambu Lab A1 mini",
                    "build_volume_mm": [180, 180, 180],
                    "materials": ["PLA"],
                },
                "bench": {"width_mm": 550, "depth_mm": 450, "height_mm": 600},
                "measuring_tools": ["Digital caliper"],
                "owned_equipment": ["Fixed overhead camera"],
            },
        )
        assert put_resp.status_code == 200
        applied = client.post(
            f"/api/projects/{pid}/apply-lab-profile",
            json={"project_revision": created["revision"]},
        )
        assert applied.status_code == 200
        updated = applied.json()["project"]
        assert updated["summary"]["workspace_width_mm"] == 550.0
        assert "550 mm" in (updated["brief"]["constraints"] or "")
        assert updated["brief"]["equipment"] == "Fixed overhead camera"
        assert updated["dimension_provenance"]["bench_width_mm"]["provenance"] == "user_measured"
    api._sessions.clear()


def test_model_client_includes_lab_profile_only_when_configured(isolated_profile_env):
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=valid_response(assistant_message="Noted."))

    proj = apply_update(new_project(), {"workflow_kind": "general"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as fake_client:
            await model_client.generate_turn(
                proj, "Hello", [], client=fake_client, settings=TEST_SETTINGS
            )
            assert '"lab_profile"' not in captured[0]["messages"][1]["content"]

            lab_profile.save_lab_profile(
                {"printer": {"name": "Bambu Lab A1 mini", "build_volume_mm": [180, 180, 180]}}
            )
            await model_client.generate_turn(
                proj, "Hello", [], client=fake_client, settings=TEST_SETTINGS
            )
            assert '"lab_profile"' in captured[1]["messages"][1]["content"]
            assert "Bambu Lab A1 mini" in captured[1]["messages"][1]["content"]

    asyncio.run(run())
