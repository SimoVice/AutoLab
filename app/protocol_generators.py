"""Generate executable Opentrons API v2 and Science Jubilee liquid-handling protocols."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pprint import pformat

from app.dilution import PLATE_96_WELL_ORDER, create_protocol_dilution_plan

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

    # Build step instructions & track exact tips needed per pipette.
    tip_counts = {name: 0 for name in pipette_names}
    code_ops: list[str] = []

    def _well_expr(role: str, well: str) -> str:
        if role == "destination_plate":
            return f"dest_plate[{well!r}]"
        if role == "diluent":
            return f"diluent_reservoir[{well!r}]"
        return f"source_rack[{well!r}]"

    # Track current volume in every non-reservoir vessel so mix volume never overflows.
    vessel_volumes: dict[str, float] = {}

    # 1) Diluent steps (multi-dispense when enabled and beneficial).
    md_groups = plan.get("multi_dispense_groups") or []
    diluent_transfers = [t for t in plan["transfers"] if t["phase"] == "diluent"]
    if multi_dispense and md_groups:
        for grp in md_groups:
            dispenses = grp["dispenses"]
            if len(dispenses) > 1:
                pip = _assign_pipette_for_volume(
                    max(d["volume_ul"] for d in dispenses), pipette_names
                )
                spec = PIPETTE_SPECS[pip]
                total_need = sum(d["volume_ul"] for d in dispenses) + grp["disposal_ul"]
                if total_need <= spec["max_volume_ul"]:
                    tip_counts[pip] += 1
                    pvar = f"pip_{pip}"
                    code_ops.append(
                        f"    # Multi-dispense diluent group {grp['group_id']} "
                        f"({len(dispenses)} wells)"
                    )
                    code_ops.append(f"    {pvar}.pick_up_tip()")
                    code_ops.append(
                        f"    {pvar}.aspirate({total_need:g}, diluent_reservoir['A1'])"
                    )
                    for item in dispenses:
                        dexpr = _well_expr(item["dest_role"], item["dest_well"])
                        code_ops.append(f"    {pvar}.dispense({item['volume_ul']:g}, {dexpr})")
                        key = f"{item['dest_role']}:{item['dest_well']}"
                        vessel_volumes[key] = vessel_volumes.get(key, 0.0) + item["volume_ul"]
                    if grp["disposal_ul"] > 0:
                        code_ops.append(f"    {pvar}.blow_out(diluent_reservoir['A1'].top())")
                    code_ops.append(f"    {pvar}.drop_tip()")
                    continue
            for item in dispenses:
                pip = _assign_pipette_for_volume(item["volume_ul"], pipette_names)
                spec = PIPETTE_SPECS[pip]
                tip_counts[pip] += 1
                pvar = f"pip_{pip}"
                dexpr = _well_expr(item["dest_role"], item["dest_well"])
                chunks = _split_volume_chunks(
                    item["volume_ul"], spec["min_volume_ul"], spec["max_volume_ul"]
                )
                code_ops.append(f"    {pvar}.pick_up_tip()")
                for chunk in chunks:
                    code_ops.append(f"    {pvar}.aspirate({chunk:g}, diluent_reservoir['A1'])")
                    code_ops.append(f"    {pvar}.dispense({chunk:g}, {dexpr})")
                    code_ops.append(f"    {pvar}.blow_out({dexpr}.top())")
                code_ops.append(f"    {pvar}.drop_tip()")
                key = f"{item['dest_role']}:{item['dest_well']}"
                vessel_volumes[key] = vessel_volumes.get(key, 0.0) + item["volume_ul"]
    else:
        for item in diluent_transfers:
            pip = _assign_pipette_for_volume(item["volume_ul"], pipette_names)
            spec = PIPETTE_SPECS[pip]
            tip_counts[pip] += 1
            pvar = f"pip_{pip}"
            dexpr = _well_expr(item["dest_role"], item["dest_well"])
            chunks = _split_volume_chunks(
                item["volume_ul"], spec["min_volume_ul"], spec["max_volume_ul"]
            )
            code_ops.append(f"    {pvar}.pick_up_tip()")
            for chunk in chunks:
                code_ops.append(f"    {pvar}.aspirate({chunk:g}, diluent_reservoir['A1'])")
                code_ops.append(f"    {pvar}.dispense({chunk:g}, {dexpr})")
                code_ops.append(f"    {pvar}.blow_out({dexpr}.top())")
            code_ops.append(f"    {pvar}.drop_tip()")
            key = f"{item['dest_role']}:{item['dest_well']}"
            vessel_volumes[key] = vessel_volumes.get(key, 0.0) + item["volume_ul"]

    # 2) Serial intermediates + 3) Sample transfers (strictly one fresh tip per source).
    for item in plan["transfers"]:
        if item["phase"] == "diluent":
            continue
        pip = _assign_pipette_for_volume(item["volume_ul"], pipette_names)
        spec = PIPETTE_SPECS[pip]
        tip_counts[pip] += 1
        pvar = f"pip_{pip}"
        sexpr = _well_expr(item["source_role"], item["source_well"])
        dexpr = _well_expr(item["dest_role"], item["dest_well"])
        chunks = _split_volume_chunks(
            item["volume_ul"], spec["min_volume_ul"], spec["max_volume_ul"]
        )
        code_ops.append(
            f"    # {item['phase']}: {item['source_role']} ({item['source_well']}) -> "
            f"{item['dest_well']} ({item['target']})"
        )
        code_ops.append(f"    {pvar}.pick_up_tip()")
        for chunk in chunks:
            code_ops.append(f"    {pvar}.aspirate({chunk:g}, {sexpr})")
            code_ops.append(f"    {pvar}.dispense({chunk:g}, {dexpr})")
        key = f"{item['dest_role']}:{item['dest_well']}"
        vessel_volumes[key] = vessel_volumes.get(key, 0.0) + item["volume_ul"]
        if mix_after and item.get("mix_after"):
            cur_vol = vessel_volumes.get(key, item["volume_ul"])
            raw_mix = min(spec["max_volume_ul"], max(spec["min_volume_ul"], cur_vol * 0.5))
            mix_vol = round(min(raw_mix, spec["max_volume_ul"]), 4)
            if mix_vol >= spec["min_volume_ul"]:
                code_ops.append(f"    {pvar}.mix(3, {mix_vol:g}, {dexpr})")
        code_ops.append(f"    {pvar}.blow_out({dexpr}.top())")
        code_ops.append(f"    {pvar}.touch_tip({dexpr})")
        code_ops.append(f"    {pvar}.drop_tip()")

    # Allocate deck slots (1=dest, 2=source/intermediates, 3=diluent, 4..11=tipracks).
    deck_slots: dict[str, str] = {
        "1": dest_loadname,
        "2": "opentrons_15_tuberack_falcon_15ml_conical",
        "3": "nest_1_reservoir_195ml",
    }
    free_slots = [str(s) for s in range(4, 12)]
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
        for _ in range(racks_needed):
            slot = free_slots.pop(0)
            rack_idx += 1
            var_name = f"tiprack_{rack_idx}"
            deck_slots[slot] = tiprack_load
            var_list.append(f"{var_name} = protocol.load_labware({tiprack_load!r}, {slot!r})")
        tiprack_vars_by_pip[pip] = var_list

    setup_lines: list[str] = [
        f"    dest_plate = protocol.load_labware({dest_loadname!r}, '1')",
        "    source_rack = protocol.load_labware("
        "'opentrons_15_tuberack_falcon_15ml_conical', '2')",
        "    diluent_reservoir = protocol.load_labware('nest_1_reservoir_195ml', '3')",
    ]
    rack_counter = 0
    pipette_lines: list[str] = []
    for pip in pipette_names:
        mount = mount_map[pip]
        assigned_vars: list[str] = []
        for stmt in tiprack_vars_by_pip[pip]:
            rack_counter += 1
            assigned_vars.append(stmt.split(" =", 1)[0])
            setup_lines.append("    " + stmt)
        racks_arg = ", ".join(assigned_vars)
        pipette_lines.append(
            f"    pip_{pip} = protocol.load_instrument({pip!r}, {mount!r}, "
            f"tip_racks=[{racks_arg}])"
        )

    project_id = str(project.get("id", "autolab-project"))
    revision = int(project.get("revision", 1) or 1)
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
        "",
        "",
        "def run(protocol: protocol_api.ProtocolContext):",
        f"    protocol.comment({header_comment!r})",
        *setup_lines,
        *pipette_lines,
        "",
        *code_ops,
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

    project_id = str(project.get("id", "autolab-project"))
    revision = int(project.get("revision", 1) or 1)
    dest_loadname = _select_destination_labware(
        project,
        final_volume_ul=plan["final_volume_ul"],
        target_count=plan["target_count"],
    )
    compact_plan = {
        "project_id": project_id,
        "revision": revision,
        "mode": plan["mode"],
        "stock": plan["stock"],
        "final_volume_ul": plan["final_volume_ul"],
        "target_count": plan["target_count"],
        "intermediate_steps": plan["intermediate_steps"],
        "transfers": plan["transfers"],
        "multi_dispense_groups": plan.get("multi_dispense_groups") or [],
        "multi_dispense_enabled": bool(multi_dispense),
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
    python jubilee_protocol.py --address <duet-ip> --arm-hardware
"""

