"""Rest-pose geometry and conservative access checks for externally sourced parts."""

from __future__ import annotations

from itertools import product
from math import isfinite

from app.concept_design import _mat_mul_vec, _rotation_matrix
from app.upstream_cad_adapt import check_custom_part_printer_fit

IDENTITY = ((1, 0, 0), (0, 1, 0), (0, 0, 1))


def _cad_bounds(geom):
    low, high = geom["cad_min_mm"], geom["cad_max_mm"]
    if not all(isfinite(v) for v in (*low, *high)) or not all(a <= b for a, b in zip(low, high)):
        raise ValueError("Invalid normalized source bounds.")
    return ([low[0], low[2], -high[1]], [high[0], high[2], -low[1]])


def _translate(point, position, matrix):
    return [a + b for a, b in zip(position, _mat_mul_vec(matrix, point))]


def _world_bounds(bounds, position, matrix):
    points = [_translate(p, position, matrix) for p in product(*zip(*bounds))]
    return [[round(fn(axis), 6) for axis in zip(*points)] for fn in (min, max)]


def _overlap(a, b):
    return all(min(x, y) - max(i, j) > 1e-5 for i, x, j, y in zip(*a, *b))


def _parent_key(graph, parent):
    if parent == "project":
        return None
    for key, part in graph.instances.items():
        if parent in (part["id"], key) and part["placement"]:
            return key
    raise ValueError(f"Missing or unplaced CAD parent frame '{parent}'.")


def _append_one(graph, name, source):
    pose, geom, prov = source["placement"], source["geometry"], source["provenance"]
    parent = _parent_key(graph, pose["parent"])
    parent_pose = graph.instances[parent]["placement"] if parent else {}
    parent_matrix = parent_pose.get("world_rotation_matrix", IDENTITY)
    position = _translate(
        pose["translation_mm"], parent_pose.get("world_translation_mm", [0, 0, 0]), parent_matrix
    )
    local = _rotation_matrix(pose["rotation_deg_xyz"]) or IDENTITY
    matrix = [list(row) for row in zip(*[_mat_mul_vec(parent_matrix, col) for col in zip(*local)])]
    key = "cad:" + name
    cad_frame = graph.frame_id(key) + "/normalized-cad"
    graph.frames[cad_frame] = {
        "unit": "mm",
        "up_axis": "Z",
        "parent": graph.frame_id(key),
        "rotation_deg_xyz": [-90, 0, 0],
        "translation_mm": [0, 0, 0],
    }
    proof = graph.record(
        key,
        "pinned_cad_instance",
        {
            **prov,
            "source_units": source["source_units"],
            "source_up_axis": source["source_up_axis"],
            "normalization": geom["normalization_transform"],
            "detected_units": geom["detected_units"],
        },
    )
    datum = graph.record(key + ":datum", "measured_placement", pose)
    shape = [
        prov["source_host"],
        prov["repo_full_name"],
        prov["commit_sha"],
        prov["file_path"],
        prov["file_sha256"],
        geom["normalization_transform"],
        geom["cad_min_mm"],
        geom["cad_max_mm"],
    ]
    graph.part(
        key,
        name,
        "imported_cad",
        "cad_instances",
        shape,
        position,
        geom["app_size_mm"],
        [proof, datum],
        parent=parent,
        local=pose["translation_mm"],
        geometry_frame=cad_frame,
        anchor="normalized_origin",
    )
    from app.project_assembly import _cad_interfaces

    _cad_interfaces(graph, key, geom, cad_frame, [proof])
    part = graph.instances[key]
    part["placement"].update(
        rotation_deg_xyz=pose["rotation_deg_xyz"], world_rotation_matrix=matrix
    )
    graph.frames[part["frame_id"]]["rotation_deg_xyz"] = pose["rotation_deg_xyz"]
    part["bounds_mm"] = _world_bounds(_cad_bounds(geom), position, matrix)
    if parent:
        graph.connect(key + ":placement", "measured_mount", parent, key, [datum], pose)
    if access := pose.get("access"):
        low, high = _cad_bounds(geom)
        sign, axis = access["face"]
        i = "xyz".index(axis)
        if sign == "+":
            low[i], high[i] = high[i], high[i] + access["distance_mm"]
        else:
            low[i], high[i] = low[i] - access["distance_mm"], low[i]
        part["access_bounds_mm"] = _world_bounds((low, high), position, matrix)
    if adapt := source.get("adaptation"):
        custom = adapt["custom_geometry"]
        adapter = key + ":adapter"
        evidence = graph.record(
            adapter,
            "adaptation_recipe",
            {"spec": adapt["spec"], "interference": adapt["interference"]},
        )
        graph.part(
            adapter,
            adapt["part_id"],
            "source_adaptation",
            "cad_instances",
            [shape, adapt["spec"], custom],
            position,
            custom["app_size_mm"],
            [proof, evidence],
            parent=key,
            local=[0, 0, 0],
            geometry_frame=cad_frame,
            anchor="normalized_origin",
        )
        graph.instances[adapter]["placement"]["world_rotation_matrix"] = matrix
        graph.instances[adapter]["bounds_mm"] = _world_bounds(_cad_bounds(custom), position, matrix)
        graph.connect(adapter, "modeled_mating", key, adapter, [evidence], adapt["spec"])


