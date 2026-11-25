"""Mixed assemblies use real CAD workers and deterministic, offline source transports."""

import asyncio
import hashlib
import json
import struct
from copy import deepcopy

import httpx
import numpy as np
import pytest
from build123d import Box, Pos, export_step, export_stl
from fastapi.testclient import TestClient
from scipy.spatial.transform import Rotation
from test_concept_interfaces import component
from test_concept_interfaces import fixture as optical_proposal
from test_custom_project_routes import project_fixture

from app import api
from app.agent_tools import ToolContext, execute_tool
from app.bom_engine import build_complete_bom
from app.cad_instances import instance_file, validate_pin
from app.concept_design import compile_concept
from app.design_workflow import approve_machine, workflow_state
from app.lab_profile import load_lab_profile, save_lab_profile
from app.project_assembly import assembly_binding, physical_assembly
from app.project_storage import make_snapshot, restore_snapshot
from app.spatial_fit import evaluate_spatial_fit


def assert_mesh_bounds(data, geometry):
    """Read glTF vertex positions and node transforms independently of the CAD worker."""
    size = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20 : 20 + size])
    binary = data[28 + size :]
    vertices = []

    def visit(index, parent):
        node = doc["nodes"][index]
        if "matrix" in node:
            local = np.array(node["matrix"]).reshape(4, 4).T
        else:
            local = np.eye(4)
            rotation = Rotation.from_quat(node.get("rotation", [0, 0, 0, 1])).as_matrix()
            local[:3, :3] = rotation @ np.diag(node.get("scale", [1, 1, 1]))
            local[:3, 3] = node.get("translation", [0, 0, 0])
        world = parent @ local
        if "mesh" in node:
            for primitive in doc["meshes"][node["mesh"]]["primitives"]:
                attr = doc["accessors"][primitive["attributes"]["POSITION"]]
                view = doc["bufferViews"][attr["bufferView"]]
                points = np.ndarray(
                    (attr["count"], 3),
                    dtype="<f4",
                    buffer=binary,
                    offset=view.get("byteOffset", 0) + attr.get("byteOffset", 0),
                    strides=(view.get("byteStride", 12), 4),
                )
                vertices.append(1000 * (points @ world[:3, :3].T + world[:3, 3]))
        for child in node.get("children", []):
            visit(child, world)

    for root in doc["scenes"][doc.get("scene", 0)]["nodes"]:
        visit(root, np.eye(4))
    points = np.concatenate(vertices)
    low, high = geometry["cad_min_mm"], geometry["cad_max_mm"]
    assert points.min(axis=0) == pytest.approx([low[0], low[2], -high[1]], abs=0.1)
    assert points.max(axis=0) == pytest.approx([high[0], high[2], -low[1]], abs=0.1)


def optical_host():
    project = project_fixture()
    proposal = optical_proposal()
    next(p for p in proposal["components"] if p["key"] == "camera")["source_id"] = (
        "rpi-camera-module-3"
    )
    proposal["components"][0]["primitives"][0]["size_mm"] = [270, 10, 160]
    board = component("controller", "box", [51, 8, 21], "base", [-105, 10, -35])
    board.update(name="Readout controller", source_id="pico-h")
    proposal["components"].append(board)
    proposal["components"][0]["name"] = "Printed optical base"
    project["concept_design"] = compile_concept(proposal, 600)
    message = "Use SC0917. I have 1 unit on hand."
    ctx = ToolContext(project, message)
    choice = asyncio.run(
        execute_tool(
            "choose_requirement_part",
            {
                "line_id": "concept:controller",
                "action": "select",
                "catalog_id": "pico-h",
                "owned_quantity": 1,
                "inventory_quote": message,
                "user_quote": message,
            },
            ctx,
        )
    )
    assert choice.ok, choice
    return ctx.project


