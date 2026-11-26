"""Generate executable Opentrons API v2 and Science Jubilee liquid-handling protocols."""

from __future__ import annotations

import inspect
import json
import math
from decimal import Decimal
from pprint import pformat

from app.dilution import PLATE_96_WELL_ORDER, create_protocol_dilution_plan
from app.operation_plan import device, liquid_plan, operation
from app.project_assembly import component_instance_ids
from app.protocol_preview import simulate_operations

SUPPORTED_OT2_LABWARE: dict[str, dict] = {
    "nest_96_wellplate_200ul_flat": {
        "kind": "plate_96",
        "well_count": 96,
        "well_capacity_ul": 200.0,
    },
    "corning_96_wellplate_360ul_flat": {
        "kind": "plate_96",
        "well_count": 96,
        "well_capacity_ul": 360.0,
    },
    "nest_96_wellplate_2ml_deep": {
        "kind": "plate_96",
        "well_count": 96,
        "well_capacity_ul": 2000.0,
    },
    "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap": {
        "kind": "rack_24",
        "well_count": 24,
        "well_capacity_ul": 1500.0,
        "wells": [f"{r}{c}" for c in range(1, 7) for r in ("A", "B", "C", "D")],
    },
    "opentrons_15_tuberack_falcon_15ml_conical": {
        "kind": "rack_15",
        "well_count": 15,
        "well_capacity_ul": 15000.0,
        "wells": [f"{r}{c}" for c in range(1, 6) for r in ("A", "B", "C")],
    },
    "nest_1_reservoir_195ml": {
        "kind": "reservoir_1",
        "well_count": 1,
        "well_capacity_ul": 195000.0,
        "wells": ["A1"],
    },
    "opentrons_96_tiprack_20ul": {"kind": "tiprack", "well_count": 96, "max_volume_ul": 20.0},
    "opentrons_96_tiprack_300ul": {"kind": "tiprack", "well_count": 96, "max_volume_ul": 300.0},
    "opentrons_96_tiprack_1000ul": {"kind": "tiprack", "well_count": 96, "max_volume_ul": 1000.0},
}

PIPETTE_SPECS: dict[str, dict] = {
    "p20_single_gen2": {
        "min_volume_ul": 1.0,
        "max_volume_ul": 20.0,
        "tiprack": "opentrons_96_tiprack_20ul",
        "mount": "left",
    },
    "p300_single_gen2": {
        "min_volume_ul": 20.0,
        "max_volume_ul": 300.0,
        "tiprack": "opentrons_96_tiprack_300ul",
        "mount": "right",
    },
    "p1000_single_gen2": {
        "min_volume_ul": 100.0,
        "max_volume_ul": 1000.0,
        "tiprack": "opentrons_96_tiprack_1000ul",
        "mount": "right",
    },
}


