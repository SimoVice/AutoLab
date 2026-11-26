"""A split stem clamp with an installed, detailed catalog screw. Dimensions in mm."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BODY = "stem-clamp"
SCREW = "clamp-screw"
CATALOG = {
    "id": "bd-warehouse-iso4762",
    "title": "bd_warehouse 0.4.0 · ISO 4762 socket head cap screw",
    "url": "https://github.com/gumyr/bd_warehouse/blob/"
           "8e9be0645b53db3d7414f74828c2d67b0da21138/src/bd_warehouse/fastener.py",
    "publisher": "Roger Maitland", "license": "Apache-2.0",
    "source_kind": "parametric_catalog", "geometry_basis": "parametric_catalog",
    "wheel_sha256": "2bb830513dc53a5095904d645a92466e629a50bd5493783544bdf3a7d5efc339",
    "use": "Detailed right-hand SocketHeadCapScrew from the pinned catalog.",
    "limits": "Nominal library geometry. Supplier, tolerance class and price are unverified.",
}

# ISO 4762 head diameters from the pinned catalog's socket_head_cap_parameters.csv.
_HEADS = {(3, .5): 5.68, (4, .7): 7.22, (5, .8): 8.72, (6, 1): 10.22,
          (8, 1.25): 13.27, (10, 1.5): 16.27}
_LENGTH = 20.0
_HEIGHT = 18.0
_SLIT = 1.2
_RADIAL_RELIEF = .025
_CAD_LOCK = RLock()


class MountDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    stem_diameter_mm: float = Field(ge=3, le=50)
    thread_diameter_mm: float = Field(ge=3, le=10)
    thread_pitch_mm: float = Field(ge=.5, le=1.5)

    @model_validator(mode="after")
    def supported_fastener(self):
        if (self.thread_diameter_mm, self.thread_pitch_mm) not in _HEADS:
            raise ValueError("Select an available ISO 4762 coarse thread from M3 through M10.")
        return self


class MountSpec(MountDimensions):
    catalog_version: Literal["0.4.0"] = "0.4.0"

    @property
    def designation(self):
        return f"M{self.thread_diameter_mm:g}-{self.thread_pitch_mm:g}"

    @property
    def head_radius(self):
        return _HEADS[self.thread_diameter_mm, self.thread_pitch_mm] / 2

    @property
    def bore_radius(self):
        return (self.stem_diameter_mm + .2) / 2

    @property
    def shaft_x(self):
        # Reserve the same two ears and shaft datum for every supported catalog member.
        return -(self.bore_radius + 11)

    @property
    def depth(self):
        return max(22.0, self.stem_diameter_mm + 8)

    @property
    def left(self):
        return self.shaft_x - self.bore_radius - 4

    def components(self):
        right = 12.0
        return [
            dict(key=BODY, name="Split stem clamp", role="Support for the measured round stem.",
                 primitives=[dict(shape="box", size_mm=[right-self.left, self.depth, _HEIGHT],
                                  center_mm=[(right+self.left)/2, self.depth/2, 0],
                                  finish="polymer")], source_id=None,
                 assembly="Slide the stem through the bore before tightening the split ears.",
                 check=f"Check the {2*self.bore_radius:g} mm free bore and the tapped lower ear."),
            dict(key=SCREW, name=f"ISO 4762 {self.designation} × {_LENGTH:g} mm screw",
                 role="Catalog screw crossing the split and engaging the threaded lower ear.",
                 parent_key=BODY, placement_mm=[0, self.depth/2-self.head_radius, _HEIGHT/2],
                 primitives=[dict(shape="cylinder", axis="z", finish="steel",
                                  size_mm=[2*self.head_radius, 2*self.head_radius,
                                           _LENGTH+self.thread_diameter_mm],
                                  center_mm=[0, self.head_radius,
                                             (self.thread_diameter_mm-_LENGTH)/2])],
                 source_id=CATALOG["id"], assembly="Seat the head against the clearance ear.",
                 check="Check shank diameter and successive thread crests below the head."),
        ]

    def proposal(self):
        return dict(
            title="Threaded stem support", overview="Split clamp with an installed catalog screw.",
            threaded_mount=self.model_dump(), components=self.components(),
            steps=[dict(name="Install the stem and seat the screw",
                        instruction="Pass the stem through the free bore and engage the screw.",
                        component_keys=[BODY, SCREW], moving_key=None, translation_mm=[0, 0, 0])],
            software=dict(controller="Manual clamp", interfaces=[],
                          setup=["Install the clamp on the measured stem."],
                          checks=["Verify alignment and clamping force on the physical assembly."]),
            open_questions=["The support-to-base position and contact height require measurement."],
        )


@lru_cache(maxsize=6)
def _catalog_screw(diameter, pitch):
    from bd_warehouse.fastener import SocketHeadCapScrew

    source = SocketHeadCapScrew(f"M{diameter:g}-{pitch:g}", _LENGTH, "iso4762", simple=False)
    core, *thread = source.solids()
    merged = core.fuse(*thread)
    if len(merged.solids()) != 1 or not merged.is_valid:
        raise ValueError("The catalog screw did not produce one valid threaded solid.")
    return merged.solids()[0]


@lru_cache(maxsize=8)
def _local_solid(spec, key):
    from build123d import Matrix, Plane, Solid

    screw = _catalog_screw(spec.thread_diameter_mm, spec.thread_pitch_mm)
    if key == SCREW:
        return screw.translate((0, spec.head_radius, 0))
    if key != BODY:
        raise ValueError("Unknown threaded mount member.")
    stock = Solid.make_box(12 - spec.left, spec.depth, _HEIGHT).translate(
        (spec.left, 0, -_HEIGHT / 2))
    bore = Solid.make_cylinder(spec.bore_radius, spec.depth + 2,
                               Plane(origin=(spec.shaft_x, -1, 0), z_dir=(0, 1, 0)))
    slit = Solid.make_box(13 - spec.shaft_x, spec.depth + 2, _SLIT).translate(
        (spec.shaft_x, -1, -_SLIT / 2))
    clearance = Solid.make_cylinder(spec.thread_diameter_mm/2 + .3, _HEIGHT/2 + 1).translate(
        (0, spec.depth/2, 0))
    # Scale only XY about the screw axis. This preserves both pitch and installed
    # helical phase. The cutter only touches the lower ear below the opened slit.
    scale = 1 + 2 * _RADIAL_RELIEF / spec.thread_diameter_mm
    cutter = screw.transform_geometry(Matrix([[scale, 0, 0, 0], [0, scale, 0, 0],
                                               [0, 0, 1, 0], [0, 0, 0, 1]]))
    lower = Solid.make_box(24, 24, _HEIGHT/2 + 1).translate((-12, -12, -_HEIGHT-1))
    taps = cutter.intersect(lower).solids()
    if len(taps) != 1:
        raise ValueError("The lower ear needs one continuous thread cutter.")
    tap = taps[0].translate((0, spec.depth/2, _HEIGHT/2))
    body = stock.cut(bore, slit, clearance, tap)
    if len(body.solids()) != 1 or not body.is_valid:
        raise ValueError("The tapped clamp did not produce one valid solid.")
    return body.solids()[0]


def component_body(recipe, key):
    # Exporters can attach triangulation to OCCT topology shared by located copies.
    # Keep those mutations away from the source used to cut a later mating part.
    with _CAD_LOCK:
        return deepcopy(_local_solid(MountSpec.model_validate(recipe), key))


def build_mount(spec):
    with _CAD_LOCK:
        for key in (SCREW, BODY):
            _local_solid(spec, key)


@lru_cache(maxsize=6)
def _local_mesh(spec, key):
    vertices, faces = deepcopy(_local_solid(spec, key)).tessellate(0.015, 0.08)
    return [tuple(v) for v in vertices], faces


def component_mesh(recipe, key):
    with _CAD_LOCK:
        return _local_mesh(MountSpec.model_validate(recipe), key)