def test_attach_two_sources_and_adapt_from_normal_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AUTOLAB_LAB_PROFILE_PATH", str(tmp_path / "lab.json"))
    lab = load_lab_profile()
    lab["printer"] = {"name": "Test printer", "build_volume_mm": [100, 100, 100]}
    save_lab_profile(lab)
    assets = {}
    for repo, dimensions in (("lamp", (22, 14, 6)), ("lens", (16, 12, 8))):
        path = tmp_path / (repo + ".step")
        export_step(Pos(7, -13, 21) * Box(*dimensions), path)
        assets[repo] = path.read_bytes()

    def respond(request):
        repo = request.url.path.split("/")[2]
        if request.url.path.endswith(".step"):
            return httpx.Response(200, content=assets[repo])
        return httpx.Response(404)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: original_client(transport=httpx.MockTransport(respond))
    )
    monkeypatch.setattr(
        "app.upstream_service.lookup_discovered_candidate", lambda *args: {"spdx_id": "MIT"}
    )
    client = TestClient(api.app)
    host = approve_machine(optical_host())
    session = api.ProjectSession(project=host)
    monkeypatch.setitem(api._sessions, host["id"], session)
    base = "/api/projects/" + host["id"]
    route = base + "/upstream/ingest"
    first = {
        "instance_id": "illuminator",
        "source_host": "github",
        "repo_full_name": "test/lamp",
        "commit_sha": "a" * 40,
        "cad_file_path": "cad/body.step",
        "expected_sha256": hashlib.sha256(assets["lamp"]).hexdigest(),
        "placement": {
            "parent": "base",
            "translation_mm": [85, 17, 12],
            "rotation_deg_xyz": [0, 90, 0],
            "datum": "Offset from the plate centre.",
            "access": {"face": "+x", "distance_mm": 12},
        },
    }

    def ingest(data, revision):
        return client.post(route, json={**data, "project_revision": revision})

    bad = ingest({**first, "expected_sha256": "0" * 64}, host["revision"])
    assert bad.status_code == 422 and session.project is host
    assert workflow_state(session.project)["machine_approved"]
    accepted = ingest(first, host["revision"])
    assert accepted.status_code == 200, accepted.text
    current = session.project
    assert not workflow_state(current)["machine_approved"]
    assert current["concept_design"] == host["concept_design"]
    assert ingest(first, host["revision"]).status_code == 409
    assert ingest(first, current["revision"]).json()["project"]["revision"] == current["revision"]
    assert ingest({**first, "commit_sha": "b" * 40}, current["revision"]).status_code == 422
    assert session.project is current

    second = {
        **first,
        "instance_id": "lens",
        "repo_full_name": "test/lens",
        "commit_sha": "b" * 40,
        "expected_sha256": hashlib.sha256(assets["lens"]).hexdigest(),
        "placement": {
            "parent": "cad:illuminator",
            "translation_mm": [21, 0, 0],
            "datum": "21 mm along the illuminator X axis.",
        },
    }
    stale = ingest(second, host["revision"])
    assert stale.status_code == 409 and session.project == current
    conflict = ingest({**first, "commit_sha": "c" * 40}, current["revision"])
    assert conflict.status_code == 422 and session.project == current
    missing = ingest(
        {**second, "placement": {**second["placement"], "parent": "removed-part"}},
        current["revision"],
    )
    assert missing.status_code == 422 and session.project == current
    assert ingest(first, current["revision"]).json()["project"] == current
    ctx = ToolContext(deepcopy(current), "Add the lens to the optical fixture.", lab_profile=lab)

    def agent(tool, args):
        result = asyncio.run(execute_tool(tool, args, ctx))
        assert result.ok, result
        return result

    agent("ingest_open_hardware_project", second)
    assert not agent("ingest_open_hardware_project", second).mutated
    inspect = agent("inspect_upstream_project", {"instance_id": "lens", "section": "license"})
    assert inspect.data["provenance"]["commit_sha"] == "b" * 40
    lens_before = deepcopy(ctx.project["cad_instances"]["lens"])
    adapt = {"instance_id": "illuminator", "mode": "mating", "mating_kind": "adapter_plate"}
    agent("adapt_open_hardware_project", adapt)
    assert not agent("adapt_open_hardware_project", adapt).mutated
    current = ctx.project
    assert current["revision"] == host["revision"] + 3
    assert current["cad_instances"]["lens"] == lens_before
    assert (
        current["cad_instances"]["illuminator"]["provenance"]["file_sha256"]
        == first["expected_sha256"]
    )
    graph = physical_assembly(current)
    parts = {p["native_id"]: p for p in graph["instances"]}
    assert {
        "base",
        "camera",
        "cad:lens",
        "cad:illuminator",
        "cad:illuminator:adapter",
    } <= parts.keys()
    assert any(
        row.get("owned_quantity") == 1 and parts["controller"]["id"] in row["instance_ids"]
        for row in graph["requirements"]
    )
    rows = build_complete_bom(current, lab_profile=lab)["lines"]
    assert len({r["line_id"] for r in rows}) == len(rows)
    assert len([r for r in rows if r["component_ids"] == ["cad:illuminator:adapter"]]) == 1
    assert all(r.get("line_total") is None for r in rows if r["component_ids"] == ["cad:lens"])
    checks = evaluate_spatial_fit(current, lab_profile=lab)["cad_assembly"]
    lamp, lens = checks["instances"]
    assert lamp["access_status"] == "review" and lamp["access_blockers"] == ["cad:lens"]
    assert lens["world_translation_mm"] == pytest.approx(
        [
            lamp["world_translation_mm"][0],
            lamp["world_translation_mm"][1],
            lamp["world_translation_mm"][2] - 21,
        ]
    )
    assert lamp["mating_interference"]["method"] == "brep_boolean_intersection"
    assert lamp["mating_interference"]["passed"] and lamp["printer_fit"]["fits"]
    small = evaluate_spatial_fit(current, lab_profile={"printer": {"build_volume_mm": [3, 3, 3]}})
    assert small["cad_assembly"]["instances"][0]["printer_fit"]["fits"] is False
    assert evaluate_spatial_fit(current)["cad_assembly"]["instances"][0]["printer_fit"] is None
    session.project = current
    for name, role, prefix in (
        ("lens", "original", b"ISO-10303"),
        ("illuminator", "preview", b"glTF"),
        ("illuminator", "adapter-step", b"ISO-10303"),
        ("illuminator", "adapter-preview", b"glTF"),
    ):
        asset = client.get(
            f"{base}/upstream/instances/{name}/{role}", params={"revision": current["revision"]}
        )
        assert asset.status_code == 200 and asset.content.startswith(prefix)
        if role.endswith("preview"):
            record = current["cad_instances"][name]
            geometry = (
                record["geometry"] if role == "preview" else record["adaptation"]["custom_geometry"]
            )
            assert_mesh_bounds(asset.content, geometry)

    saved = make_snapshot(current, [])
    assert str(tmp_path) not in str(saved["cad_instances"])
    restored, _ = restore_snapshot(saved)
    assert assembly_binding(restored) == assembly_binding(current)
    broken = deepcopy(saved)
    broken["cad_instances"]["lens"]["placement"]["parent"] = "cad:lens"
    with pytest.raises(ValueError):
        restore_snapshot(broken)
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "fresh"))
    assert assembly_binding(restore_snapshot(saved)[0]) == assembly_binding(current)
    assert (
        client.get(
            f"{base}/upstream/instances/lens/preview", params={"revision": current["revision"]}
        ).status_code
        == 404
    )


