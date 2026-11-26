"""Project-scoped physical instances and connections at the compiled rest pose."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from urllib.parse import quote

from app.bom_engine import build_complete_bom, purchasing_route
from app.geometry import geometry_for_component


def _digest(value):
    # Equivalent JSON numbers from old compilers describe the same millimetres.
    def normalize(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {key: normalize(v) for key, v in item.items()}
        if isinstance(item, list):
            return [normalize(v) for v in item]
        return item

    raw = json.dumps(normalize(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fields(record, *keys):
    return {key: deepcopy(record[key]) for key in keys if record.get(key) is not None}


def _key(*parts):
    return "/".join(quote(str(part), safe="") for part in parts)


class _Assembly:
    def __init__(self, project: dict):
        self.route = purchasing_route(project)
        self.scope = self.route
        if self.route == "workstation":
            design = project.get("design") or {}
            platform = design.get("foundation") or design.get("platform") or {}
            self.scope = _key(self.route, platform.get("id", "custom"))
        elif self.route == "upstream_project":
            source = project[self.route].get("provenance") or {}
            self.scope = _key(self.route, source.get("source_host", ""),
                              source.get("repo_full_name", ""), source.get("file_path", ""))
        self.instances = {}
        self.frames = {"project": {"unit": "mm", "up_axis": "Y"}}
        if self.route == "upstream_project":
            self.frames["normalized_cad"] = {
                "unit": "mm", "up_axis": "Z", "parent": "project",
                "rotation_deg_xyz": [-90, 0, 0], "translation_mm": [0, 0, 0],
            }
        self.interfaces = {}
        self.connections = {}
        self.evidence = {}
        self.requirements = {}

    def instance_id(self, native):
        return f"{self.scope}/part/{_key(native)}"

    def frame_id(self, native):
        return f"{self.instance_id(native)}/frame"

    def record(self, key, kind, details):
        ref = f"{self.scope}/evidence/{_key(key)}"
        self.evidence[ref] = {"id": ref, "kind": kind, "details": deepcopy(details)}
        return ref

    def part(self, key, label, origin, collection, shape, position, size, evidence,
             *, parent=None, local=None, anchor="component_center", geometry_frame=None,
             kind=None):
        if key in self.instances:
            raise ValueError(f"Repeated physical instance {key!r}.")
        placement = None if position is None else {
            "frame_id": self.frame_id(parent) if parent is not None else "project",
            "translation_mm": deepcopy(local if parent is not None else position),
            "world_translation_mm": deepcopy(position),
            "rotation_deg_xyz": [0, 0, 0],
            "anchor": anchor,
        }
        self.frames[self.frame_id(key)] = {
            "unit": "mm", "up_axis": "Y", "owner_id": self.instance_id(key),
            "parent": placement["frame_id"] if placement else None,
            **(_fields(placement, "rotation_deg_xyz", "translation_mm") if placement else {}),
        }
        self.instances[key] = {
            "id": self.instance_id(key), "native_id": key, "label": label, "origin": origin,
            "kind": kind or origin,
            "frame_id": self.frame_id(key),
            "geometry": {"collection": collection, "key": key,
                         "frame_id": geometry_frame or self.frame_id(key),
                         "sha256": _digest(shape) if shape is not None else None},
            "placement": placement, "size_mm": deepcopy(size), "evidence_ids": list(evidence),
        }

    def port(self, owner, key, kind, evidence, details=None):
        ref = f"{owner}/interface/{_key(key)}"
        port = {"id": ref, "owner_id": owner, "kind": kind,
                "details": deepcopy(details or {}), "evidence_ids": list(evidence)}
        if ref in self.interfaces and self.interfaces[ref] != port:
            raise ValueError(f"Conflicting physical interface {ref}.")
        self.interfaces[ref] = port
        return ref

    def connect(self, key, kind, left, right, evidence, details=None, *, ports=(None, None)):
        if left not in self.instances or right not in self.instances:
            raise ValueError("A physical connection refers to a missing instance.")
        a = self.port(self.instance_id(left), key + ":from", kind, evidence, ports[0])
        b = self.port(self.instance_id(right), key + ":to", kind, evidence, ports[1])
        ref = f"{self.scope}/connection/{_key(key)}"
        self.connections[ref] = {"id": ref, "kind": kind, "from_interface": a, "to_interface": b,
                                 "details": deepcopy(details or {}), "evidence_ids": list(evidence)}


def _concept(graph, design):
    for part in design.get("components", []):
        key = part["key"]
        proof = [graph.record("component:" + key, "compiled_concept", {"component_key": key})]
        if part.get("source"):
            proof.append(graph.record("candidate:" + key, "sourcing_candidate", part["source"]))
        graph.part(key, part["name"], "custom_concept", "concept_design.components",
                   part.get("primitives"), part.get("position_mm"), part.get("size_mm"), proof,
                   parent=part.get("parent_key"), local=part.get("placement_mm"),
                   anchor="component_local_origin")
    for part in design.get("components", []):
        if parent := part.get("parent_key"):
            key = part["key"]
            graph.connect("parent:" + key, "proposed_mount", parent, key,
                          graph.instances[key]["evidence_ids"][:1],
                          _fields(part, "placement_mm", "assembly", "check"))
    for interface in design.get("work_interfaces", []):
        key = "work:" + interface["key"]
        ref = graph.record(key, "compiled_work_interface", interface)
        limits = _fields(interface, "phase", "step_name", "min_distance_mm", "max_distance_mm")
        graph.connect(key, "working_axis", interface["tool_key"], interface["target_key"], [ref],
                      limits,
                      ports=({"frame_id": graph.frame_id(interface["tool_key"]),
                              **_fields(interface, "tool_primitive", "axis", "origin_mm")},
                             {"frame_id": graph.frame_id(interface["target_key"]),
                              **_fields(interface, "target_primitive")}))


def _fixture(graph, fixture):
    parts = {p["id"]: p for p in fixture.get("parts", [])}
    basis = graph.record("fixture", "fixture_inputs", fixture.get("spec") or {})
    reference = fixture.get("reference")
    ref = (graph.record("fixture-reference", "dimensional_reference", reference)
           if reference else None)
    for comp in fixture.get("components", []):
        key = comp["id"]
        if key == "reference-object":
            origin = "reference_envelope"
        else:
            origin = "custom_fixture"
        evidence = [basis, ref] if ref and key in ("reference-object", "locating-base") else [basis]
        graph.part(key, comp["label"], origin, "standalone_fixture.components",
                   [comp.get("geometry"), parts.get(key, {}).get("part_signature")],
                   comp.get("position"), comp.get("size"),
                   evidence, kind=comp.get("kind"))
    for key, part in parts.items():
        if key not in graph.instances:
            graph.part(key, part["label"], "separately_fabricated", "standalone_fixture.parts",
                       part.get("part_signature"), None, part.get("size_mm"), [basis])
    derived = fixture.get("derived") or {}
    for hole in derived.get("mounting_holes", []):
        graph.port(graph.instance_id("locating-base"), hole["role"], "mounting_hole", [basis],
                   {"frame_id": graph.frame_id("locating-base"), **hole})
    if "camera-arm" in graph.instances and derived.get("camera_cradle"):
        graph.port(graph.instance_id("camera-arm"), "camera-cradle", "camera_mount", [basis],
                   derived["camera_cradle"])
    for base, child in (("locating-base", "camera-mast"), ("camera-mast", "camera-arm"),
                        ("locating-base", "reference-object")):
        if base in graph.instances and child in graph.instances:
            kind = "locating_pocket" if child == "reference-object" else "proposed_mount"
            graph.connect("fixture:" + child, kind, base, child, [basis])


def _cad_interfaces(graph, key, geometry, frame_id, evidence):
    for collection, kind in (("hole_features", "cylindrical_feature"),
                             ("mounting_planes", "planar_face")):
        for feature in geometry.get(collection, []):
            graph.port(graph.instance_id(key), kind + ":" + _digest(feature), kind, evidence,
                       {"frame_id": frame_id, **feature})


def _upstream(graph, source):
    provenance = source.get("provenance") or {}
    geom = source.get("geometry") or {}
    evidence = [graph.record("upstream-file", "source_manifest",
                             _fields(provenance, "source_host", "repo_full_name", "repo_url",
                                     "commit_sha", "file_path", "raw_url", "file_sha256",
                                     "spdx_id", "license_url", "authors"))]
    evidence.append(graph.record("normalization", "source_coordinate_frame",
                                 _fields(geom, "cad_kind", "detected_units",
                                         "normalization_transform", "cad_min_mm", "cad_max_mm")))
    imported = "upstream-source"
    rendered = geom.get("cad_kind") in ("step_brep", "binary_stl", "ascii_stl")
    signature = [provenance.get("file_sha256"),
                 _fields(geom, "normalization_transform", "cad_min_mm", "cad_max_mm",
                         "solid_count", "volume_mm3", "hole_features", "mounting_planes")]
    graph.part(imported, source.get("title") or provenance.get("file_path") or "Imported source",
               "imported_cad" if rendered else "upstream_reference", "upstream_project.geometry",
               signature if rendered else None, [0, 0, 0] if rendered else None,
               geom.get("app_size_mm") if rendered else None, evidence,
               anchor="normalized_origin", geometry_frame="normalized_cad")
    _cad_interfaces(graph, imported, geom, "normalized_cad", evidence)
    adapt = source.get("adaptation") or {}
    if not adapt.get("part_id"):
        return
    part_id = adapt["part_id"]
    custom = adapt.get("custom_geometry") or {}
    spec = adapt.get("spec") or {}
    ref = graph.record("adaptation:" + part_id, "adaptation_recipe",
                       _fields(adapt, "mode", "spec", "parameter_diffs", "render_status"))
    graph.part(part_id, part_id, "source_adaptation", "upstream_project.adaptation",
               [signature, spec, custom] if custom else None,
               [0, 0, 0] if custom else None, custom.get("app_size_mm"), [*evidence, ref],
               anchor="normalized_origin", geometry_frame="normalized_cad")
    if adapt.get("mode") == "mating" and custom:
        graph.connect("mating:" + part_id, "modeled_mating", imported, part_id, [ref], spec)


def _workstation(graph, project):
    design = project.get("design") or {}
    foundation = design.get("foundation") or {}
    platform = design.get("platform") or {}
    ref = graph.record("layout", "workstation_layout",
                       _fields(project.get("engineering_design") or {}, "schema_version"))
    asset = None
    if foundation:
        asset = graph.record("foundation", "pinned_cad",
                             _fields(foundation, "id", "source_url", "source_sha256", "license",
                                     "source_to_app", "deck_origin_app_mm", "bed_drop_mm"))
    if platform:
        asset = graph.record("platform", "commercial_reference",
                             _fields(platform, "id", "source_url", "geometry_scope",
                                     "deck_definition_url", "deck_definition_sha256",
                                     "deck_definition_revision", "deck_definition_license"))
    modules = {module["target_component_id"]: module
               for name in ("labware_rack", "labware_plate", "tip_rack")
               if (module := project.get(name)) and module.get("target_component_id")}
    for comp in design.get("components", []):
        key = comp["id"]
        geometry = comp.get("geometry")
        geometry_frame = None
        origin, proof = "workstation_model", [ref]
        if foundation and key == foundation.get("component_id"):
            origin, proof = "imported_foundation", [asset]
            geometry = _fields(foundation, "source_sha256", "source_to_app", "bed_drop_mm")
            # The pinned transform already locates the source in project coordinates.
            geometry_frame = "project"
        else:
            if geometry is None and comp.get("kind") and comp.get("size"):
                geometry = geometry_for_component(comp)
            if platform:
                origin, proof = "schematic_envelope", [ref, asset]
        if reference := comp.get("reference_geometry"):
            proof.append(graph.record("reference:" + key, "dimensional_reference", reference))
        if module := modules.get(key):
            origin = "workstation_labware"
            proof.append(graph.record("labware:" + key, "labware_specification",
                                      _fields(module, "kind", "profile_id", "spec", "reference",
                                              "provenance")))
            geometry = [geometry, _fields(module, "kind", "spec", "geometry")]
        graph.part(key, comp.get("label", key), origin, "design.components",
                   [geometry, comp.get("size"), comp.get("fabrication_shape")],
                   comp.get("position"), comp.get("size"), proof, kind=comp.get("kind"),
                   geometry_frame=geometry_frame)
    for comp in design.get("components", []):
        if comp.get("slot_id") and "base" in graph.instances:
            graph.connect("deck-slot:" + str(comp["slot_id"]), "deck_slot", "base", comp["id"],
                          [ref, asset] if asset else [ref], {"slot_id": comp["slot_id"]})
    layout = (project.get("engineering_design") or {}).get("proposal", {})
    for record in layout.get("placements", []):
        key = record.get("component_id")
        if key in graph.instances and key != "base" and "base" in graph.instances:
            graph.connect("deck-placement:" + key, "proposed_layout", "base", key, [ref],
                          _fields(record, "x_mm", "z_mm"))


def _geometry(project):
    graph = _Assembly(project)
    if graph.route == "concept_design":
        _concept(graph, project[graph.route])
    elif graph.route == "standalone_fixture":
        _fixture(graph, project[graph.route])
    elif graph.route == "upstream_project":
        _upstream(graph, project[graph.route])
    elif graph.route == "workstation":
        _workstation(graph, project)
    elif graph.route == "bench_station":
        for module in project[graph.route].get("modules", []):
            key = module["id"]
            proof = graph.record("station:" + key, "planned_work_surface",
                                 _fields(module, "functional_surface_mm"))
            graph.part(key, module["label"], "planning_envelope", "bench_station.modules",
                       module.get("size_mm"), module.get("position_mm"), module.get("size_mm"),
                       [proof])
    if records := project.get("cad_instances"):
        from app.cad_placement import append_instances
        append_instances(graph, records)
    return graph


def _geometry_signature(graph):
    return _digest({
        "frames": graph.frames,
        "instances": {part["id"]: [part["geometry"], part["placement"], part["size_mm"]]
                      for part in graph.instances.values()},
    })


def component_instance_ids(project: dict) -> dict[str, str]:
    """Resolve the compiler's component names to their placed assembly instances."""
    return {key: part["id"] for key, part in _geometry(project).instances.items()}


