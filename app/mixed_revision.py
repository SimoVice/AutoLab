"""Revision-bound fabrication changes, installation instructions and interface assignments."""

import json

from app.assembly import assembly_steps
from app.concept_guide import concept_gateway_config


def change_impact(project, assembly):
    change = project.get("mating_change")
    if not change:
        return None
    parts = {p["native_id"]: p for p in assembly["instances"]}
    key = "cad:" + change["instance"]
    source, adapter = parts[key], parts[key + ":adapter"]
    old, new = change["previous_spec"] or {}, change["current_spec"]
    affected_frames = {source["frame_id"], adapter["frame_id"]}
    affected = {source["id"]}
    # A child may itself carry another mounted device.
    for part in assembly["instances"]:
        place = part["placement"]
        if place and place["frame_id"] in affected_frames and part["id"] != adapter["id"]:
            affected.add(part["id"])
            affected_frames.add(part["frame_id"])
    fit = project.get("design", {}).get("spatial_fit", {}).get("cad_assembly", {})
    overlaps = [
        [parts[left]["id"], parts[right]["id"]]
        for left, right in fit.get("possible_overlaps", [])
        if key + ":adapter" in (left, right)
    ]
    return {
        **change,
        "rest_pose_overlap_review": overlaps,
        "previous_revision": change["revision"] - 1,
        "dimensions": {
            k: {"before": old.get(k), "after": v} for k, v in new.items() if old.get(k) != v
        },
        "reprint": [adapter["id"]],
        "recheck_mount_datums": sorted(affected),
        "affected_bom_lines": [
            r["bom_line_id"] for r in assembly["requirements"] if adapter["id"] in r["instance_ids"]
        ],
    }


def device_bindings(project, assembly, impact):
    ports = {p["id"]: p for p in assembly["interfaces"]}
    assignments = []
    impacted = set(impact["recheck_mount_datums"] + impact["reprint"]) if impact else set()
    for connection in assembly["connections"]:
        endpoints = [ports[connection[k]] for k in ("from_interface", "to_interface")]
        owners = [p["owner_id"] for p in endpoints]
        assignments.append(
            {
                "connection_id": connection["id"],
                "kind": connection["kind"],
                "interfaces": [p["id"] for p in endpoints],
                "owners": owners,
                "assignment": connection["details"],
                "evidence": connection["evidence_ids"],
                "check_mount_datum": bool(impacted.intersection(owners)),
            }
        )
    concept = project.get("concept_design") or {}
    result = {
        "project_id": assembly["project_id"],
        "revision": assembly["revision"],
        "geometry_sha256": assembly["geometry_sha256"],
        "connections": assignments,
        "cad_mounts": [],
        "electrical_interfaces": [
            p for p in ports.values() if p["kind"] == "electrical_specification"
        ],
    }
    parts = {p["native_id"]: p for p in assembly["instances"]}
    for name, item in sorted(project["cad_instances"].items()):
        part = parts["cad:" + name]
        result["cad_mounts"].append(
            {
                "instance_id": part["id"],
                "frame_id": part["frame_id"],
                "parent": item["placement"]["parent"],
                "world_pose": part["placement"],
                "datum": item["placement"]["datum"],
                "device_id": None,
                "driver_status": "unassigned",
                "mount_check": "recalibrate_on_installation"
                if part["id"] in impacted
                else "verify_on_installation",
            }
        )
    if concept:
        result["gateway_template"] = json.loads(
            concept_gateway_config(concept, assembly["project_id"], project["revision"])
        )
        from app.operation_plan import custom_operation_plan

        result["gateway_template"]["plan_sha256"] = custom_operation_plan(project)["sha256"]
        result["declared_interfaces"] = concept["software"].get("interfaces", [])
    elif platform := (project.get("design") or {}).get("platform"):
        result["platform"] = {
            k: platform[k] for k in ("id", "pipette_mounts", "geometry_scope") if k in platform
        }
    return result