def _split_volume_chunks(total_ul: float, min_vol: float, max_vol: float) -> list[float]:
    """Split a transfer > max_vol into equal chunks each inside [min_vol, max_vol]."""
    if total_ul <= 0:
        return []
    if total_ul <= max_vol:
        return [round(total_ul, 4)]
    n = math.ceil(total_ul / max_vol)
    chunk = Decimal(str(total_ul)) / Decimal(n)
    if float(chunk) < min_vol:
        n = max(1, int(total_ul // min_vol))
        chunk = Decimal(str(total_ul)) / Decimal(n)
    chunks = [round(float(chunk), 4) for _ in range(n - 1)]
    remainder = round(total_ul - sum(chunks), 4)
    chunks.append(max(min_vol, remainder))
    return chunks


def _select_destination_labware(
    project: dict,
    *,
    final_volume_ul: float,
    target_count: int,
    override: str | None = None,
) -> str:
    if override:
        if override not in SUPPORTED_OT2_LABWARE:
            raise ValueError(f"Unsupported destination labware load name: {override!r}.")
        spec = SUPPORTED_OT2_LABWARE[override]
        if target_count > spec["well_count"]:
            raise ValueError(
                f"Labware {override} has {spec['well_count']} wells, "
                f"cannot hold {target_count} targets."
            )
        if final_volume_ul > spec["well_capacity_ul"]:
            raise ValueError(
                f"Target volume {final_volume_ul:g} uL exceeds "
                f"{override} well capacity ({spec['well_capacity_ul']:g} uL)."
            )
        return override

    plate_id = (project.get("labware_plate") or {}).get("profile_id")
    if plate_id in SUPPORTED_OT2_LABWARE:
        spec = SUPPORTED_OT2_LABWARE[plate_id]
        if (
            target_count <= spec["well_count"]
            and final_volume_ul <= spec["well_capacity_ul"]
        ):
            return plate_id

    if target_count <= 96 and final_volume_ul <= 200.0:
        return "nest_96_wellplate_200ul_flat"
    if target_count <= 96 and final_volume_ul <= 360.0:
        return "corning_96_wellplate_360ul_flat"
    if target_count <= 24 and final_volume_ul <= 1500.0:
        return "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap"
    if target_count <= 96 and final_volume_ul <= 2000.0:
        return "nest_96_wellplate_2ml_deep"
    raise ValueError(
        f"No standard OT-2 destination labware holds {target_count} targets at "
        f"{final_volume_ul:g} uL per well."
    )


def extract_or_build_protocol_plan(
    project: dict,
    *,
    stock_concentration: str | None = None,
    target_concentrations: str | None = None,
    final_volume: str | None = None,
    allow_serial: bool = True,
    allow_multi_dispense: bool = True,
    min_transfer_ul: float = 1.0,
) -> dict:
    """Build a protocol-grade dilution plan from explicit inputs or the project summary."""
    summary = project.get("summary") or {}
    stock = stock_concentration or summary.get("stock_concentration")
    targets = target_concentrations or summary.get("target_concentrations")
    volume = final_volume or summary.get("final_volume")
    plate = project.get("labware_plate") or {}
    ordering = (plate.get("derived") or {}).get("well_ordering") or PLATE_96_WELL_ORDER
    plan = create_protocol_dilution_plan(
        stock,
        targets,
        volume,
        min_transfer_ul=min_transfer_ul,
        allow_serial=allow_serial,
        allow_multi_dispense=allow_multi_dispense,
        well_ordering=ordering,
    )
    if plan["status"] != "ready":
        raise ValueError(plan["message"])
    return plan


def _assign_pipette_for_volume(volume_ul: float, loaded_pipettes: list[str]) -> str:
    """Pick the loaded pipette whose volume range covers volume_ul or supports clean chunking."""
    for name in ("p20_single_gen2", "p300_single_gen2", "p1000_single_gen2"):
        if name not in loaded_pipettes:
            continue
        spec = PIPETTE_SPECS[name]
        if spec["min_volume_ul"] <= volume_ul <= spec["max_volume_ul"]:
            return name
    # Chunk larger transfers with the highest-capacity loaded pipette.
    for name in ("p1000_single_gen2", "p300_single_gen2", "p20_single_gen2"):
        if name in loaded_pipettes and volume_ul >= PIPETTE_SPECS[name]["min_volume_ul"]:
            return name
    return loaded_pipettes[0]


def _choose_ot2_pipettes(plan: dict) -> list[str]:
    """Select at most 2 valid OT-2 pipettes covering all transfer volumes cleanly."""
    vols = [t["volume_ul"] for t in plan["transfers"] if t["volume_ul"] > 0]
    if not vols:
        return ["p300_single_gen2"]
    needs_low = any(v < 20.0 for v in vols)
    needs_mid = any(20.0 <= v < 100.0 for v in vols)
    needs_300 = any(20.0 <= v <= 300.0 for v in vols)
    needs_high = any(v > 300.0 for v in vols)

    if needs_low and not needs_300 and not needs_high:
        return ["p20_single_gen2"]
    if needs_low:
        return ["p20_single_gen2", "p300_single_gen2"]
    if needs_high and not needs_mid:
        return ["p1000_single_gen2"]
    if needs_mid and needs_high:
        return ["p300_single_gen2", "p1000_single_gen2"]
    if needs_high:
        return ["p1000_single_gen2"]
    return ["p300_single_gen2"]


def _ot2_command(row):
    """Render one compiler-authored operation as an Opentrons call."""
    values = row["parameters"]
    args = []
    if "repetitions" in values:
        args.append(str(values["repetitions"]))
    if "volume_ul" in values:
        args.append(repr(values["volume_ul"]))
    if "well" in values:
        labware = {"destination_plate": "dest_plate", "diluent": "diluent_reservoir"}.get(
            values["role"], "source_rack"
        )
        args.append(f"{labware}[{values['well']!r}]" + (".top()" if values.get("top") else ""))
    return f"    pip_{row['binding']}.{row['action']}({', '.join(args)})"


def generate_opentrons_protocol(
    project: dict,
    *,
    protocol_plan: dict | None = None,
    multi_dispense: bool = True,
    allow_serial: bool = True,
    mix_after: bool = True,
    dest_plate_loadname: str | None = None,
) -> dict:
    """Generate an executable Opentrons Protocol API v2 (apiLevel 2.19) Python script."""
    plan = protocol_plan or extract_or_build_protocol_plan(
        project,
        allow_serial=allow_serial,
        allow_multi_dispense=multi_dispense,
    )
    if plan.get("status") != "ready":
        raise ValueError(plan.get("message") or "Protocol dilution plan is not ready.")

    dest_loadname = _select_destination_labware(
        project,
        final_volume_ul=plan["final_volume_ul"],
        target_count=plan["target_count"],
        override=dest_plate_loadname,
    )
    pipette_names = _choose_ot2_pipettes(plan)
    # Resolve left/right mounts safely (never put two pipettes on the same mount).
    mount_map: dict[str, str] = {}
    if len(pipette_names) == 1:
        mount_map[pipette_names[0]] = PIPETTE_SPECS[pipette_names[0]]["mount"]
    else:
        ordered = sorted(pipette_names, key=lambda n: PIPETTE_SPECS[n]["max_volume_ul"])
        mount_map[ordered[0]] = "left"
        mount_map[ordered[1]] = "right"

    rows = []
    target_volumes = {}
    reservoir = {"role": "diluent", "well": "A1"}

    def emit(pip, action, location=None, volume=None, **extra):
        parameters = dict(extra)
        if location is not None:
            parameters.update(location)
        if volume is not None:
            parameters["volume_ul"] = volume
        rows.append(operation(pip, action, parameters))

    def single(item, *, diluent):
        volume = item["volume_ul"]
        pip = _assign_pipette_for_volume(volume, pipette_names)
        spec = PIPETTE_SPECS[pip]
        loc = {"role": item["dest_role"], "well": item["dest_well"]}
        src = reservoir if diluent else {"role": item["source_role"], "well": item["source_well"]}
        emit(pip, "pick_up_tip")
        for chunk in _split_volume_chunks(volume, spec["min_volume_ul"], spec["max_volume_ul"]):
            emit(pip, "aspirate", src, chunk)
            emit(pip, "dispense", loc, chunk)
            if diluent:
                emit(pip, "blow_out", loc, top=True)
        key = (loc["role"], loc["well"])
        target_volumes[key] = target_volumes.get(key, 0) + volume
        if not diluent:
            if mix_after and item.get("mix_after"):
                amount = min(
                    spec["max_volume_ul"], max(spec["min_volume_ul"], target_volumes[key] * 0.5)
                )
                emit(pip, "mix", loc, round(amount, 4), repetitions=3)
            emit(pip, "blow_out", loc, top=True)
            emit(pip, "touch_tip", loc)
        emit(pip, "drop_tip")

    groups = plan.get("multi_dispense_groups") if multi_dispense else None
    for group in groups or []:
        dispenses = group["dispenses"]
        pip = _assign_pipette_for_volume(max(d["volume_ul"] for d in dispenses), pipette_names)
        total = sum(d["volume_ul"] for d in dispenses) + group["disposal_ul"]
        if len(dispenses) == 1 or total > PIPETTE_SPECS[pip]["max_volume_ul"]:
            for item in dispenses:
                single(item, diluent=True)
            continue
        emit(pip, "pick_up_tip")
        emit(pip, "aspirate", reservoir, total)
        for item in dispenses:
            loc = {"role": item["dest_role"], "well": item["dest_well"]}
            emit(pip, "dispense", loc, item["volume_ul"])
            key = (loc["role"], loc["well"])
            target_volumes[key] = target_volumes.get(key, 0) + item["volume_ul"]
        if group["disposal_ul"] > 0:
            emit(pip, "blow_out", reservoir, top=True)
        emit(pip, "drop_tip")
    for item in plan["transfers"]:
        diluent = item["phase"] == "diluent"
        if not (groups and diluent):
            single(item, diluent=diluent)

    component = component_instance_ids(project).get("pipette")
    bindings = {
        pip: device(
            component,
            "liquid_handling",
            pip,
            "uL",
            {
                **{k: PIPETTE_SPECS[pip][k] for k in ("min_volume_ul", "max_volume_ul", "tiprack")},
                "mount": mount_map[pip],
                "destination_labware": dest_loadname,
            },
            [f"Verify the {mount_map[pip]} mount, tip rack and calibrated deck."],
        )
        for pip in pipette_names
    }
    tip_counts = {
        pip: sum(row["binding"] == pip and row["action"] == "pick_up_tip" for row in rows)
        for pip in pipette_names
    }
    # Only an OT-2 design has meaningful OT-2 slot assignments. Other platforms are previews.
    design = project.get("design") or {}
    assigned = {}
    if (design.get("platform") or {}).get("id") == "opentrons-ot2":
        assigned = {
            part["id"]: str(part["slot_id"]) for part in design["components"] if part.get("slot_id")
        }
    dest_slot = assigned.get("sample-rack", "1")
    source_slot = assigned.get("source-rack", "2")
    tip_slot = assigned.get("tip-rack", "4")
    free_slots = [
        str(i)
        for i in range(1, 12)
        if str(i) not in assigned.values() and str(i) not in {dest_slot, source_slot, tip_slot}
    ]
    diluent_slot = assigned.get("diluent-reservoir") or free_slots.pop(0)
    source_load = "opentrons_15_tuberack_falcon_15ml_conical"
    diluent_load = "nest_1_reservoir_195ml"
    deck_slots = {dest_slot: dest_loadname, source_slot: source_load, diluent_slot: diluent_load}
    free_slots.insert(0, tip_slot)
    blockers = []
    if assigned and "diluent-reservoir" not in assigned:
        blockers.append(f"The diluent reservoir in slot {diluent_slot} has no bound assembly part.")
    tiprack_vars_by_pip: dict[str, list[str]] = {}
    rack_idx = 0
    for pip in pipette_names:
        tips_needed = max(1, tip_counts[pip])
        racks_needed = math.ceil(tips_needed / 96)
        if racks_needed > len(free_slots):
            raise ValueError(
                f"Protocol requires {racks_needed} tip racks for {pip} "
                f"({tips_needed} tips), exceeding available OT-2 deck slots."
            )
        tiprack_load = PIPETTE_SPECS[pip]["tiprack"]
        var_list: list[str] = []
        limits = bindings[pip]["limits"]
        limits.update(
            destination_slot=dest_slot,
            source_slot=source_slot,
            diluent_slot=diluent_slot,
            tiprack_slots=[],
        )
        for _ in range(racks_needed):
            slot = free_slots.pop(0)
            limits["tiprack_slots"].append(slot)
            rack_idx += 1
            var_name = f"tiprack_{rack_idx}"
            deck_slots[slot] = tiprack_load
            var_list.append(f"{var_name} = protocol.load_labware({tiprack_load!r}, {slot!r})")
        tiprack_vars_by_pip[pip] = var_list

    if assigned and rack_idx > 1:
        blockers.append("Bind all required tip racks to their installed deck slots.")
    if assigned and len(pipette_names) > 1:
        blockers.append("Bind each pipette mount to a distinct installed instrument.")
    sequence = liquid_plan(project, "opentrons-ot2", plan, bindings, rows, blockers=blockers)
    setup_lines = [
        f"    dest_plate = protocol.load_labware({dest_loadname!r}, {dest_slot!r})",
        f"    source_rack = protocol.load_labware({source_load!r}, {source_slot!r})",
        f"    diluent_reservoir = protocol.load_labware({diluent_load!r}, {diluent_slot!r})",
    ]
    pipette_lines: list[str] = []
    for pip in pipette_names:
        mount = mount_map[pip]
        assigned_vars: list[str] = []
        for stmt in tiprack_vars_by_pip[pip]:
            assigned_vars.append(stmt.split(" =", 1)[0])
            setup_lines.append("    " + stmt)
        racks_arg = ", ".join(assigned_vars)
        pipette_lines.append(
            f"    pip_{pip} = protocol.load_instrument({pip!r}, {mount!r}, "
            f"tip_racks=[{racks_arg}])"
        )

    project_id = str(project.get("lineage_id") or project["id"])
    revision = int(project["revision"])
    header_comment = (
        f"Project {project_id} r{revision} | mode={plan['mode']} | "
        f"{plan['target_count']} targets @ {plan['final_volume_ul']:g} uL"
    )
    meta = {
        "protocolName": f"AutoLab Dilution ({project_id} r{revision})",
        "author": "AutoLab",
        "description": header_comment,
    }
    reqs = {"robotType": "OT-2", "apiLevel": "2.19"}

    lines: list[str] = [
        '"""AutoLab-generated Opentrons Protocol API v2 script."""',
        "",
        "from opentrons import protocol_api",
        "",
        f"metadata = {pformat(meta, width=96, sort_dicts=False)}",
        f"requirements = {json.dumps(reqs)}",
        f"PLAN_SHA256 = {sequence['sha256']!r}",
        f"DESTINATION_SLOT = {dest_slot!r}",
        "",
        "",
        "def run(protocol: protocol_api.ProtocolContext):",
        f"    protocol.comment({header_comment!r})",
        "    if not protocol.is_simulating():",
        (
            "        protocol.pause("
            + repr(
                f"Confirm commissioning of the bound instruments for "
                f"{project_id} r{revision} before resuming."
            )
            + ")"
            if sequence["scope"] == "approved"
            and not sequence["hardware_blockers"]
            and all(r["mode"] == "driver" for r in sequence["operations"])
            else "        raise RuntimeError('This protocol is available for offline review only.')"
        ),
        *setup_lines,
        *pipette_lines,
        "",
        *[
            line
            for row in sequence["operations"]
            for line in (
                f"    protocol.comment({('Operation ' + row['id'] + ' ' + row['mode'])!r})",
                _ot2_command(row)
                if row["mode"] == "driver"
                else f"    protocol.comment({row['action']!r})",
            )
        ],
        "",
        "    protocol.comment('AutoLab protocol completed.')",
        "",
    ]
    source_code = "\n".join(lines)
    return {
        "platform": "opentrons-ot2",
        "filename": "opentrons_protocol.py",
        "api_level": "2.19",
        "destination_labware": dest_loadname,
        "deck_slots": deck_slots,
        "pipettes": {mount_map[p]: p for p in pipette_names},
        "tips_required": tip_counts,
        "operation_plan": sequence,
        "plan": plan,
        "source_code": source_code,
    }


def generate_jubilee_protocol(
    project: dict,
    *,
    protocol_plan: dict | None = None,
    multi_dispense: bool = True,
    allow_serial: bool = True,
    safe_z_mm: float = 100.0,
) -> dict:
    """Generate an executable Science Jubilee Python protocol with offline --simulate mode."""
    plan = protocol_plan or extract_or_build_protocol_plan(
        project,
        allow_serial=allow_serial,
        allow_multi_dispense=multi_dispense,
    )
    if plan.get("status") != "ready":
        raise ValueError(plan.get("message") or "Protocol dilution plan is not ready.")
    if not math.isfinite(safe_z_mm) or safe_z_mm <= 0 or safe_z_mm > 350:
        raise ValueError("safe_z_mm must be within (0, 350].")

    project_id = str(project.get("lineage_id") or project["id"])
    revision = int(project["revision"])
    dest_loadname = _select_destination_labware(
        project,
        final_volume_ul=plan["final_volume_ul"],
        target_count=plan["target_count"],
    )
    rows = []
    for item in plan["transfers"]:
        rows.append(
            operation(
                "pipette",
                "transfer",
                {
                    k: item[k]
                    for k in ("source_role", "source_well", "dest_role", "dest_well", "volume_ul")
                }
                | {"new_tip": "always"},
            )
        )
        if item.get("mix_after"):
            rows.append(
                operation(
                    "pipette",
                    "mix",
                    {
                        "dest_role": item["dest_role"],
                        "dest_well": item["dest_well"],
                        "volume_ul": item.get("mix_volume_ul", item["volume_ul"]),
                        "repetitions": 3,
                    },
                )
            )
    bindings = {
        "pipette": device(
            component_instance_ids(project).get("pipette"),
            "liquid_handling",
            "science_jubilee.tools.Pipette",
            "uL",
            {
                "minimum_planned_ul": plan["min_transfer_ul"],
                "commissioned_range_ul": None,
                "fresh_tip_per_transfer": True,
            },
            ["Verify pipette range, tip inventory and deck calibration."],
        )
    }
    tiprack = "opentrons_96_tiprack_300ul"
    tip_capacity = SUPPORTED_OT2_LABWARE[tiprack]["max_volume_ul"]
    limits = bindings["pipette"]["limits"]
    limits.update(
        tip_capacity_ul=tip_capacity,
        tip_positions=96,
        safe_z_mm=float(safe_z_mm),
        destination_labware=dest_loadname,
        tiprack=tiprack,
    )
    blockers = []
    if any(row["parameters"]["volume_ul"] > tip_capacity for row in rows):
        blockers.append("A planned command exceeds the configured 300 uL tip capacity.")
    if len(plan["transfers"]) > 96:
        blockers.append("The required fresh tips exceed the configured rack.")
    sequence = liquid_plan(project, "jubilee", plan, bindings, rows, blockers=blockers)
    compact_plan = {
        "operation_plan": sequence,
        "project_id": project_id,
        "revision": revision,
        "mode": plan["mode"],
        "stock": plan["stock"],
        "final_volume_ul": plan["final_volume_ul"],
        "target_count": plan["target_count"],
        "intermediate_steps": plan["intermediate_steps"],
        "safe_z_mm": float(safe_z_mm),
        "labware": {
            "destination": dest_loadname,
            "source_rack": "opentrons_15_tuberack_falcon_15ml_conical",
            "diluent_reservoir": "nest_1_reservoir_195ml",
        },
    }
    plan_literal = pformat(compact_plan, width=92, sort_dicts=False)

    script = f'''"""Science Jubilee executable liquid-handling protocol ({project_id} r{revision}).

Simulate locally without hardware:
    python jubilee_protocol.py --simulate

Execute on a commissioned Science Jubilee over Duet HTTP:
    python jubilee_protocol.py --address <duet-ip> --arm-hardware --commissioning FILE
"""

from __future__ import annotations

import argparse
import json

PROTOCOL_SPEC = {plan_literal}


{inspect.getsource(simulate_operations)}


def _simulate_plan() -> dict:
    return {{"status": "simulated_ok", "platform": "jubilee",
            "project_id": PROTOCOL_SPEC["project_id"], "revision": PROTOCOL_SPEC["revision"],
            "mode": PROTOCOL_SPEC["mode"], "target_count": PROTOCOL_SPEC["target_count"],
            **simulate_operations(PROTOCOL_SPEC["operation_plan"])}}


def _dispatch(pipette, resolve, sequence):
    done = []
    for row in sequence["operations"]:
        if row["mode"] != "driver":
            raise ValueError("Unresolved operation in device sequence.")
        item = row["parameters"]
        dst = resolve(item["dest_role"], item["dest_well"])
        if row["action"] == "transfer":
            src = resolve(item["source_role"], item["source_well"])
            pipette.transfer(item["volume_ul"], src, dst, new_tip=item["new_tip"])
        elif row["action"] == "mix":
            pipette.mix(item["volume_ul"], item["repetitions"], dst)
        else:
            raise ValueError("Unknown Jubilee operation.")
        done.append(row["id"])
    return done


def _run_hardware(address: str, tool_index: int, *, armed=False, commissioning=None) -> dict:
    sequence = PROTOCOL_SPEC["operation_plan"]
    expected = {{"project_id": sequence["project_id"], "revision": sequence["revision"],
                "plan_sha256": sequence["sha256"], "address": address, "tool_index": tool_index}}
    if (not armed or sequence["scope"] != "approved" or sequence["hardware_blockers"]
            or any(r["mode"] != "driver" for r in sequence["operations"])
            or not isinstance(commissioning, dict)
            or commissioning.get("commissioning_reviewed") is not True
            or any(commissioning.get(k) != v for k, v in expected.items())):
        raise ValueError("Review and arm this revision and its bound controller and tool.")
    low, high = commissioning.get("pipette_min_ul"), commissioning.get("pipette_max_ul")
    volumes = [row["parameters"]["volume_ul"] for row in sequence["operations"]]
    if (type(low) not in (int, float) or type(high) not in (int, float) or not 0 < low <= high
            or not all(low <= volume <= high for volume in volumes)):
        raise ValueError("The reviewed pipette range must cover each planned command.")
    from science_jubilee.Machine import Machine
    from science_jubilee.tools.Pipette import Pipette

    machine = Machine(address=address)
    machine.home_all()
    machine.move_to(z=PROTOCOL_SPEC["safe_z_mm"])
    pipette = Pipette.from_config(tool_index, "Pipette", "pipette_config.json")
    machine.load_tool(pipette)
    deck = machine.load_deck("lab_automation_deck.json")
    tiprack = deck.load_labware(sequence["bindings"]["pipette"]["limits"]["tiprack"], slot=0)
    source_rack = deck.load_labware(PROTOCOL_SPEC["labware"]["source_rack"], slot=1)
    diluent_res = deck.load_labware(PROTOCOL_SPEC["labware"]["diluent_reservoir"], slot=2)
    dest_plate = deck.load_labware(PROTOCOL_SPEC["labware"]["destination"], slot=3)
    pipette.add_tiprack(tiprack)

    def resolve(role: str, well: str):
        if role == "destination_plate":
            return dest_plate[well]
        if role == "diluent":
            return diluent_res[well]
        return source_rack[well]

    machine.pickup_tool(pipette)
    try:
        done = _dispatch(pipette, resolve, sequence)
    finally:
        machine.park_tool()
    return {{"status": "hardware_completed", "steps_executed": len(done), "operation_ids": done,
            "plan_sha256": sequence["sha256"]}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate", action="store_true", help="Run offline simulation check")
    parser.add_argument("--address", default=None, help="Duet IP/hostname for commissioned Jubilee")
    parser.add_argument("--tool-index", type=int, default=0, help="Configured Jubilee pipette tool")
    parser.add_argument(
        "--arm-hardware",
        action="store_true",
        help="Explicitly confirm physical bench hardware is commissioned and clear",
    )
    parser.add_argument("--commissioning", help="Reviewed record for this plan, address and tool")
    args = parser.parse_args()
    if args.address and args.arm_hardware and not args.simulate:
        if not args.commissioning:
            parser.exit(2, "Supply the reviewed commissioning record for this device.\\n")
        with open(args.commissioning, encoding="utf-8") as stream:
            reviewed = json.load(stream)
        result = _run_hardware(args.address, args.tool_index, armed=True, commissioning=reviewed)
    elif args.address and not args.arm_hardware:
        parser.exit(2, "Refusing live Jubilee motion without --arm-hardware confirmation.\\n")
    else:
        result = _simulate_plan()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
    return {
        "platform": "jubilee",
        "filename": "jubilee_protocol.py",
        "operation_plan": sequence,
        "tips_required": sum(row["action"] == "transfer" for row in sequence["operations"]),
        "plan": plan,
        "source_code": script,
    }
