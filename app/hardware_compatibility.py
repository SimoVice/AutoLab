"""Deterministic electrical, I/O, mechanical, and fabrication hardware compatibility checks."""

from __future__ import annotations

import math
from typing import Any

from app.bom_engine import build_complete_bom


def _entry(
    check_id: str,
    category: str,
    severity: str,
    title: str,
    detail: str,
    *,
    recommended_fix: str | None = None,
    affected_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "category": category,
        "severity": severity,  # "pass" | "warning" | "error"
        "passed": severity == "pass",
        "title": title,
        "detail": detail,
        "recommended_fix": recommended_fix,
        "affected_ids": list(affected_ids or []),
    }


def _compute_available_rails(lines: list[dict[str, Any]]) -> tuple[dict[float, float], float]:
    """Return ({voltage_v: max_watts}, total_primary_ac_dc_watts) across BOM supplies."""
    rails: dict[float, float] = {}
    primary_watts = 0.0
    psus = [
        r for r in lines if (r.get("specs") or {}).get("kind") == "power_supply"
    ]
    converters = [
        r for r in lines if (r.get("specs") or {}).get("kind") == "dc_dc_converter"
    ]
    robots = [
        r for r in lines if (r.get("specs") or {}).get("kind") == "commercial_robot"
    ]
    for r in psus:
        specs = r["specs"]
        qty = max(1, int(r.get("quantity") or 1))
        v = float(specs["output_voltage_v"])
        w = float(specs.get("rated_power_w") or 0.0) * qty
        rails[v] = rails.get(v, 0.0) + w
        primary_watts += w
    for r in robots:
        v = float((r.get("specs") or {}).get("supply_voltage_v") or 115.0)
        rails[v] = rails.get(v, 0.0) + 250.0
    # Converters produce downstream rails only when fed by a compatible primary rail.
    for conv in converters:
        specs = conv["specs"]
        vmin = float(specs.get("input_voltage_min_v") or 0.0)
        vmax = float(specs.get("input_voltage_max_v") or 999.0)
        if any(vmin - 0.5 <= rv <= vmax + 0.5 for rv in rails):
            out_v = float(specs["output_voltage_v"])
            qty = max(1, int(conv.get("quantity") or 1))
            out_w = float(specs.get("rated_power_w") or 0.0) * qty
            rails[out_v] = rails.get(out_v, 0.0) + out_w
    return rails, primary_watts