def host_software(project, assembly):
    if design := project.get("concept_design"):
        from app.concept_files import render_asset
        from app.operation_plan import custom_operation_plan

        sequence = custom_operation_plan(project)
        files = {}
        for name in ("controller.py", "gateway-config.json", "project.json"):
            data, _ = render_asset(
                design, assembly["project_id"], assembly["revision"], name, operation_plan=sequence
            )
            if name.endswith(".json"):
                data = json.dumps(json.loads(data), sort_keys=True, indent=2).encode()
            files["software/" + name] = data
        return files
    if assembly["route"] == "workstation":
        from app.assembly import build_preparation_csv, build_preview_source

        host = {**project, "id": assembly["project_id"]}
        return {
            "preparation.csv": build_preparation_csv(host),
            "automation/preview.py": build_preview_source(host),
        }
    return {}


def _host_steps(project, assembly):
    if design := project.get("concept_design"):
        for c in design["components"]:
            yield f"{c['name']} — {c['assembly']} {c['check']}"
    elif fixture := project.get("standalone_fixture"):
        for part in fixture["parts"]:
            yield f"Prepare {part['label']} ({part['id']})."
        for check in fixture.get("checks", []):
            yield (
                check
                if isinstance(check, str)
                else str(check.get("detail") or check.get("label") or check)
            )
    elif station := project.get("bench_station"):
        yield from station["steps"]
    elif source := project.get("upstream_project"):
        for step in source.get("assembly", {}).get("upstream_steps", []):
            yield step["text"] if isinstance(step, dict) else step
        if adapter := source.get("adaptation"):
            yield (
                f"Fabricate {adapter['part_id']} from sources/host/adapter-stl.stl or "
                "sources/host/adapter-step.step. Dry-fit it to the original host and "
                "verify the fasteners and alignment before installation."
            )
    else:
        for step in assembly_steps({**project, "id": assembly["project_id"]}):
            yield " ".join([step["title"], *step["instructions"], *step["checks"]])


def guide(project, assembly, impact):
    lines = [
        f"# Assembly guide · revision {assembly['revision']}",
        "",
        f"Project {assembly['project_id']}",
        f"Assembly SHA-256 {assembly['geometry_sha256']}",
        "",
        "## Host assembly",
        "",
    ]
    lines.extend(f"{n}. {step}" for n, step in enumerate(_host_steps(project, assembly), 1))
    lines.extend(["", "## Install attached modules", ""])
    for item in sorted(project["cad_instances"].values(), key=lambda p: p["name"]):
        pose = item["placement"]
        lines.append(
            f"- Mount {item['name']} on {pose['parent']} at {pose['translation_mm']} mm "
            f"with XYZ rotation {pose['rotation_deg_xyz']} degrees. Datum — {pose['datum']}"
        )
        if a := item.get("adaptation"):
            spec = a["spec"]
            parameters = f"{spec['placement']}, {spec['clearance_mm']:g} mm clearance"
            if spec["kind"] in ("adapter_plate", "cutout_mount"):
                parameters += f", {spec['plate_thickness_mm']:g} mm plate thickness"
            lines.append(
                f"  Print {a['part_id']} from sources/instances/{item['name']}/adapter-stl.stl. "
                f"Check fit with these settings ({parameters})."
            )
    if impact:
        lines.extend(
            [
                "",
                f"## Revision {impact['previous_revision']} to {impact['revision']}",
                "",
                "Reprint " + ", ".join(impact["reprint"]) + ".",
                "Recheck alignment and recalibrate the installed mount datum for "
                + ", ".join(impact["recheck_mount_datums"])
                + ".",
            ]
        )
        labels = {p["id"]: p["label"] for p in assembly["instances"]}
        for left, right in impact["rest_pose_overlap_review"]:
            lines.append(
                f"Check clearance between {labels[left]} and {labels[right]} before "
                "fabrication. Their bounding boxes overlap at the saved rest pose."
            )
        for key, values in sorted(impact["dimensions"].items()):
            label = key.removesuffix("_mm").replace("_", " ").capitalize()
            unit = " mm" if key.endswith("_mm") else ""
            if values["before"] is None:
                lines.append(f"- {label} set to {values['after']}{unit}.")
            else:
                lines.append(
                    f"- {label} changed from {values['before']} to {values['after']}{unit}."
                )
    lines.extend(
        [
            "",
            "Use software/device-bindings.json to identify the affected mount and its interfaces.",
            "Check alignment and clearances on the assembled equipment before commissioning.",
            "",
        ]
    )
    return "\n".join(lines).encode()