def assembly_binding(project: dict) -> dict:
    """Only the version and geometric fingerprint need space in a portable input file."""
    return {"version": 1, "geometry_sha256": _geometry_signature(_geometry(project))}


def _purchasing(graph, project):
    # Purchasing identities must be reproducible offline in another session or lab.
    bom = build_complete_bom(project, lab_profile={}, use_cached_quotes=False)
    for row in bom["lines"]:
        key = row["line_id"]
        owner = f"{graph.scope}/requirement/{_key(key)}"
        choice = row.get("part_choice") or {}
        accepted = choice.get("accepted") or {}
        proof = [graph.record("requirement:" + key, "purchasing_requirement",
                              _fields(row, "line_id", "kind", "source_url", "requirement",
                                      "scope_selection", "catalog_id", "supplier", "mpn", "sku"))]
        if choice:
            proof.append(graph.record("choice:" + key, "part_choice", choice))
        instance_ids = [graph.instance_id(cid) for cid in row["component_ids"]
                        if cid in graph.instances]
        # The fixture compiler models the cradle within the arm rather than as a separate solid.
        if graph.route == "standalone_fixture" and "camera-cradle" in row["component_ids"]:
            if "camera-arm" in graph.instances:
                instance_ids.append(graph.instance_id("camera-arm"))
        record = {"id": owner, "bom_line_id": key, "instance_ids": instance_ids,
                  "evidence_ids": proof, **_fields(row, "label", "quantity", "unit", "catalog_id",
                                                   "group_id", "owned_quantity", "requirement",
                                                   "scope_selection", "resolution_status")}
        if choice.get("required_specs"):
            record["required_specs"] = deepcopy(choice["required_specs"])
        if accepted:
            record["selected_part"] = _fields(accepted, "issuer", "part_number", "catalog_id",
                                              "source_url", "unit", "facts", "interface_status")
        graph.requirements[owner] = record
        specs = row.get("specs") or {}
        electrical = _fields(specs, "interface", "supply_voltage_v", "coil_voltage_v",
                             "rated_current_a", "usb_ports_required", "csi_ports_required")
        if electrical:
            graph.port(owner, "electrical", "electrical_specification", proof, electrical)
        mechanical = _fields(specs, "mounting_pitch_mm", "shaft_diameter_mm", "fastener")
        if mechanical:
            graph.port(owner, "mount", "mounting_specification", proof, mechanical)


