"""Offline coverage of one mating revision and its portable assembly package."""

import asyncio
import io
import json
import zipfile
from copy import deepcopy

import pytest
from build123d import Box, export_step
from test_cad_instances import optical_host

from app.agent_tools import ToolContext, execute_tool
from app.cad_instances import instance_file
from app.mixed_handoff import _pack, _unpack, build_package, encode, open_package
from app.mixed_sources import digest
from app.project_assembly import physical_assembly
from app.project_storage import make_snapshot
from app.upstream_service import attach_upstream_project


@pytest.fixture(scope="module")
def issued(tmp_path_factory):
    folder = tmp_path_factory.mktemp("issued")
    with pytest.MonkeyPatch.context() as env:
        env.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(folder / "cache"))
        path = folder / "holder.step"
        export_step(Box(18, 12, 5), path)
        project = asyncio.run(
            attach_upstream_project(
                optical_host(),
                instance_id="holder",
                source_host="github",
                repo_full_name="fixture/holder",
                commit_sha="e" * 40,
                cad_file_path="holder.step",
                spdx_hint="MIT",
                license_text="Test fixture notice. Retain attribution.",
                local_bytes=path.read_bytes(),
                doc_texts_override={"README.md": ""},
                placement={
                    "parent": "base",
                    "translation_mm": [100, 30, 15],
                    "rotation_deg_xyz": [10, 30, 5],
                    "datum": "Base reference corner.",
                },
            )
        )
        ctx = ToolContext(project, "Change only the plate thickness to 11 mm.")

        def adapt(**spec):
            result = asyncio.run(
                execute_tool(
                    "adapt_open_hardware_project",
                    {"instance_id": "holder", "mode": "mating", **spec},
                    ctx,
                )
            )
            assert result.ok, result
            return result.mutated

        assert adapt(
            mating_kind="adapter_plate",
            plate_thickness_mm=5,
            clearance_mm=0.8,
            placement="above",
            flange_margin_mm=16,
        )
        before = deepcopy(ctx.project)
        assert adapt(plate_thickness_mm=11)
        assert not adapt(plate_thickness_mm=11)
        project = ctx.project
        raw = asyncio.run(build_package(project, make_snapshot(project, [])))
        return before, project, raw


def test_partial_revision_rebuilds_only_one_mating_part(issued):
    before, project, raw = issued
    old, new = (p["cad_instances"]["holder"] for p in (before, project))
    assert new["adaptation"]["spec"] == {**old["adaptation"]["spec"], "plate_thickness_mm": 11}
    assert project["revision"] == before["revision"] + 1
    assert new["artifact_hashes"]["original"] == old["artifact_hashes"]["original"]
    manifest, files = _unpack(raw)
    assert "software/controller.py" in files and "software/gateway-config.json" in files
    assert files["sources/instances/holder/license-text.txt"].startswith(b"Test fixture notice.")
    change = json.loads(files["assembly/changes.json"])["last_mating_change"]
    assert change["dimensions"] == {"plate_thickness_mm": {"before": 5, "after": 11}}
    graph = physical_assembly(project)
    assert manifest["geometry_sha256"] == graph["geometry_sha256"]
    different = [
        p["id"]
        for p, q in zip(graph["instances"], physical_assembly(before)["instances"])
        if p != q
    ]
    assert different == change["reprint"] and len(different) == 1
    assert change["affected_bom_lines"] and len(change["recheck_mount_datums"]) == 1


def test_reopen_verified_assets_and_issue_following_revision(issued, monkeypatch, tmp_path):
    _, project, raw = issued
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "empty"))
    opened, _ = open_package(raw)
    assert opened["id"] != project["id"]
    assert physical_assembly(opened) == physical_assembly(project)
    item = opened["cad_instances"]["holder"]
    for role, sha in item["artifact_hashes"].items():
        assert digest(instance_file(item, role).read_bytes()) == sha
    ctx = ToolContext(opened, "Change only the clearance to 1 mm.")
    result = asyncio.run(
        execute_tool(
            "adapt_open_hardware_project",
            {"instance_id": "holder", "mode": "mating", "clearance_mm": 1},
            ctx,
        )
    )
    assert result.ok and ctx.project["revision"] == opened["revision"] + 1
    assert ctx.project["mating_change"]["current_spec"]["plate_thickness_mm"] == 11


@pytest.mark.parametrize(
    "damage", ["file", "pin", "revision", "extra", "missing", "guide", "software"]
)
def test_incomplete_or_inconsistent_package_preserves_empty_workspace(
    issued, damage, monkeypatch, tmp_path
):
    raw = issued[2]
    manifest, files = _unpack(raw)
    binding = {k: manifest[k] for k in ("project_id", "revision", "geometry_sha256")}
    if damage == "pin":
        files["sources/instances/holder/original.step"] += b" "
    elif damage == "file":
        stale = json.loads(files["software/device-bindings.json"])
        stale["revision"] -= 1
        files["software/device-bindings.json"] = encode(stale)
    elif damage == "extra":
        files["sources/extra.txt"] = b"Unlisted source"
    elif damage == "missing":
        del files["sources/instances/holder/preview.glb"]
    elif damage == "guide":
        files["assembly/guide.md"] = b"Earlier guide"
    elif damage == "software":
        files["software/controller.py"] = b"# Incomplete archived controller"
    else:
        binding["revision"] += 1
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        open_package(_pack(files, binding))
    assert not list(tmp_path.rglob("*.step"))


