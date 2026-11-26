import hashlib
import json
import struct
from pathlib import Path

import pytest

from app.upstream_cad import STEP_PATH, foundation_metadata, original_step_bytes

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web" / "public" / "assets"
MANIFEST = STEP_PATH.with_name("jubilee-c877bd2.provenance.json")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def test_positioned_instances_retain_source_topology_and_local_placements():
    from build123d import Box, Compound, Pos, Rot

    from app.upstream_cad import STEP_CENTER_X, STEP_CENTER_Z, STEP_FLOOR_Y, position_foundation

    fixed = Box(10, 20, 30)
    fixed.label = "fixed member"
    fixed.move(Pos(7, 3, 2))
    pin = Box(1, 2, 3)
    pin.move(Pos(2, 0, 0))
    bed = Compound(children=[pin], label="build_plate_assembly")
    bed.move(Pos(3, 4, 5) * Rot(0, 90, 0))
    original = Compound(children=[fixed, bed])
    transform = Pos(-STEP_CENTER_X, STEP_CENTER_Z, -STEP_FLOOR_Y) * Rot(90, 0, 0)

    for drop in (0, 100):
        placed = position_foundation(original, drop)
        frame, platform = placed.children
        assert frame.children[0].wrapped.IsPartner(fixed.wrapped)
        assert platform.children[0].wrapped.IsPartner(pin.wrapped)
        assert platform.label == bed.label
        assert frame.location * frame.children[0].location == transform * fixed.location
        assert platform.location == Pos(0, 0, -drop) * transform * bed.location
        assert platform.children[0].location == pin.location
    assert fixed.parent is original and bed.parent is original and pin.parent is bed


def _bounds(path):
    data = path.read_bytes()
    assert data[:4] == b"glTF"
    size = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20:20 + size])
    return gltf["accessors"][0]["min"], gltf["accessors"][0]["max"]


def test_pinned_source_and_previews_match_provenance():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert _digest(STEP_PATH.read_bytes()) == manifest["gzip_sha256"]
    assert _digest(original_step_bytes()) == manifest["original_step_sha256"]
    for name in ("static", "bed"):
        path = ASSETS / f"jubilee-c877bd2-{name}.glb"
        assert _digest(path.read_bytes()) == manifest[f"{name}_preview_glb_sha256"]


def test_bed_pose_is_bounded_and_maps_to_preview_coordinates():
    metadata = foundation_metadata(100)
    low, high = _bounds(ASSETS / "jubilee-c877bd2-bed.glb")
    assert high[1] - low[1] == pytest.approx(19, abs=0.001)
    assert metadata["deck_origin_app_mm"][1] == pytest.approx(
        high[1] - 272.216535584 - 100, abs=0.001
    )
    assert (foundation_metadata(0)["deck_origin_app_mm"][1]
            - metadata["deck_origin_app_mm"][1]) == 100
    for invalid in (-1, 151, float("nan"), True):
        with pytest.raises(ValueError):
            foundation_metadata(invalid)
