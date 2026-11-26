import asyncio
import struct
from copy import deepcopy

import pytest

from app.agent_tools import ToolContext, execute_tool
from app.concept_cad import concept_step
from app.concept_design import compile_concept, concept_stl
from app.domain import new_project
from app.project_storage import make_snapshot, restore_snapshot
from app.threaded_mount import BODY, CATALOG, SCREW, MountSpec


def install(project, message, diameter, pitch):
    ctx = ToolContext(project, message)
    result = asyncio.run(execute_tool("configure_threaded_mount", {
        "stem_diameter_mm": 9, "thread_diameter_mm": diameter, "thread_pitch_mm": pitch,
    }, ctx))
    return ctx.project, result


def test_mount_changes_require_requested_dimensions_and_preserve_catalog_binding(monkeypatch):
    monkeypatch.setattr("app.threaded_mount.build_mount", lambda _: None)
    first, result = install(new_project(), "Fit an M5 × 0.8 screw to the 9 mm shaft.", 5, .8)
    assert result.ok and result.mutated
    reopened, _ = restore_snapshot(make_snapshot(first, []))
    for message, size, pitch in [("Do not install M6 x 1.", 6, 1), ("Fit M4 x 0.7.", 6, 1)]:
        original, result = install(reopened, message, size, pitch)
        assert not result.ok and original == reopened
    changed, result = install(reopened, "Passa alla M6 × 1,0.", 6, 1)
    assert result.ok and changed["revision"] == first["revision"] + 1
    same, result = install(changed, "Install M6 x 1.", 6, 1)
    assert result.ok and same["revision"] == changed["revision"]
    restored, _ = restore_snapshot(make_snapshot(changed, []))
    concept = restored["concept_design"]
    assert concept["proposal"]["threaded_mount"]["stem_diameter_mm"] == 9
    assert concept["components"][1]["source"] == CATALOG
    spoof = deepcopy(concept["proposal"])
    spoof["components"][1]["placement_mm"][0] += 1
    with pytest.raises(ValueError, match="share a mounting spec"):
        compile_concept(spoof)


@pytest.mark.parametrize("diameter,pitch", [(6, 1), (8, 1.25)])
def test_exports_contain_installed_threaded_solids(tmp_path, diameter, pitch):
    pytest.importorskip("bd_warehouse")
    from build123d import Vector, import_step

    spec = MountSpec(stem_diameter_mm=9, thread_diameter_mm=diameter, thread_pitch_mm=pitch)
    design = compile_concept(spec.proposal())
    path = tmp_path / "assembly.step"
    path.write_bytes(concept_step(design))
    assembly = import_step(path)
    support, screw = sorted(assembly.solids(), key=lambda s: -s.volume)
    assert support.is_valid and screw.is_valid
    for key in [None, SCREW, BODY]:
        data = concept_stl(design, key)
        count = struct.unpack_from("<I", data, 80)[0]
        assert count > 1000 and len(data) == 84 + 50 * count
    screw_bounds = screw.bounding_box()
    x, y = screw_bounds.center().X, screw_bounds.center().Y
    bottom = support.bounding_box().min.Z
    # Probe both sides of the engaged interface over several turns at a fixed radius.
    # The solid/void transition must repeat once per requested pitch in both members.
    for solid in (screw, support):
        occupancy = [
            solid.is_inside(Vector(x + diameter/2 - .23*pitch, y, bottom + .6 + i*pitch/8))
            for i in range(33)
        ]
        assert any(occupancy) and not all(occupancy)
        assert occupancy[8:] == occupancy[:-8]
