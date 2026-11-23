"""HTTP endpoint tests for standalone fixtures, drawings, and build feedback (Phase 2)."""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient
from test_foundation_storage import source_project

import app.api as api
from app.domain import apply_update, new_project
from app.microplates import PROFILE_ID, LabwareSelection, select_plate


def test_fixture_endpoints_downloads_and_build_feedback_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOLAB_LAB_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.delenv("AUTOLAB_DATABASE_PATH", raising=False)
    monkeypatch.delenv("AUTOLAB_LIBRARY_DIRECTORY", raising=False)

    project = apply_update(
        new_project(),
        {"workflow_kind": "general", "workspace_width_mm": 350, "workspace_depth_mm": 260},
    )
    api._sessions[project["id"]] = api.ProjectSession(project=project)

    with TestClient(api.app) as client:
        refs = client.get("/api/fixture-references")
        assert refs.status_code == 200
        assert any(r["id"] == "slas_96_well_plate" for r in refs.json()["references"])

        stale = client.post(
            f"/api/projects/{project['id']}/fixture",
            json={"project_revision": 99, "spec": {"object_reference_id": "slas_96_well_plate"}},
        )
        assert stale.status_code == 409

        created = client.post(
            f"/api/projects/{project['id']}/fixture",
            json={
                "project_revision": project["revision"],
                "spec": {
                    "title": "Plate imaging rig",
                    "fixture_kind": "imaging_rig",
                    "object_reference_id": "slas_96_well_plate",
                },
            },
        )
        assert created.status_code == 200
        rev1_proj = created.json()["project"]
        rev1 = rev1_proj["revision"]
        assert rev1_proj["standalone_fixture"]["derived"]["pocket_length_mm"] == 128.66

        svg = client.get(
            f"/api/projects/{project['id']}/fixture/download/drawing.svg?revision={rev1}"
        )
        assert svg.status_code == 200 and b"128.66" in svg.content

        pdf = client.get(
            f"/api/projects/{project['id']}/fixture/download/drawing.pdf?revision={rev1}"
        )
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-1.4")

        coupon = client.get(
            f"/api/projects/{project['id']}/fixture/download/tolerance-coupon.stl?revision={rev1}"
        )
        assert coupon.status_code == 200 and len(coupon.content) > 100

        partial_zip = client.get(
            f"/api/projects/{project['id']}/fixture/download/package.zip"
            f"?revision={rev1}&parts=locating-base"
        )
        assert partial_zip.status_code == 200
        with zipfile.ZipFile(io.BytesIO(partial_zip.content)) as zf:
            names = set(zf.namelist())
            assert "parts/locating-base.stl" in names
            assert "parts/camera-mast.stl" not in names
            assert "drawing.pdf" in names

        logged = client.post(
            f"/api/projects/{project['id']}/build-logs",
            json={
                "project_revision": rev1,
                "log": {
                    "parts_printed": [
                        "locating-base",
                        "camera-mast",
                        "camera-arm",
                        "tolerance-coupon",
                    ],
                    "fit_result": "too_tight",
                    "measured_features": [
                        {
                            "feature": "pocket_length",
                            "axis": "length",
                            "nominal_mm": 128.66,
                            "measured_mm": 128.35,
                        }
                    ],
                },
            },
        )
        assert logged.status_code == 201
        log_id = logged.json()["log"]["id"]

        revised = client.post(
            f"/api/projects/{project['id']}/build-logs/{log_id}/revise",
            json={"project_revision": rev1},
        )
        assert revised.status_code == 200
        rev2_proj = revised.json()["project"]
        assert rev2_proj["revision"] == rev1 + 1
        assert rev2_proj["standalone_fixture"]["derived"]["pocket_length_mm"] == 128.97
        summary = revised.json()["revision_summary"]
        assert set(summary["parts_to_reprint"]) == {"locating-base", "tolerance-coupon"}
        assert set(summary["parts_unchanged"]) == {"camera-mast", "camera-arm"}

        logs_list = client.get(f"/api/projects/{project['id']}/build-logs")
        assert logs_list.status_code == 200
        assert logs_list.json()["build_feedback"]["tested_revision"] == rev1


def test_labware_drawing_endpoints():
    plate_proj = source_project()
    plate_proj, _run = select_plate(
        plate_proj,
        LabwareSelection(profile_id=PROFILE_ID, source="96-well plate"),
        "Use a Corning 96-well plate.",
    )
    api._sessions[plate_proj["id"]] = api.ProjectSession(project=plate_proj)
    rev = plate_proj["revision"]

    with TestClient(api.app) as client:
        svg = client.get(f"/api/projects/{plate_proj['id']}/labware/drawing.svg?revision={rev}")
        assert svg.status_code == 200 and b"129.36 x 87.08" in svg.content
        pdf = client.get(f"/api/projects/{plate_proj['id']}/labware/drawing.pdf?revision={rev}")
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-1.4")
