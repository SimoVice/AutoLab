"""Integration tests for Phase 4 routes, spatial fit, Save/Open, and autonomous agent loop."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from build123d import Box, Cylinder, Pos, Unit, export_step
from fastapi.testclient import TestClient

from app import api
from app.domain import apply_update, new_project
from app.lab_profile import load_lab_profile, save_lab_profile
from app.open_hardware_discovery import clear_discovery_cache
from app.project_storage import make_snapshot, restore_snapshot
from app.spatial_fit import evaluate_spatial_fit


def _mock_transport(step_bytes: bytes, commit_sha: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://api.github.com/search/repositories"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "full_name": "openlab/syringe-pump",
                            "description": "Open hardware syringe pump with STEP CAD and BOM",
                            "html_url": "https://github.com/openlab/syringe-pump",
                            "default_branch": "main",
                            "stargazers_count": 250,
                            "topics": ["syringe-pump", "open-hardware"],
                            "license": {"spdx_id": "MIT"},
                        }
                    ]
                },
            )
        if "/repos/openlab/syringe-pump/commits/main" in url:
            return httpx.Response(200, json={"sha": commit_sha})
        if f"/repos/openlab/syringe-pump/git/trees/{commit_sha}" in url:
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"type": "blob", "path": "cad/pump-frame.step"},
                        {"type": "blob", "path": "bom/parts.csv"},
                        {"type": "blob", "path": "docs/ASSEMBLY.md"},
                        {"type": "blob", "path": "firmware/pump_driver.py"},
                    ]
                },
            )
        if url == f"https://raw.githubusercontent.com/openlab/syringe-pump/{commit_sha}/cad/pump-frame.step":
            return httpx.Response(200, content=step_bytes)
        if url == f"https://raw.githubusercontent.com/openlab/syringe-pump/{commit_sha}/bom/parts.csv":
            return httpx.Response(200, text="Part,Qty,SKU,Notes\nNEMA 17,1,17HS,Stepper\n")
        if url == f"https://raw.githubusercontent.com/openlab/syringe-pump/{commit_sha}/docs/ASSEMBLY.md":
            return httpx.Response(200, text="1. Bolt stepper to frame.\n2. Attach leadscrew.\n")
        if url == f"https://raw.githubusercontent.com/openlab/syringe-pump/{commit_sha}/firmware/pump_driver.py":
            return httpx.Response(200, text="import time\n# stepper driver\n")
        return httpx.Response(404, json={"results": [], "resultList": {"result": []}})

    return httpx.MockTransport(handler)


def _step_fixture(tmp_path: Path) -> bytes:
    solid = (
        Box(80, 50, 18)
        - Pos(28, 16, 0) * Cylinder(1.7, 25)
        - Pos(-28, -16, 0) * Cylinder(1.7, 25)
    )
    out = tmp_path / "pump-frame.step"
    export_step(solid, out, unit=Unit.MM)
    return out.read_bytes()


def test_upstream_http_routes_glb_preview_zip_and_save_open_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clear_discovery_cache()
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AUTOLAB_LAB_PROFILE_PATH", str(tmp_path / "lab-profile.json"))
    step_bytes = _step_fixture(tmp_path)
    commit_sha = "1234567890abcdef" * 2 + "12345678"
    transport = _mock_transport(step_bytes, commit_sha)

    orig_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.open_hardware_discovery.httpx.AsyncClient",
        lambda **kw: orig_async_client(transport=transport),
    )
    monkeypatch.setattr(
        "app.upstream_cad_ingest.httpx.AsyncClient",
        lambda **kw: orig_async_client(transport=transport),
    )
    monkeypatch.setattr(
        "app.upstream_docs.httpx.AsyncClient",
        lambda **kw: orig_async_client(transport=transport),
    )

    prof = load_lab_profile()
    prof["printer"] = {
        "name": "Bambu Lab A1 mini",
        "build_volume_mm": [180.0, 180.0, 180.0],
        "nozzle_mm": 0.4,
        "materials": ["PLA"],
    }
    save_lab_profile(prof)

    client = TestClient(api.app)
    created = client.post("/api/projects").json()["project"]
    pid = created["id"]

    disc = client.post(
        "/api/upstream/discover",
        json={"query": "syringe pump", "sources": ["github"], "max_results": 4},
    )
    assert disc.status_code == 200
    assert disc.json()["candidate_count"] == 1
    cand = disc.json()["candidates"][0]

    ing = client.post(
        f"/api/projects/{pid}/upstream/ingest",
        json={
            "project_revision": created["revision"],
            "source_host": cand["source_host"],
            "repo_full_name": cand["repo_full_name"],
            "commit_sha": cand["commit_sha"],
            "cad_file_path": cand["cad_files"][0],
        },
    )
    assert ing.status_code == 200
    ing_proj = ing.json()["project"]
    rev = ing_proj["revision"]
    assert ing_proj["upstream_project"]["provenance"]["spdx_id"] == "MIT"
    assert ing_proj["workflow"]["label"] == "Adapted open-source hardware"

    adp = client.post(
        f"/api/projects/{pid}/upstream/adapt",
        json={
            "project_revision": rev,
            "mode": "mating",
            "mating_spec": {
                "kind": "adapter_plate",
                "placement": "below",
                "clearance_mm": 0.4,
            },
        },
    )
    assert adp.status_code == 200
    adp_proj = adp.json()["project"]
    adp_rev = adp_proj["revision"]
    assert adp_proj["upstream_project"]["adaptation"]["interference"]["passed"] is True
    assert adp_proj["upstream_project"]["adaptation"]["printer_fit"]["fits"] is True

    # Spatial fit integrates upstream envelope & printable adaptation check cleanly.
    bounded = apply_update(
        adp_proj,
        {"workspace_width_mm": 400.0, "workspace_depth_mm": 350.0, "workspace_height_mm": 300.0},
    )
    fit = evaluate_spatial_fit(bounded, lab_profile=prof)
    assert fit["status"] == "fits"
    assert fit["subject"] == "upstream_project"
    assert any(c["fits"] is True for c in fit["printable_checks"])

    # GLB previews stream binary glTF
    for kind in ("upstream", "custom", "assembly"):
        glb = client.get(f"/api/projects/{pid}/upstream/preview/{kind}.glb?revision={adp_rev}")
        assert glb.status_code == 200
        assert glb.content[:4] == b"glTF"

    # Package ZIP download
    zip_resp = client.get(f"/api/projects/{pid}/upstream/download/package.zip?revision={adp_rev}")
    assert zip_resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
        assert "THIRD_PARTY_NOTICES.md" in zf.namelist()
        assert "bom/bom.csv" in zf.namelist()
        assert "custom/custom-adapter-plate.step" in zf.namelist()
        assert "upstream/source/pump-frame.step" in zf.namelist()

    # Save / Open snapshot round-trip preserves upstream_project cleanly
    snap = make_snapshot(adp_proj, [{"role": "user", "content": "Import open syringe pump"}])
    assert len(json.dumps(snap).encode("utf-8")) < 256 * 1024
    restored, _ = restore_snapshot(snap)
    assert restored["upstream_project"]["provenance"]["commit_sha"] == commit_sha
    assert restored["upstream_project"]["adaptation"]["part_id"] == "custom-adapter-plate"


def test_autonomous_agent_loop_discovers_ingests_and_adapts_open_hardware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from app.agent_loop import run_agent_turn
    from app.model_client import ModelSettings

    clear_discovery_cache()
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AUTOLAB_LAB_PROFILE_PATH", str(tmp_path / "lab-profile.json"))
    step_bytes = _step_fixture(tmp_path)
    commit_sha = "abcdef1234567890" * 2 + "abcdef12"
    transport = _mock_transport(step_bytes, commit_sha)

    orig_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.open_hardware_discovery.httpx.AsyncClient",
        lambda **kw: orig_async_client(transport=transport),
    )
    monkeypatch.setattr(
        "app.upstream_cad_ingest.httpx.AsyncClient",
        lambda **kw: orig_async_client(transport=transport),
    )
    monkeypatch.setattr(
        "app.upstream_docs.httpx.AsyncClient",
        lambda **kw: orig_async_client(transport=transport),
    )

    step_calls = 0

    class MockOpenAITransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal step_calls
            step_calls += 1
            if step_calls == 1:
                tool_call = {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "discover_open_hardware_projects",
                        "arguments": json.dumps(
                            {
                                "query": "syringe pump",
                                "sources": ["github"],
                                "max_results": 3,
                                "require_cad": True,
                                "commercial_use_ok": False,
                            }
                        ),
                    },
                }
            elif step_calls == 2:
                tool_call = {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "ingest_open_hardware_project",
                        "arguments": json.dumps(
                            {
                                "source_host": "github",
                                "repo_full_name": "openlab/syringe-pump",
                                "commit_sha": commit_sha,
                                "cad_file_path": "cad/pump-frame.step",
                                "allow_non_commercial": False,
                                "allow_unknown_license": False,
                                "source_units": "auto",
                                "source_up_axis": "z",
                            }
                        ),
                    },
                }
            elif step_calls == 3:
                tool_call = {
                    "id": "call_3",
                    "type": "function",
                    "function": {
                        "name": "adapt_open_hardware_project",
                        "arguments": json.dumps(
                            {
                                "mode": "mating",
                                "mating_kind": "adapter_plate",
                                "placement": "below",
                                "clearance_mm": 0.4,
                                "plate_thickness_mm": None,
                                "riser_height_mm": None,
                                "wall_thickness_mm": None,
                                "flange_margin_mm": None,
                                "hole_diameter_mm": None,
                                "secondary_hole_pitch_x_mm": None,
                                "secondary_hole_pitch_y_mm": None,
                                "cutout_width_mm": None,
                                "cutout_depth_mm": None,
                                "param_overrides": None,
                            }
                        ),
                    },
                }
            elif step_calls == 4:
                tool_call = {
                    "id": "call_4",
                    "type": "function",
                    "function": {
                        "name": "check_spatial_fit",
                        "arguments": json.dumps(
                            {
                                "platform_id": None,
                                "width_mm": None,
                                "depth_mm": None,
                                "height_mm": None,
                            }
                        ),
                    },
                }
            else:
                return httpx.Response(
                    200,
                    json={
                        "model": "gpt-test",
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 40,
                            "total_tokens": 140,
                        },
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": (
                                        "Discovered openlab/syringe-pump (MIT), normalized STEP "
                                        "CAD, and added a zero-interference adapter plate."
                                    ),
                                },
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "model": "gpt-test",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [tool_call],
                            },
                        }
                    ],
                },
            )

    proj = apply_update(
        new_project(),
        {"workspace_width_mm": 400.0, "workspace_depth_mm": 400.0, "workspace_height_mm": 300.0},
    )

    async def _run():
        settings = ModelSettings(
            api_key="test-key", base_url="https://mock.openai.local/v1", model="gpt-test"
        )
        async with orig_async_client(transport=MockOpenAITransport()) as oa_client:
            return await run_agent_turn(
                proj,
                (
                    "Find an open-source syringe pump on GitHub, import its STEP file, "
                    "and design a base adapter plate."
                ),
                [],
                client=oa_client,
                settings=settings,
            )

    res = asyncio.run(_run())
    tools_run = [t["tool"] for t in res["tool_trace"]]
    assert tools_run == [
        "discover_open_hardware_projects",
        "ingest_open_hardware_project",
        "adapt_open_hardware_project",
        "check_spatial_fit",
    ]
    assert res["project"]["upstream_project"]["adaptation"]["interference"]["passed"] is True


def test_upstream_project_allowed_in_liquid_preparation_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio
    from copy import deepcopy

    from app.upstream_service import apply_upstream_adaptation, attach_upstream_project

    clear_discovery_cache()
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AUTOLAB_LAB_PROFILE_PATH", str(tmp_path / "lab-profile.json"))
    step_bytes = _step_fixture(tmp_path)
    commit_sha = "f0e1d2c3b4a59687" * 2 + "f0e1d2c3"

    liq = apply_update(
        new_project(),
        {
            "workflow_kind": "liquid_preparation",
            "workspace_width_mm": 400.0,
            "workspace_depth_mm": 400.0,
            "workspace_height_mm": 400.0,
            "equipment": "Existing P200 and P1000 pipettes sharing tips",
            "stock_concentration": "10 mg/mL",
            "target_concentrations": "5 mg/mL, 2 mg/mL",
            "final_volume": "1 mL",
        },
    )

    async def _attach_and_adapt():
        attached = await attach_upstream_project(
            liq,
            source_host="github",
            repo_full_name="openlab/pipette-gantry",
            commit_sha=commit_sha,
            cad_file_path="cad/pump-frame.step",
            repo_url="https://github.com/openlab/pipette-gantry",
            spdx_hint="MIT",
            local_bytes=step_bytes,
            doc_texts_override={
                "bom/parts.csv": "Part,Qty,SKU,Notes\nNEMA 17,1,17HS,Stepper\n",
                "docs/ASSEMBLY.md": "1. Bolt stepper to frame.\n2. Mount pipette clamp.\n",
            },
        )
        return await apply_upstream_adaptation(
            attached,
            mode="mating",
            mating_spec={
                "kind": "adapter_plate",
                "placement": "below",
                "clearance_mm": 0.4,
            },
        )

    adp = asyncio.run(_attach_and_adapt())
    assert adp["workflow"]["kind"] == "liquid_preparation"
    assert adp["workflow"]["label"] == "Adapted open-source hardware"
    assert adp["workflow"]["design_available"] is True
    assert adp["upstream_project"]["adaptation"]["interference"]["passed"] is True

    updated = apply_update(adp, {"workspace_depth_mm": 380.0})
    assert updated["design"]["footprint"] == adp["design"]["footprint"]
    assert updated["design"]["spatial_fit"]["subject"] == "upstream_project"
    assert updated["design"]["spatial_fit"]["status"] == "fits"

    snap = make_snapshot(updated, [{"role": "user", "content": "Automate pipetting in 400mm box"}])
    restored, _ = restore_snapshot(snap)
    assert restored["workflow"]["kind"] == "liquid_preparation"
    assert restored["summary"]["stock_concentration"] == "10 mg/mL"
    assert restored["summary"]["equipment"] == "Existing P200 and P1000 pipettes sharing tips"
    assert restored["upstream_project"]["adaptation"]["part_id"] == "custom-adapter-plate"

    with_foundation = deepcopy(liq)
    with_foundation["design"]["foundation"] = {"id": "jubilee"}
    with pytest.raises(ValueError, match="keep the current workstation or design intact"):
        asyncio.run(
            attach_upstream_project(
                with_foundation,
                source_host="github",
                repo_full_name="openlab/pipette-gantry",
                commit_sha=commit_sha,
                cad_file_path="cad/pump-frame.step",
                repo_url="https://github.com/openlab/pipette-gantry",
                spdx_hint="MIT",
                local_bytes=step_bytes,
                doc_texts_override={},
            )
        )


def test_agent_prompt_enforces_proactive_discovery_and_pros_cons():
    from app.agent_loop import AGENT_SYSTEM_PROMPT

    assert "discover_open_hardware_projects" in AGENT_SYSTEM_PROMPT
    assert "liquid_preparation" in AGENT_SYSTEM_PROMPT
    assert "NEVER wait for the user" in AGENT_SYSTEM_PROMPT
    assert "Pros and Cons" in AGENT_SYSTEM_PROMPT