from __future__ import annotations

import argparse
import json

PROTOCOL_SPEC = {plan_literal}


def _simulate_plan() -> dict:
    vessels: dict[str, float] = {{}}
    consumed = {{"stock": 0.0, "diluent": 0.0}}
    tips_used = 0
    executed_steps: list[dict] = []

    use_md = PROTOCOL_SPEC["multi_dispense_enabled"] and PROTOCOL_SPEC["multi_dispense_groups"]
    if use_md:
        for grp in PROTOCOL_SPEC["multi_dispense_groups"]:
            tips_used += 1
            for item in grp["dispenses"]:
                key = f"{{item['dest_role']}}:{{item['dest_well']}}"
                vessels[key] = round(vessels.get(key, 0.0) + item["volume_ul"], 4)
                consumed["diluent"] = round(consumed["diluent"] + item["volume_ul"], 4)
                executed_steps.append(
                    {{"op": "multi_dispense", "well": item["dest_well"], "uL": item["volume_ul"]}}
                )

    for item in PROTOCOL_SPEC["transfers"]:
        if use_md and item["phase"] == "diluent":
            continue
        tips_used += 1
        key = f"{{item['dest_role']}}:{{item['dest_well']}}"
        vessels[key] = round(vessels.get(key, 0.0) + item["volume_ul"], 4)
        if item["source_role"] in ("stock", "diluent"):
            consumed[item["source_role"]] = round(
                consumed[item["source_role"]] + item["volume_ul"], 4
            )
        executed_steps.append(
            {{
                "op": item["phase"],
                "from": f"{{item['source_role']}}:{{item['source_well']}}",
                "to": key,
                "uL": item["volume_ul"],
                "mix_after": bool(item.get("mix_after")),
            }}
        )

    return {{
        "status": "simulated_ok",
        "platform": "jubilee",
        "project_id": PROTOCOL_SPEC["project_id"],
        "revision": PROTOCOL_SPEC["revision"],
        "mode": PROTOCOL_SPEC["mode"],
        "target_count": PROTOCOL_SPEC["target_count"],
        "steps_executed": len(executed_steps),
        "tips_used": tips_used,
        "consumed_ul": consumed,
        "destination_wells": {{
            k.split(":", 1)[1]: v
            for k, v in vessels.items()
            if k.startswith("destination_plate:")
        }},
    }}