@pytest.mark.parametrize("revision,path", [("main", "part.step"), ("1" * 40, "part.py")])
def test_a_named_source_requires_pinned_passive_geometry(revision, path):
    with pytest.raises(ValueError):
        validate_pin("github", "test/optics", revision, path)


def test_two_instances_of_one_mesh_keep_independent_units_and_previews(tmp_path, monkeypatch):
    from app.upstream_service import attach_upstream_project

    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    mesh = tmp_path / "scale.stl"
    export_stl(Box(2, 3, 1), mesh)
    source = {
        "source_host": "github",
        "repo_full_name": "fixture/scaleblock",
        "commit_sha": "d" * 40,
        "cad_file_path": "scale.stl",
        "local_bytes": mesh.read_bytes(),
        "spdx_hint": "MIT",
        "doc_texts_override": {"README.md": ""},
        "placement": {
            "parent": "project",
            "translation_mm": [100, 15, 20],
            "datum": "Centre of the test jig.",
        },
    }
    project = project_fixture()
    with pytest.raises(ValueError, match="units"):
        asyncio.run(attach_upstream_project(project, instance_id="metric", **source))
    metric = asyncio.run(
        attach_upstream_project(project, instance_id="metric", source_units="mm", **source)
    )
    mixed = asyncio.run(
        attach_upstream_project(metric, instance_id="inch", source_units="in", **source)
    )
    records = mixed["cad_instances"]
    assert records["metric"] == metric["cad_instances"]["metric"]
    assert (
        records["metric"]["artifact_hashes"]["original"]
        == records["inch"]["artifact_hashes"]["original"]
    )
    assert (
        records["metric"]["artifact_hashes"]["preview"]
        != records["inch"]["artifact_hashes"]["preview"]
    )
    assert records["inch"]["geometry"]["app_size_mm"] == pytest.approx([50.8, 25.4, 76.2], abs=0.01)
    for record in records.values():
        assert record["geometry"]["detected_units"] == "unitless"
        assert_mesh_bounds(instance_file(record, "preview").read_bytes(), record["geometry"])
    overlaps = mixed["design"]["spatial_fit"]["cad_assembly"]["possible_overlaps"]
    assert ["cad:inch", "cad:metric"] in overlaps