def physical_assembly(project: dict) -> dict:
    """Resolve geometry, interfaces, purchasing links and evidence in one project namespace."""
    graph = _geometry(project)
    _purchasing(graph, project)
    result = {"schema_version": 1, "project_id": project.get("lineage_id") or project["id"],
            "revision": project["revision"], "route": graph.route, "frames": graph.frames,
            "geometry_sha256": _geometry_signature(graph),
            "instances": list(graph.instances.values()),
            "interfaces": list(graph.interfaces.values()),
            "connections": list(graph.connections.values()),
            "requirements": list(graph.requirements.values()),
            "evidence": list(graph.evidence.values())}
    if records := project.get("cad_instances"):
        result["cad_exports"] = {
            "host_geometry_scope": "Dedicated host exports describe the native host design.",
            "mixed_geometry_scope": "Handoff STEP and STL contain every placed instance in "
                                    "the project rest frame (millimetres, Y up).",
            "instances": {
                name: {
                    **_fields(item, "provenance", "source_units", "source_up_axis", "placement",
                              "artifact_hashes"),
                    "adapter_spec": (item.get("adaptation") or {}).get("spec"),
                }
                for name, item in sorted(records.items())
            },
        }
    return result


def assembly_json(project: dict) -> bytes:
    return json.dumps(physical_assembly(project), indent=2, sort_keys=True, ensure_ascii=False,
                      allow_nan=False).encode("utf-8")
