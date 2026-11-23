"""Tests for Phase 4.4 non-destructive mating and AST-sandboxed parametric CAD adaptation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from build123d import Box, Cylinder, Pos, Unit, export_step, export_stl, import_step

from app.upstream_cad_adapt import (
    MatingAdaptationSpec,
    ParamOverride,
    adapt_upstream_mating,
    adapt_upstream_parametric,
)
from app.upstream_cad_ingest import UpstreamCadError, ingest_upstream_cad


def _step_fixture(tmp_path: Path) -> bytes:
    solid = (
        Box(70, 50, 20)
        - Pos(25, 16, 0) * Cylinder(1.7, 30)
        - Pos(-25, 16, 0) * Cylinder(1.7, 30)
    )
    out = tmp_path / "fixture.step"
    export_step(solid, out, unit=Unit.MM)
    return out.read_bytes()


def _stl_fixture(tmp_path: Path) -> bytes:
    out = tmp_path / "fixture.stl"
    export_stl(Box(60, 40, 25), out)
    return out.read_bytes()


@pytest.mark.parametrize(
    "kind,placement",
    [
        ("adapter_plate", "below"),
        ("riser", "below"),
        ("bracket", "rear"),
        ("enclosure", "surround"),
        ("cutout_mount", "below"),
    ],
)
def test_non_destructive_mating_kinds_on_brep_step_zero_interference(
    kind: str, placement: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    upstream = asyncio.run(
        ingest_upstream_cad(
            source_host="github",
            repo_full_name="openlab/pump",
            repo_url="https://github.com/openlab/pump",
            commit_sha="a" * 40,
            cad_file_path="cad/pump.step",
            spdx_hint="MIT",
            local_bytes=_step_fixture(tmp_path),
        )
    )
    spec = MatingAdaptationSpec(
        kind=kind,
        placement=placement,
        clearance_mm=0.4,
        secondary_hole_pitch_x_mm=75.0,
        secondary_hole_pitch_y_mm=50.0,
    )
    a1_mini = {"printer": {"name": "Bambu Lab A1 mini", "build_volume_mm": [180.0, 180.0, 180.0]}}
    adapted = asyncio.run(adapt_upstream_mating(upstream, spec, lab_profile=a1_mini))
    assert adapted["interference"]["passed"] is True
    assert adapted["interference"]["interference_volume_mm3"] == 0.0
    assert adapted["custom_geometry"]["is_valid"] is True
    assert adapted["custom_geometry"]["solid_count"] == 1
    assert adapted["printer_fit"]["fits"] is True

    c_step = Path(adapted["artifacts"]["custom_step_path"])
    a_step = Path(adapted["artifacts"]["assembly_step_path"])
    c_glb = Path(adapted["artifacts"]["custom_glb_path"])
    assert import_step(c_step).is_valid
    assert import_step(a_step).is_valid
    assert c_glb.read_bytes()[:4] == b"glTF"


def test_stl_mating_collision_detection_and_printer_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    upstream = asyncio.run(
        ingest_upstream_cad(
            source_host="github",
            repo_full_name="openlab/mesh-holder",
            repo_url="https://github.com/openlab/mesh-holder",
            commit_sha="b" * 40,
            cad_file_path="stl/holder.stl",
            spdx_hint="Apache-2.0",
            local_bytes=_stl_fixture(tmp_path),
        )
    )
    clean = asyncio.run(
        adapt_upstream_mating(upstream, {"kind": "adapter_plate", "clearance_mm": 0.5})
    )
    assert clean["interference"]["passed"] is True
    assert clean["interference"]["min_clearance_mm"] >= 0.49

    # Intentional penetration fails interference check cleanly.
    colliding = asyncio.run(
        adapt_upstream_mating(
            upstream,
            {"kind": "adapter_plate", "clearance_mm": 0.4},
            test_overlap_mm=3.0,
        )
    )
    assert colliding["interference"]["passed"] is False

    # Tiny printer triggers printer_fit overflow cleanly.
    tiny = {"printer": {"name": "Tiny SLA", "build_volume_mm": [70.0, 50.0, 50.0]}}
    over = asyncio.run(
        adapt_upstream_mating(
            upstream,
            {"kind": "adapter_plate", "flange_margin_mm": 20.0},
            lab_profile=tiny,
        )
    )
    assert over["printer_fit"]["fits"] is False


def test_parametric_build123d_adaptation_scad_diff_and_ast_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    commit = "c" * 40

    py_src = (
        "from build123d import Box, Cylinder, Pos\n"
        "plate_w = 80.0\n"
        "plate_d = 50.0\n"
        "plate_h = 6.0\n"
        "hole_r = 2.0\n"
        "result = Pos(0, 0, plate_h / 2) * Box(plate_w, plate_d, plate_h) "
        "- Cylinder(hole_r, plate_h * 2)\n"
    ).encode("utf-8")
    up_py = asyncio.run(
        ingest_upstream_cad(
            source_host="github",
            repo_full_name="openlab/param-py",
            repo_url="https://github.com/openlab/param-py",
            commit_sha=commit,
            cad_file_path="cad/plate.py",
            spdx_hint="MIT",
            local_bytes=py_src,
        )
    )
    adapted_py = asyncio.run(
        adapt_upstream_parametric(
            up_py,
            [ParamOverride(name="plate_w", value=110.0), ParamOverride(name="plate_h", value=10.0)],
        )
    )
    assert adapted_py["render_status"] == "rendered_brep"
    assert adapted_py["parameter_diffs"] == [
        {"name": "plate_w", "original": 80.0, "updated": 110.0},
        {"name": "plate_h", "original": 6.0, "updated": 10.0},
    ]
    assert adapted_py["custom_geometry"]["cad_size_mm"] == pytest.approx(
        [110.0, 50.0, 10.0], abs=0.1
    )
    assert Path(adapted_py["artifacts"]["custom_glb_path"]).read_bytes()[:4] == b"glTF"

    # Malicious script blocked at ingest AST gate
    for bad_code in (
        b"import os\nx = os.listdir('.')\n",
        b"import socket\ns = socket.socket()\n",
        b"open('/etc/passwd').read()\n",
        b"x = ().__class__.__base__.__subclasses__()\n",
    ):
        with pytest.raises((ValueError, UpstreamCadError)):
            asyncio.run(
                ingest_upstream_cad(
                    source_host="github",
                    repo_full_name="evil/repo",
                    repo_url="https://github.com/evil/repo",
                    commit_sha=commit,
                    cad_file_path="cad/bad.py",
                    spdx_hint="MIT",
                    local_bytes=bad_code,
                )
            )

    # OpenSCAD parameter substitution & diffing
    scad_src = b"wall_mm = 3.0;\nwidth_mm = 90;\ncube([width_mm, 40, wall_mm]);\n"
    up_scad = asyncio.run(
        ingest_upstream_cad(
            source_host="github",
            repo_full_name="openlab/scad-box",
            repo_url="https://github.com/openlab/scad-box",
            commit_sha=commit,
            cad_file_path="scad/box.scad",
            spdx_hint="CERN-OHL-P-2.0",
            local_bytes=scad_src,
        )
    )
    adapted_scad = asyncio.run(adapt_upstream_parametric(up_scad, {"width_mm": 125}))
    assert adapted_scad["parameter_diffs"] == [{"name": "width_mm", "original": 90, "updated": 125}]
    adapted_text = Path(adapted_scad["artifacts"]["adapted_source_path"]).read_text(
        encoding="utf-8"
    )
    assert "width_mm = 125;" in adapted_text