def append_instances(graph, records):
    pending = dict(sorted(records.items()))
    while pending:
        previous = len(pending)
        available = {"project", *graph.instances, *(p["id"] for p in graph.instances.values())}
        for name, source in list(pending.items()):
            if source["name"] != name:
                raise ValueError("CAD instance keys must match their saved names.")
            if source["placement"]["parent"] in available:
                _append_one(graph, name, source)
                del pending[name]
        if len(pending) == previous:
            raise ValueError("A named CAD instance has a missing or cyclic parent frame.")


def _native_bounds(project, graph):
    concept = {
        p["key"]: p["bounds_mm"]
        for p in (project.get("concept_design") or {}).get("components", [])
    }
    standalone = project.get("upstream_project") or {}
    adapted = standalone.get("adaptation") or {}
    upstream = {
        "upstream-source": standalone.get("geometry"),
        adapted.get("part_id"): adapted.get("custom_geometry"),
    }
    for key, p in graph.instances.items():
        if bounds := p.get("bounds_mm"):
            yield key, bounds
        elif p["placement"] and p["size_mm"]:
            if key in concept:
                local = (concept[key]["min"], concept[key]["max"])
            elif upstream.get(key):
                local = _cad_bounds(upstream[key])
            else:
                local = [[sign * v / 2 for v in p["size_mm"]] for sign in (-1, 1)]
            yield key, _world_bounds(local, p["placement"]["world_translation_mm"], IDENTITY)


def check_assembly(project, graph, lab_profile):
    bounds = dict(_native_bounds(project, graph))
    overlaps, entries, prints = [], [], []
    checked = set()
    for name, source in sorted(project["cad_instances"].items()):
        key = "cad:" + name
        instance = graph.instances[key]
        adapter = key + ":adapter"
        for member in (key, adapter):
            if member not in bounds:
                continue
            for other, box in bounds.items():
                if other in (key, adapter):
                    continue
                pair = tuple(sorted((member, other)))
                if pair not in checked and _overlap(bounds[member], box):
                    overlaps.append(list(pair))
                checked.add(pair)
        corridor = instance.get("access_bounds_mm")
        blocked = [k for k, b in bounds.items() if k != key and corridor and _overlap(corridor, b)]
        adapt = source.get("adaptation") or {}
        printer = check_custom_part_printer_fit(
            (adapt.get("custom_geometry") or {}).get("app_size_mm"),
            adapter,
            f"{name} mating part",
            lab_profile,
        )
        if printer:
            prints.append(printer)
        host = key
        while graph.instances[host]["geometry"]["collection"] == "cad_instances":
            parent_frame = graph.instances[host]["placement"]["frame_id"]
            if parent_frame == "project":
                host = "project"
                break
            host = next(k for k, p in graph.instances.items() if p["frame_id"] == parent_frame)
        entries.append(
            {
                "host": host,
                "name": name,
                "world_translation_mm": instance["placement"]["world_translation_mm"],
                "world_rotation_matrix": instance["placement"]["world_rotation_matrix"],
                "bounds_mm": bounds[key],
                "access_status": ("review" if blocked else "clear")
                if corridor
                else "not_specified",
                "access_blockers": blocked,
                "mating_interference": adapt.get("interference"),
                "printer_fit": printer,
            }
        )
    combined = [
        [fn(b[end][i] for b in bounds.values()) for i in range(3)]
        for fn, end in ((min, 0), (max, 1))
    ]
    return {
        "method": "rest_pose_axis_aligned_envelopes",
        "bounds_mm": combined,
        "possible_overlaps": sorted(overlaps),
        "unplaced_parts": sorted(set(graph.instances) - bounds.keys()),
        "instances": entries,
    }, prints