def _run_hardware(address: str, tool_index: int) -> dict:
    from science_jubilee.Machine import Machine
    from science_jubilee.tools.Pipette import Pipette

    machine = Machine(address=address)
    machine.home_all()
    machine.move_to(z=PROTOCOL_SPEC["safe_z_mm"])
    pipette = Pipette.from_config(tool_index, "Pipette", "pipette_config.json")
    machine.load_tool(pipette)
    deck = machine.load_deck("lab_automation_deck.json")
    tiprack = deck.load_labware("opentrons_96_tiprack_300ul", slot=0)
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
    steps_done = 0
    try:
        for item in PROTOCOL_SPEC["transfers"]:
            src = resolve(item["source_role"], item["source_well"])
            dst = resolve(item["dest_role"], item["dest_well"])
            pipette.transfer(item["volume_ul"], src, dst, new_tip="always")
            if item.get("mix_after"):
                pipette.mix(item.get("mix_volume_ul", item["volume_ul"]), 3, dst)
            steps_done += 1
    finally:
        machine.park_tool()
    return {{"status": "hardware_completed", "steps_executed": steps_done}}


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
    args = parser.parse_args()
    if args.address and args.arm_hardware and not args.simulate:
        result = _run_hardware(args.address, args.tool_index)
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
        "plan": plan,
        "source_code": script,
    }
