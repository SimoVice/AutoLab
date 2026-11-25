"""Build the issued mixed assembly in the scene's millimetre, Y-up rest frame."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from app.cad_instances import instance_file
from app.project_assembly import _geometry


def _source_shape(item, role, path):
    from build123d import Rot, Shape, import_step
    from OCP.StlAPI import StlAPI_Reader
    from OCP.TopoDS import TopoDS_Shape

    if path is None or not path.is_file():
        raise ValueError("An inspected CAD artifact is missing.")
    if role != "original":
        return Rot(-90, 0, 0) * import_step(path), "editable_step"
    from app.upstream_cad_worker import _analyze_stl_file, _normalize_shape

    if _analyze_stl_file(path)["triangle_count"] > 75_000:
        raise ValueError("Faceted STEP export supports up to 75,000 triangles per source.")
    triangles = TopoDS_Shape()
    if not StlAPI_Reader().Read(triangles, str(path)) or triangles.IsNull():
        raise ValueError("The pinned STL has no usable triangle faces.")
    shape, _ = _normalize_shape(
        Shape.cast(triangles),
        source_units=item["source_units"],
        source_up_axis=item["source_up_axis"],
    )
    return Rot(-90, 0, 0) * shape, "faceted_stl"


def _located(shape, pose):
    from build123d import Location
    from OCP.gp import gp_Trsf

    rotation = pose["world_rotation_matrix"]
    position = pose["world_translation_mm"]
    transform = gp_Trsf()
    transform.SetValues(*(v for row, offset in zip(rotation, position) for v in (*row, offset)))
    return Location(transform) * shape


def _native_parts(project, graph):
    from build123d import Box, Compound, Plane, Pos, Rot

    def from_workstation_axes(shape):
        # Native CAD uses (scene x, scene z, scene y). Mirror the actual compound
        # topology. Its assembly children still describe the original coordinate frame.
        topology = Compound.cast(shape.mirror(Plane.XZ).wrapped)
        return Rot(-90, 0, 0) * topology

    native = {k: p for k, p in graph.instances.items() if not k.startswith("cad:")}
    if graph.route == "concept_design":
        from app.concept_cad import _component_body

        for comp in project["concept_design"]["components"]:
            yield comp["key"], _component_body(comp, True), "compiled_concept"
    elif graph.route == "workstation":
        from app.cad_exports import _build_solid, _components
        from app.upstream_cad import load_foundation_cad

        for comp in _components(project):
            imported = comp["kind"] == "upstream-cad"
            shape = load_foundation_cad(comp["bed_drop_mm"]) if imported else _build_solid(comp)
            yield comp["id"], from_workstation_axes(shape), native[comp["id"]]["origin"]
    elif graph.route == "upstream_project":
        from app.mixed_sources import legacy_files

        source = project["upstream_project"]
        paths = legacy_files(source)
        needed = "normalized" if "normalized" in paths else "original"
        geom = source["geometry"]["normalization_transform"]
        item = {
            "source_units": geom["source_units_applied"],
            "source_up_axis": geom["source_up_axis"],
        }
        yield "upstream-source", *_source_shape(item, needed, paths[needed])
        if adapter := source.get("adaptation"):
            yield adapter["part_id"], *_source_shape(item, "adapter-step", paths["adapter-step"])
    else:
        built = {}
        if graph.route == "standalone_fixture":
            from app.fixture_cad import _world_positioned_assembly

            solids = _world_positioned_assembly(project["standalone_fixture"]).children
            built = {
                key: from_workstation_axes(s)
                for key, s in zip(("locating-base", "camera-mast", "camera-arm"), solids)
            }
        for key, part in native.items():
            if key in built:
                yield key, built[key], "fixture_brep"
            elif part["placement"] and part["origin"] in (
                "reference_envelope",
                "planning_envelope",
            ):
                yield (
                    key,
                    Pos(*part["placement"]["world_translation_mm"]) * Box(*part["size_mm"]),
                    part["origin"],
                )
            elif part["placement"]:
                raise ValueError(f"No solid compiler for placed part {key}.")


def build_mixed_cad(project):
    from build123d import Compound, Unit, export_step, export_stl

    graph = _geometry(project)
    bodies, entries = [], []

    def add(key, shape, representation):
        part = graph.instances[key]
        shape.label = part["id"]
        bodies.append(shape)
        bounds = shape.bounding_box()
        entries.append(
            {
                "instance_id": part["id"],
                "representation": representation,
                "bounds_mm": [
                    [round(float(v), 4) for v in xyz] for xyz in (bounds.min, bounds.max)
                ],
            }
        )

    for key, shape, representation in _native_parts(project, graph):
        add(key, shape, representation)
    for name, item in sorted(project["cad_instances"].items()):
        key = "cad:" + name
        role = "normalized" if "normalized" in item["artifact_hashes"] else "original"
        sources = [(key, role)]
        if item.get("adaptation"):
            sources.append((key + ":adapter", "adapter-step"))
        for part_key, role in sources:
            shape, kind = _source_shape(item, role, instance_file(item, role))
            add(part_key, _located(shape, graph.instances[part_key]["placement"]), kind)
    placed = {part["id"] for part in graph.instances.values() if part["placement"] is not None}
    if {entry["instance_id"] for entry in entries} != placed:
        raise ValueError("A placed instance is missing from the mixed assembly.")
    assembly = Compound(children=bodies, label="AutoLab mixed assembly")
    with TemporaryDirectory(prefix="autolab-mixed-") as tmp:
        step, stl = (Path(tmp) / f"assembly.{ext}" for ext in ("step", "stl"))
        if not export_step(assembly, step, unit=Unit.MM) or not export_stl(assembly, stl):
            raise ValueError("The mixed assembly could not be written.")
        return {
            "cad/assembly.step": step.read_bytes(),
            "cad/assembly.stl": stl.read_bytes(),
            "cad/geometry.json": json.dumps(
                {
                    "unit": "mm",
                    "up_axis": "Y",
                    "parts": entries,
                    "unplaced_instance_ids": sorted(
                        p["id"] for p in graph.instances.values() if not p["placement"]
                    ),
                },
                indent=2,
            ).encode(),
        }
