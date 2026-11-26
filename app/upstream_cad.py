"""Pinned Jubilee foundation and its coordinate mapping.

The upstream STEP is a rigid, unscaled reference assembly; it does not validate
the interfaces or operation of an AutoLab design. See the asset manifest.
"""

from __future__ import annotations

import math
from functools import lru_cache
from gzip import decompress
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

ASSET_ID = "jubilee-v2.2.2-c877bd2"
STEP_SHA256 = "533417712183775a544802ae29a6f94d50ef0839bc2f24f68bce7a048798afea"
STEP_PATH = Path(__file__).parent / "assets" / "jubilee-c877bd2.step.gz"

# STEP uses Y-up. App axes are X right, Y up, Z depth. Reversing depth
# makes the existing app-to-CAD mapping a proper rotation, not a mirror.
STEP_CENTER_X = 274.855945675
STEP_FLOOR_Y = 272.216535584
STEP_CENTER_Z = 425.743280356


def _bed_drop(value: float) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 150):
        raise ValueError("Jubilee bed drop must be between 0 and 150 mm.")
    return float(value)


def foundation_metadata(bed_drop_mm: float = 100.0) -> dict:
    """Return a fresh JSON-safe immutable-asset description in app millimetres."""
    drop = _bed_drop(bed_drop_mm)
    return {
        "id": ASSET_ID,
        "component_id": "foundation-jubilee",
        "schema_version": "1",
        "source_sha256": STEP_SHA256,
        "static_mesh_url": "/assets/jubilee-c877bd2-static.glb",
        "bed_mesh_url": "/assets/jubilee-c877bd2-bed.glb",
        "source_to_app": "(X-274.855945675, Y-272.216535584, 425.743280356-Z)",
        "bbox_mm": [483.143869005, 564.358391585, 612.687507964],
        "bed_drop_mm": drop,
        "bed_drop_bounds_mm": [0.0, 150.0],
        "deck_origin_app_mm": [-1.57118753, 372.86690226 - drop, -6.13189038],
        "workspace_size_mm": [300.0, 300.0],
        "workspace_scope": "Proposed conservative rectangle over the imported build-plate "
        "assembly bounding box; actual surface, mounting interfaces, physical travel and "
        "connections are unchecked.",
        "license": "CC-BY-4.0",
        "source_url": "https://github.com/machineagency/jubilee/blob/c877bd2e889560c83da2a68479641a4f7f0d9b83/frame/cads/STEP/jubilee.STEP",
        "preview_scope": "Simplified tessellation; upstream STEP is the geometric authority.",
    }


def original_step_bytes() -> bytes:
    """Read and validate the known asset; never accept caller-controlled paths."""
    raw = decompress(STEP_PATH.read_bytes())
    if sha256(raw).hexdigest() != STEP_SHA256:
        raise ValueError("Pinned Jubilee STEP hash mismatch.")
    return raw


@lru_cache(maxsize=1)
def _original_shape():
    from build123d import import_step

    with TemporaryDirectory() as temp:
        path = Path(temp) / "jubilee.step"
        path.write_bytes(original_step_bytes())
        return import_step(path)


def position_foundation(original, bed_drop_mm):
    """Place a fresh assembly tree while sharing the STEP's immutable part geometry."""
    from build123d import Compound, Pos, Rot, Shape

    def instance(node):
        if node.children:
            child = Compound(children=[instance(part) for part in node.children])
            child.locate(node.location)
        else:
            # Make an independent placement handle to the same geometric definition.
            child = Shape.cast(node.wrapped.Located(node.wrapped.Location()))
        child.label = node.label
        child.color = node.color
        return child

    beds = [part for part in original.children if part.label == "build_plate_assembly"]
    if len(beds) != 1:
        raise ValueError("Pinned Jubilee hierarchy must contain exactly one build plate assembly.")
    fixed = Compound(children=[instance(part) for part in original.children if part is not beds[0]])
    bed = instance(beds[0])
    transform = Pos(-STEP_CENTER_X, STEP_CENTER_Z, -STEP_FLOOR_Y) * Rot(90, 0, 0)
    fixed.move(transform)
    bed.move(Pos(0, 0, -bed_drop_mm) * transform)
    return Compound(children=[fixed, bed], label="Jubilee foundation")


def load_foundation_topology(bed_drop_mm):
    """Detach the positioned topology for a one-shot mixed CAD worker."""
    import gc

    topology = load_foundation_cad(bed_drop_mm).wrapped
    _original_shape.cache_clear()
    gc.collect()
    return topology


def load_foundation_cad(bed_drop_mm: float = 100.0):
    """Return STEP assembly in app's CAD coordinates, millimetres, Z-up.

    STEP (X,Y,Z) -> CAD (X-274.855945675, 425.743280356-Z,
    Y-272.216535584).  This is a rotation and translation, preserving geometry.
    The source shape is cached; caller receives a relocated copy.
    """
    drop = _bed_drop(bed_drop_mm)
    return position_foundation(_original_shape(), drop)