def evaluate_hardware_compatibility(
    project: dict[str, Any],
    bom: dict[str, Any] | None = None,
    *,
    lab_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run deterministic electrical, I/O, mechanical, and fabrication compatibility validation."""
    resolved_bom = bom or build_complete_bom(project, lab_profile=lab_profile)
    lines = resolved_bom.get("lines") or []
    checks: list[dict[str, Any]] = []

    rails, total_primary_watts = _compute_available_rails(lines)
    controllers = [r for r in lines if (r.get("specs") or {}).get("kind") == "controller"]
    drivers = [r for r in lines if (r.get("specs") or {}).get("kind") == "stepper_driver"]
    motors = [
        r
        for r in lines
        if (r.get("specs") or {}).get("kind") in ("stepper_motor", "stepper_motor_kit")
    ]
    coolers = [
        r
        for r in lines
        if (r.get("specs") or {}).get("kind") in ("cooling", "commercial_module")
    ]
    cameras = [r for r in lines if (r.get("specs") or {}).get("kind") == "camera"]
    sensors = [r for r in lines if (r.get("specs") or {}).get("kind") == "sensor"]
    endstops = [r for r in lines if (r.get("specs") or {}).get("kind") == "endstop"]
    printed = [r for r in lines if r.get("kind") == "3d_printed"]
    mounts = [r for r in lines if (r.get("specs") or {}).get("kind") == "motor_mount"]
    pulleys = [
        r
        for r in lines
        if (r.get("specs") or {}).get("kind") in ("pulley", "coupler", "motion_kit")
    ]
    extrusions = [r for r in lines if (r.get("specs") or {}).get("kind") == "extrusion"]
    tnut_kits = [r for r in lines if (r.get("specs") or {}).get("kind") == "fastener"]

    # 1. Voltage rail compatibility
    for col in coolers:
        specs = col["specs"]
        req_v = float(specs.get("supply_voltage_v") or 12.0)
        if specs.get("kind") == "commercial_module":
            continue
        matched = any(abs(rv - req_v) <= 0.1 * req_v for rv in rails)
        if not matched:
            avail_list = ", ".join(f"{v:g} V" for v in sorted(rails)) or "none"
            checks.append(
                _entry(
                    f"voltage-{col['catalog_id']}",
                    "voltage_compatibility",
                    "error",
                    f"Missing {req_v:g} V supply rail for {col['label']}",
                    f"{col['label']} requires {req_v:g} V ({specs.get('peak_power_w', 72):g} W "
                    f"peak), but BOM only supplies [{avail_list}].",
                    recommended_fix=(
                        f"Add a {req_v:g} V power supply (e.g. Mean Well LRS-150-12) or DC-DC "
                        f"buck converter rated >= "
                        f"{math.ceil(float(specs.get('rated_current_a') or 6) * 1.25)} A."
                    ),
                    affected_ids=[col["line_id"]],
                )
            )
        else:
            checks.append(
                _entry(
                    f"voltage-{col['catalog_id']}",
                    "voltage_compatibility",
                    "pass",
                    f"{req_v:g} V supply matches {col['label']}",
                    f"Verified {req_v:g} V power rail present in BOM.",
                    affected_ids=[col["line_id"]],
                )
            )

    for ctrl in controllers:
        specs = ctrl["specs"]
        cmin = float(specs.get("input_voltage_min_v") or 4.5)
        cmax = float(specs.get("input_voltage_max_v") or 5.5)
        # Standalone SBC hosts (e.g. Pi Zero 2 W on fixture rig without main gantry PSU)
        # assume standard USB-C 5V supply when no frame PSU is present.
        if not rails and cmin <= 5.0 <= cmax:
            checks.append(
                _entry(
                    f"voltage-{ctrl['catalog_id']}",
                    "voltage_compatibility",
                    "pass",
                    f"5 V USB power path noted for {ctrl['label']}",
                    "Controller runs on 5 V USB supply (no high-voltage gantry PSU present).",
                    affected_ids=[ctrl["line_id"]],
                )
            )
            continue
        ok = any(cmin - 0.1 <= rv <= cmax + 0.1 for rv in rails)
        if not ok:
            avail_list = ", ".join(f"{v:g} V" for v in sorted(rails)) or "none"
            checks.append(
                _entry(
                    f"voltage-{ctrl['catalog_id']}",
                    "voltage_compatibility",
                    "error",
                    f"Incompatible logic power voltage for {ctrl['label']}",
                    f"Requires {cmin:g}-{cmax:g} V DC, available rails: [{avail_list}].",
                    recommended_fix="Add a 24 V->5 V buck converter (Pololu D24V22F5).",
                    affected_ids=[ctrl["line_id"]],
                )
            )
        else:
            checks.append(
                _entry(
                    f"voltage-{ctrl['catalog_id']}",
                    "voltage_compatibility",
                    "pass",
                    f"Supply rail within {cmin:g}-{cmax:g} V window for {ctrl['label']}",
                    "Controller logic supply rail matches BOM power architecture.",
                    affected_ids=[ctrl["line_id"]],
                )
            )

    motor_rail_candidates = [rv for rv in rails if rv >= 8.0]
    if drivers and motor_rail_candidates:
        motor_v = max(motor_rail_candidates)
        for drv in drivers:
            specs = drv["specs"]
            dmin = float(specs.get("supply_min_v") or 8.0)
            dmax = float(specs.get("supply_max_v") or 35.0)
            if not (dmin <= motor_v <= dmax):
                checks.append(
                    _entry(
                        f"voltage-{drv['catalog_id']}",
                        "voltage_compatibility",
                        "error",
                        f"Driver VMOT out of range on {drv['label']}",
                        f"Motor rail {motor_v:g} V outside driver limits {dmin:g}-{dmax:g} V.",
                        recommended_fix=(
                            "Select a driver rated above motor rail voltage or swap PSU."
                        ),
                        affected_ids=[drv["line_id"]],
                    )
                )
            else:
                checks.append(
                    _entry(
                        f"voltage-{drv['catalog_id']}",
                        "voltage_compatibility",
                        "pass",
                        f"Driver VMOT {motor_v:g} V valid for {drv['label']}",
                        f"{motor_v:g} V falls cleanly inside {dmin:g}-{dmax:g} V driver rating.",
                        affected_ids=[drv["line_id"]],
                    )
                )

    # 2. Motor vs driver current and channel count
    total_motors = sum(
        max(1, int(m.get("quantity") or 1)) * max(1, int(m["specs"].get("motor_count") or 1))
        for m in motors
    )
    total_channels = sum(
        max(1, int(d.get("quantity") or 1)) * max(1, int(d["specs"].get("channels") or 1))
        for d in drivers
    ) + sum(
        max(1, int(c.get("quantity") or 1)) * int(c["specs"].get("onboard_drivers") or 0)
        for c in controllers
    )
    if total_motors > 0:
        if total_channels < total_motors:
            checks.append(
                _entry(
                    "driver-channel-count",
                    "motor_driver_current",
                    "error",
                    "Insufficient stepper driver channels",
                    f"BOM specifies {total_motors} stepper motors but only {total_channels} "
                    "driver channel(s).",
                    recommended_fix=f"Increase stepper driver quantity to at least {total_motors}.",
                    affected_ids=[m["line_id"] for m in motors],
                )
            )
        else:
            max_motor_a = max(float(m["specs"].get("rated_current_a") or 1.0) for m in motors)
            cont_caps = [
                float(d["specs"].get("continuous_current_no_heatsink_a") or 1.0) for d in drivers
            ] + [
                float(c["specs"].get("onboard_driver_max_a") or 1.5)
                for c in controllers
                if int(c["specs"].get("onboard_drivers") or 0) > 0
            ]
            max_caps = [
                float(d["specs"].get("max_current_with_cooling_a") or 1.5) for d in drivers
            ] + [
                float(c["specs"].get("onboard_driver_max_a") or 2.0)
                for c in controllers
                if int(c["specs"].get("onboard_drivers") or 0) > 0
            ]
            min_cont = min(cont_caps) if cont_caps else 0.0
            min_max = min(max_caps) if max_caps else 0.0
            affected_md = [
                *(m["line_id"] for m in motors),
                *(d["line_id"] for d in drivers),
            ]
            if max_motor_a > min_max + 1e-6:
                checks.append(
                    _entry(
                        "driver-current-rating",
                        "motor_driver_current",
                        "error",
                        "Motor current exceeds maximum cooled driver limit",
                        f"Peak motor current {max_motor_a:.2f} A/phase exceeds driver maximum "
                        f"{min_max:.2f} A/phase.",
                        recommended_fix="Upgrade to DRV8825 or TMC2209 drivers (>= 1.7-2.2 A).",
                        affected_ids=affected_md,
                    )
                )
            elif max_motor_a > min_cont + 1e-6:
                checks.append(
                    _entry(
                        "driver-current-rating",
                        "motor_driver_current",
                        "warning",
                        "Stepper driver requires active cooling at motor rated current",
                        f"Motor current {max_motor_a:.2f} A/phase exceeds uncooled limit "
                        f"{min_cont:.2f} A/phase (max cooled {min_max:.2f} A). Add heatsink + fan.",
                        recommended_fix="Mount heatsinks/cooling fan or switch to DRV8825/TMC2209.",
                        affected_ids=affected_md,
                    )
                )
            else:
                checks.append(
                    _entry(
                        "driver-current-rating",
                        "motor_driver_current",
                        "pass",
                        "Motor and driver current ratings compatible",
                        f"{total_motors} motors (<= {max_motor_a:.2f} A/ph) fit {total_channels} "
                        f"driver channels (continuous rating >= {min_cont:.2f} A/ph).",
                        affected_ids=affected_md,
                    )
                )

    # 3. Power budget calculation
    if total_primary_watts > 0:
        peak_load_w = 0.0
        for m in motors:
            qty = max(1, int(m.get("quantity") or 1))
            peak_load_w += qty * float(m["specs"].get("nominal_wattage_w") or 6.0)
        for col in coolers:
            cqty = max(1, int(col.get("quantity") or 1))
            peak_load_w += cqty * float(col["specs"].get("peak_power_w") or 72.0)
        for dev in (*controllers, *cameras, *sensors):
            dqty = max(1, int(dev.get("quantity") or 1))
            peak_load_w += dqty * float(dev["specs"].get("power_draw_w") or 1.5)
        util_pct = round((peak_load_w / total_primary_watts) * 100.0, 1)
        rec_w = math.ceil(peak_load_w * 1.25)
        if peak_load_w > total_primary_watts:
            checks.append(
                _entry(
                    "power-budget",
                    "power_budget",
                    "error",
                    "Total peak DC load exceeds primary power supply capacity",
                    f"Estimated peak DC draw {peak_load_w:.1f} W exceeds PSU capacity "
                    f"{total_primary_watts:.1f} W ({util_pct}%).",
                    recommended_fix=f"Upgrade primary PSU capacity to at least {rec_w} W.",
                )
            )
        elif util_pct > 80.0:
            checks.append(
                _entry(
                    "power-budget",
                    "power_budget",
                    "warning",
                    "Power supply utilization above 80% continuous derating margin",
                    f"Peak DC load {peak_load_w:.1f} W / {total_primary_watts:.1f} W "
                    f"({util_pct}%).",
                    recommended_fix=f"Consider upgrading PSU to >= {rec_w} W for 20% headroom.",
                )
            )
        else:
            checks.append(
                _entry(
                    "power-budget",
                    "power_budget",
                    "pass",
                    "Power supply budget within 80% derated margin",
                    f"Estimated peak load {peak_load_w:.1f} W vs {total_primary_watts:.1f} W "
                    f"supply capacity ({util_pct}% utilization).",
                )
            )

    # 4. Controller I/O, logic voltage & camera interface
    if controllers or cameras:
        avail_gpio = sum(
            int(c["specs"].get("available_gpio") or 0) * max(1, int(c.get("quantity") or 1))
            for c in controllers
        )
        req_gpio = (
            sum(
                int(d["specs"].get("gpio_required") or 2) * max(1, int(d.get("quantity") or 1))
                for d in drivers
            )
            + (1 if drivers else 0)
            + sum(
                int(e["specs"].get("gpio_required_per_unit") or 1)
                * max(1, int(e.get("quantity") or 1))
                for e in endstops
            )
            + sum(
                int(s["specs"].get("gpio_required") or 1) * max(1, int(s.get("quantity") or 1))
                for s in sensors
            )
            + sum(
                int(c["specs"].get("gpio_required") or 0)
                for c in coolers
            )
        )
        if controllers and req_gpio > avail_gpio:
            checks.append(
                _entry(
                    "controller-gpio",
                    "controller_io",
                    "error",
                    "Insufficient controller GPIO pins",
                    f"Selected devices need {req_gpio} GPIO pins, "
                    f"controller provides {avail_gpio}.",
                    recommended_fix="Add an I/O expander or upgrade controller board.",
                )
            )
        elif controllers:
            checks.append(
                _entry(
                    "controller-gpio",
                    "controller_io",
                    "pass",
                    "Controller GPIO capacity sufficient",
                    f"{req_gpio} required GPIO signals fit within {avail_gpio} available pins.",
                )
            )

        req_csi = sum(int(cam["specs"].get("csi_ports_required") or 0) for cam in cameras)
        avail_csi = sum(int(ctrl["specs"].get("csi_ports") or 0) for ctrl in controllers)
        if req_csi > avail_csi:
            checks.append(
                _entry(
                    "camera-csi-interface",
                    "controller_io",
                    "error",
                    "Missing MIPI CSI-2 camera interface on controller",
                    f"Camera requires {req_csi} CSI-2 port(s), selected controller has "
                    f"{avail_csi} CSI port(s).",
                    recommended_fix=(
                        "Pair with Raspberry Pi Zero 2 W / Raspberry Pi SBC or switch camera to a "
                        "1080p USB UVC webcam."
                    ),
                    affected_ids=[c["line_id"] for c in (*cameras, *controllers)],
                )
            )
        elif req_csi > 0:
            checks.append(
                _entry(
                    "camera-csi-interface",
                    "controller_io",
                    "pass",
                    "Controller provides MIPI CSI-2 camera port",
                    f"{avail_csi} CSI-2 port(s) match {req_csi} CSI camera module(s).",
                    affected_ids=[c["line_id"] for c in (*cameras, *controllers)],
                )
            )

    # 5. Mechanical interfaces (NEMA mount pitch, shaft bore, extrusion slots)
    if mounts:
        gantry_motors = [
            m
            for m in motors
            if m.get("group_id", "").startswith(("motors", "upstream-platform:jubilee-motor"))
        ]
        for mt in mounts:
            mt_pitch = float(mt["specs"].get("motor_mounting_pitch_mm") or 31.0)
            for mot in gantry_motors:
                mot_pitch = float(mot["specs"].get("mounting_pitch_mm") or 31.0)
                if abs(mt_pitch - mot_pitch) > 0.5:
                    checks.append(
                        _entry(
                            f"mech-pitch-{mt['catalog_id']}",
                            "mechanical_interfaces",
                            "error",
                            "Stepper motor bolt-hole pitch mismatch with frame mount",
                            f"{mot['label']} uses {mot_pitch:g} mm mounting pitch "
                            f"(NEMA {mot['specs'].get('nema_size')}), incompatible with "
                            f"{mt['label']} ({mt_pitch:g} mm NEMA {mt['specs'].get('nema_size')}).",
                            recommended_fix=(
                                "Switch to a matching NEMA frame motor or "
                                "machine matching mount."
                            ),
                            affected_ids=[mt["line_id"], mot["line_id"]],
                        )
                    )
                else:
                    checks.append(
                        _entry(
                            f"mech-pitch-{mt['catalog_id']}",
                            "mechanical_interfaces",
                            "pass",
                            f"Motor NEMA {mot['specs'].get('nema_size')} mounting pitch matches",
                            f"{mot_pitch:g} mm square pitch matches {mt['label']}.",
                            affected_ids=[mt["line_id"], mot["line_id"]],
                        )
                    )

    if motors and pulleys:
        shaft_d = float(motors[0]["specs"].get("shaft_diameter_mm") or 5.0)
        for pul in pulleys:
            bore = pul["specs"].get("bore_mm") or pul["specs"].get("pulley_bore_mm") or pul[
                "specs"
            ].get("motor_bore_mm")
            if bore is not None and abs(float(bore) - shaft_d) > 0.2:
                checks.append(
                    _entry(
                        f"mech-bore-{pul['catalog_id']}",
                        "mechanical_interfaces",
                        "error",
                        "Pulley/coupler bore mismatch with motor shaft",
                        f"Motor shaft {shaft_d:g} mm does not match {pul['label']} bore "
                        f"{float(bore):g} mm.",
                        recommended_fix=f"Select pulley/coupler with {shaft_d:g} mm bore.",
                        affected_ids=[pul["line_id"]],
                    )
                )
            elif bore is not None:
                checks.append(
                    _entry(
                        f"mech-bore-{pul['catalog_id']}",
                        "mechanical_interfaces",
                        "pass",
                        f"{float(bore):g} mm drive bore matches motor shaft",
                        f"Matches {shaft_d:g} mm stepper motor shaft.",
                        affected_ids=[pul["line_id"]],
                    )
                )

    if extrusions and tnut_kits:
        ext_slots = {
            float(e["specs"].get("slot_width_mm") or 6.0)
            for e in extrusions
            if e["specs"].get("slot_width_mm")
        }
        tnut_slots = [
            float(t["specs"]["slot_width_mm"])
            for t in tnut_kits
            if t["specs"].get("slot_width_mm")
        ]
        if ext_slots and tnut_slots:
            if any(ts not in ext_slots for ts in tnut_slots):
                checks.append(
                    _entry(
                        "mech-extrusion-slot",
                        "mechanical_interfaces",
                        "warning",
                        "T-nut profile slot width mismatch",
                        "Verify T-nut slot width matches aluminum extrusion profile.",
                    )
                )
            else:
                checks.append(
                    _entry(
                        "mech-extrusion-slot",
                        "mechanical_interfaces",
                        "pass",
                        "M5 T-nuts match 6 mm 2020/2040 extrusion slots",
                        "Extrusion profile slot and M5 hardware are compatible.",
                    )
                )

    # 6. 3D Printer build volume and filament compatibility
    for pr in printed:
        pest = pr.get("print_estimate") or {}
        if pest.get("fits_printer_bed") is False:
            checks.append(
                _entry(
                    f"print-fit-{pr['catalog_id']}",
                    "printer_fabrication",
                    "error",
                    f"Printed part exceeds printer build volume: {pr['label']}",
                    str(pest.get("printer_fit_detail") or "Part exceeds configured build plate."),
                    recommended_fix="Split part geometry, shorten mast/span, or outsource print.",
                    affected_ids=[pr["line_id"]],
                )
            )
        elif pest.get("fits_printer_bed") is True:
            checks.append(
                _entry(
                    f"print-fit-{pr['catalog_id']}",
                    "printer_fabrication",
                    "pass",
                    f"Fits printer build plate: {pr['label']}",
                    str(pest.get("printer_fit_detail") or "Fits configured build volume."),
                    affected_ids=[pr["line_id"]],
                )
            )
        if pest.get("material_available_in_lab") is False:
            checks.append(
                _entry(
                    f"print-mat-{pr['catalog_id']}",
                    "printer_fabrication",
                    "warning",
                    f"Filament {pest.get('material')} not listed in Lab Profile",
                    f"{pr['label']} specifies {pest.get('material')}, missing from configured "
                    "printer materials.",
                    recommended_fix="Switch print material or add filament spool to Lab Profile.",
                    affected_ids=[pr["line_id"]],
                )
            )

    for row in lines:
        part = (row.get("part_choice") or {}).get("accepted") or row.get("reference_candidate")
        if not part:
            continue
        for index, check in enumerate(part["checks"]):
            actual = check["actual"] if check["actual"] is not None else "unspecified"
            checks.append(
                _entry(
                    f"requirement-{row['line_id']}-{index}",
                    "part_requirement",
                    {"match": "pass", "unknown": "warning", "mismatch": "error"}[check["status"]],
                    f"{part['label']} · {check['key'].replace('_', ' ')}",
                    f"Required {check['comparison'].replace('_', ' ')} "
                    f"{check['required']} {check['unit']}. "
                    f"Recorded value: {actual}. Evidence: {part['basis'].replace('_', ' ')}.",
                    affected_ids=[row["line_id"]],
                )
            )
        if len(part["checks"]) == 1:
            checks.append(
                _entry(
                    f"interfaces-{row['line_id']}",
                    "part_requirement",
                    "warning",
                    "Component interfaces need review",
                    f"No critical interface specifications are recorded for {part['label']}.",
                    affected_ids=[row["line_id"]],
                )
            )
    missing = [row for row in lines if row.get("resolution_status") == "selection_required"]
    if missing:
        checks.append(_entry(
            "unresolved-parts", "component_identity", "warning", "Component selection required",
            f"Identify parts for {len(missing)} requirement(s) to check compatibility.",
            affected_ids=[row["line_id"] for row in missing],
        ))
    err_n = sum(1 for c in checks if c["severity"] == "error")
    warn_n = sum(1 for c in checks if c["severity"] == "warning")
    pass_n = sum(1 for c in checks if c["severity"] == "pass")
    status = "incompatible" if err_n > 0 else ("warnings" if warn_n > 0 else "compatible")
    if not checks:
        status = "not_evaluated"
    summary = (
        f"Hardware compatibility: {status} ({pass_n} passed, {warn_n} warning(s), "
        f"{err_n} error(s))."
    )
    if status == "not_evaluated":
        summary = "No applicable component checks are available for this design."
    return {
        "schema_version": 1,
        "project_id": project["id"],
        "project_revision": project["revision"],
        "status": status,
        "error_count": err_n,
        "warning_count": warn_n,
        "pass_count": pass_n,
        "summary": summary,
        "checks": checks,
        "scope_disclaimer": (
            "Checks verify cataloged nominal ratings (voltage rails, continuous/cooled current, "
            "power margin, GPIO/CSI ports, NEMA pitch, shaft bore, bed fit); physical wiring, "
            "EMI, thermal dissipation, and firmware safety remain to be commissioned."
        ),
    }