def test_issuing_counts_the_manifest_toward_the_expanded_limit(monkeypatch):
    from app import mixed_handoff

    monkeypatch.setattr(mixed_handoff, "MAX_EXPANDED_BYTES", 128)
    with pytest.raises(ValueError, match="portable package limit"):
        _pack({"project.json": bytes(127)}, {})


def test_corrupt_member_is_reported_before_install(issued):
    manifest, files = _unpack(issued[2])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("manifest.json", encode(manifest))
        for member, content in files.items():
            archive.writestr(
                member, b"*" * len(content) if member == "cad/assembly.step" else content
            )
    with pytest.raises(ValueError, match="Digest mismatch"):
        open_package(out.getvalue())


def test_workstation_step_places_each_named_component_in_the_scene_frame(tmp_path):
    from build123d import import_step
    from test_commercial_design import layout_project, proposal

    from app.commercial_design import adopt_commercial, compile_commercial_layout
    from app.mixed_geometry import build_mixed_cad

    project = compile_commercial_layout(adopt_commercial(layout_project()), proposal(), {})
    project["cad_instances"] = {}
    files = build_mixed_cad(project)
    path = tmp_path / "review.step"
    path.write_bytes(files["cad/assembly.step"])
    parts = {p.label: p for p in import_step(path).children}
    expected = {p["id"]: p for p in physical_assembly(project)["instances"]}
    assert parts.keys() == expected.keys()
    for entry in json.loads(files["cad/geometry.json"])["parts"]:
        shape = parts[entry["instance_id"]]
        box = shape.bounding_box()
        assert list(box.min) == pytest.approx(entry["bounds_mm"][0], abs=0.001)
        assert list(box.max) == pytest.approx(entry["bounds_mm"][1], abs=0.001)
    carriage = next(p for p in expected.values() if p["native_id"] == "carriage")
    measured = parts[carriage["id"]].bounding_box().center()
    assert list(measured) == pytest.approx(carriage["placement"]["world_translation_mm"])


def test_large_review_mesh_retains_solid_instances_and_a_free_face(tmp_path):
    import struct

    from build123d import Compound, Face, Pos, import_stl

    from app.mixed_geometry import _write_assembly_stl

    block = Box(1, 1, 1)
    blocks = Compound([Pos(x * 2, 0, 0) * block for x in range(129)])
    sheet = Pos(0, 0, 9) * Face.make_rect(2, 3)
    mixed = Compound([blocks, sheet])
    path = tmp_path / "review.stl"
    settings = _write_assembly_stl(mixed, [mixed], path)
    assert settings == {"linear_deflection_mm": 0.1, "angular_deflection_rad": 0.3}
    content = path.read_bytes()
    (triangles,) = struct.unpack_from("<I", content, 80)
    assert triangles == 129 * 12 + 2
    assert len(content) == 84 + 50 * triangles
    bounds = import_stl(path).bounding_box()
    assert bounds.max.X == pytest.approx(256.5, abs=1e-5)
    assert bounds.max.Z == pytest.approx(9, abs=1e-5)


def test_reflected_assembly_retains_a_repeated_rotated_prototype(tmp_path):
    from build123d import Compound, Pos, Rot, Shape, export_step, import_step
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf

    from app.mixed_geometry import _reflect_assembly

    prototype = Box(2, 3, 5).wrapped
    copies = [
        Shape.cast(prototype.Moved(pose.wrapped))
        for pose in (Pos(10, 20, -7), Pos(-4, 11, 13) * Rot(30, 0, 90))
    ]
    native = Pos(17, -6, 4) * Compound([Compound(copies)])
    reflection = gp_Trsf()
    reflection.SetMirror(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 1, -1)))
    mirrored = _reflect_assembly(native.wrapped, reflection)
    path = tmp_path / "repeated.step"
    export_step(Compound(children=[mirrored]), path)
    assert path.read_bytes().count(b"MANIFOLD_SOLID_BREP(") == 1
    assert len(import_step(path).solids()) == 2
    left, right = mirrored.solids()
    assert left.wrapped.IsPartner(right.wrapped)
    for original, converted in zip(native.solids(), (left, right)):
        before, after = original.bounding_box(), converted.bounding_box()
        assert list(after.min) == pytest.approx([before.min.X, before.min.Z, before.min.Y])
        assert list(after.max) == pytest.approx([before.max.X, before.max.Z, before.max.Y])
