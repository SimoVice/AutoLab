"""Direct dilution planning with strict unit handling.

This module computes one separate tube per target (no serial dilutions).
All arithmetic uses Decimal; results are returned as JSON-compatible floats.
Volumes are calculated values only and require hardware validation.
"""

from __future__ import annotations

import math
import re
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext

from app.quantities import normalize_concentration, normalize_targets, normalize_volume

MAX_INPUT_CHARS = 1000
MAX_PROTOCOL_INPUT_CHARS = 4000
MAX_TARGETS = 6
MAX_PROTOCOL_TARGETS = 96
MAX_RAW_MAGNITUDE = Decimal("1E12")
MAX_VOLUME_UL = Decimal("1E12")
MAX_BASE_CONC = Decimal("1E12")
PLATE_96_WELL_ORDER = [
    f"{row}{col}" for col in range(1, 13) for row in ("A", "B", "C", "D", "E", "F", "G", "H")
]

# Conversion to common base units using Decimal for exact arithmetic.
MOLAR_TO_M = {
    "M": Decimal("1"),
    "mM": Decimal("0.001"),
    "uM": Decimal("0.000001"),
    "nM": Decimal("0.000000001"),
}
MASS_TO_G_PER_L = {
    "g/L": Decimal("1"),
    "mg/mL": Decimal("1"),
    "ug/mL": Decimal("0.001"),
}
VOLUME_TO_UL = {
    "mL": Decimal("1000"),
    "uL": Decimal("1"),
    "nL": Decimal("0.001"),
}

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_CONC_PATTERN = re.compile(rf"^(?P<number>{_NUMBER})\s*(?P<unit>[^\s]+)\s*$")
_VOLUME_PATTERN = re.compile(rf"^(?P<number>{_NUMBER})\s*(?P<unit>[^\s]+)\s*$")

_MICRO_SIGNS = ("\u00b5", "\u03bc")


def _normalize_micro(text: str) -> str:
    for ch in _MICRO_SIGNS:
        text = text.replace(ch, "u")
    return text


def _empty_result(status: str, message: str) -> dict:
    for field, label in (
        ("stock_concentration", "stock concentration"),
        ("target_concentrations", "target concentrations"),
        ("final_volume", "final volume"),
    ):
        message = message.replace(field, label)
    return {
        "status": status,
        "message": message,
        "stock": None,
        "final_volume_ul": None,
        "steps": [],
        "assumptions": [],
    }


def _parse_decimal_number(num_str: str, what: str) -> Decimal:
    lowered = num_str.strip().lower()
    if lowered in (
        "nan",
        "+nan",
        "-nan",
        "inf",
        "+inf",
        "-inf",
        "infinity",
        "+infinity",
        "-infinity",
    ):
        raise ValueError(f"{what} must be a finite number.")
    if len(num_str) > 100:
        raise ValueError(f"{what} numeric text is too long.")
    try:
        value = Decimal(num_str)
    except (InvalidOperation, ValueError, ArithmeticError):
        raise ValueError(f"{what} has an unsupported numeric value: {num_str!r}.")
    if not value.is_finite():
        raise ValueError(f"{what} must be a finite number.")
    if abs(value) > MAX_RAW_MAGNITUDE:
        raise ValueError(f"{what} magnitude is out of the supported range.")
    tup = value.as_tuple()
    # Bound exponent to avoid resource exhaustion from extreme scientific notation.
    if tup.exponent < -1000 or tup.exponent > 1000:
        raise ValueError(f"{what} magnitude is out of the supported range.")
    # Bound significant digits to avoid resource exhaustion.
    if len(tup.digits) > 50:
        raise ValueError(f"{what} has too many significant digits.")
    return value


def _parse_concentration(text: str, what: str) -> tuple[Decimal, str, str, Decimal]:
    """Return (raw value, unit, family, base value)."""
    normalized = _normalize_micro(text.strip())
    match = _CONC_PATTERN.match(normalized)
    if not match:
        raise ValueError(
            f"{what} must be a number followed by a supported unit "
            "(M, mM, uM, nM, g/L, mg/mL, ug/mL); got {text.strip()!r}."
        )
    num_str = match.group("number")
    unit = match.group("unit")
    # Full-string match already enforced; unit must be an exact supported token.
    if unit in MOLAR_TO_M:
        family = "molar"
        factor = MOLAR_TO_M[unit]
    elif unit in MASS_TO_G_PER_L:
        family = "mass"
        factor = MASS_TO_G_PER_L[unit]
    else:
        raise ValueError(
            f"{what} has an unsupported unit {unit!r}. Supported: M, mM, uM, nM, g/L, mg/mL, ug/mL."
        )
    value = _parse_decimal_number(num_str, what)
    with localcontext() as ctx:
        ctx.prec = 80
        base = value * factor
    if abs(base) > MAX_BASE_CONC:
        raise ValueError(f"{what} magnitude is out of the supported range.")
    return value, unit, family, base


def _parse_volume(text: str) -> tuple[Decimal, Decimal]:
    """Return (raw value, volume in uL)."""
    normalized = _normalize_micro(text.strip())
    match = _VOLUME_PATTERN.match(normalized)
    if not match:
        raise ValueError(
            f"final_volume must be a number followed by a supported unit "
            f"(mL, uL, nL); got {text.strip()!r}."
        )
    num_str = match.group("number")
    unit = match.group("unit")
    if unit not in VOLUME_TO_UL:
        raise ValueError(f"final_volume has an unsupported unit {unit!r}. Supported: mL, uL, nL.")
    value = _parse_decimal_number(num_str, "final_volume")
    with localcontext() as ctx:
        ctx.prec = 80
        volume_ul = value * VOLUME_TO_UL[unit]
    if abs(volume_ul) > MAX_VOLUME_UL:
        raise ValueError("final_volume magnitude is out of the supported range.")
    return value, volume_ul


def create_dilution_plan(
    stock_concentration: str | None,
    target_concentrations: str | None,
    final_volume: str | None,
) -> dict:
    """Create a direct-dilution plan with one tube per target.

    Returns a dict with keys status, message, stock, final_volume_ul,
    steps and assumptions following the shared contract.
    """
    missing: list[str] = []
    for name, val in (
        ("stock_concentration", stock_concentration),
        ("target_concentrations", target_concentrations),
        ("final_volume", final_volume),
    ):
        if val is None:
            missing.append(name)
        elif isinstance(val, str) and val.strip() == "":
            missing.append(name)
    if missing:
        return _empty_result(
            "incomplete",
            "Missing required parameters: "
            + ", ".join(missing)
            + ". Provide stock_concentration (e.g. '10 mM'), "
            "target_concentrations (e.g. '1 mM, 2 mM') and "
            "final_volume (e.g. '1 mL').",
        )

    # Type check: all present values must be text.
    for name, val in (
        ("stock_concentration", stock_concentration),
        ("target_concentrations", target_concentrations),
        ("final_volume", final_volume),
    ):
        if not isinstance(val, str):
            return _empty_result(
                "invalid",
                f"{name} must be text such as '10 mM' or '1 mL'.",
            )
        if len(val) > MAX_INPUT_CHARS:
            return _empty_result(
                "invalid",
                f"{name} exceeds the {MAX_INPUT_CHARS} character limit.",
            )

    assert isinstance(stock_concentration, str)
    assert isinstance(target_concentrations, str)
    assert isinstance(final_volume, str)

    stock_text = stock_concentration.strip()
    targets_text = target_concentrations.strip()
    volume_text = final_volume.strip()

    try:
        stock_text = normalize_concentration(stock_text)
        targets_text = normalize_targets(targets_text)
        volume_text = normalize_volume(volume_text)
    except ValueError as exc:
        return _empty_result("invalid", str(exc))

    try:
        _, _, stock_family, stock_base = _parse_concentration(stock_text, "stock_concentration")
    except ValueError as exc:
        return _empty_result("invalid", str(exc))

    # Stock must be positive.
    if stock_base <= 0:
        return _empty_result(
            "invalid",
            "stock_concentration must be greater than zero.",
        )

    try:
        _, volume_ul = _parse_volume(volume_text)
    except ValueError as exc:
        return _empty_result("invalid", str(exc))

    if volume_ul <= 0:
        return _empty_result(
            "invalid",
            "final_volume must be greater than zero.",
        )

    # Split targets on comma or semicolon; every entry must be fully specified.
    raw_parts = re.split(r"[;,]", targets_text)
    target_tokens: list[str] = [p.strip() for p in raw_parts]
    if len(target_tokens) > MAX_TARGETS:
        return _empty_result(
            "invalid",
            f"Too many targets (maximum {MAX_TARGETS}).",
        )
    # Detect empty entries from leading/trailing/double delimiters.
    for token in target_tokens:
        if token == "":
            return _empty_result(
                "invalid",
                "target_concentrations contains an empty entry; "
                "use comma or semicolon separated values such as '1 mM, 2 mM'.",
            )

    parsed_targets: list[tuple[str, Decimal]] = []
    try:
        for token in target_tokens:
            _, _, family, base = _parse_concentration(token, f"target {token!r}")
            if family != stock_family:
                return _empty_result(
                    "invalid",
                    f"Incompatible units: stock is {stock_family} family but "
                    f"target {token!r} is {family} family. "
                    "Molar (M, mM, uM, nM) and mass (g/L, mg/mL, ug/mL) "
                    "concentrations cannot be mixed.",
                )
            if base < 0:
                return _empty_result(
                    "invalid",
                    f"Target {token!r} must not be negative.",
                )
            if base > stock_base:
                return _empty_result(
                    "invalid",
                    f"Target {token!r} exceeds stock_concentration "
                    f"{stock_text!r}; direct dilution requires target <= stock.",
                )
            parsed_targets.append((token, base))
    except ValueError as exc:
        return _empty_result("invalid", str(exc))

    if not parsed_targets:
        return _empty_result(
            "invalid",
            "target_concentrations must contain at least one value such as '1 mM'.",
        )

    steps: list[dict] = []
    for index, (token, base) in enumerate(parsed_targets):
        with localcontext() as ctx:
            ctx.prec = 80
            stock_vol = base / stock_base * volume_ul
            diluent_vol = volume_ul - stock_vol
        try:
            stock_f = float(stock_vol)
            diluent_f = float(diluent_vol)
            total_f = float(volume_ul)
        except (OverflowError, ValueError):
            return _empty_result(
                "invalid",
                "Computed volumes are out of the supported range.",
            )
        if not (math.isfinite(stock_f) and math.isfinite(diluent_f) and math.isfinite(total_f)):
            return _empty_result(
                "invalid",
                "Computed volumes are nonfinite and cannot be represented.",
            )
        if any(
            original != 0 and converted == 0
            for original, converted in (
                (stock_vol, stock_f),
                (diluent_vol, diluent_f),
                (volume_ul, total_f),
            )
        ):
            return _empty_result("invalid", "Computed volumes are too small to represent.")
        # Guard against negative zero artefacts and tiny negative drift.
        if stock_f < 0 and stock_f > -1e-18:
            stock_f = 0.0
        if diluent_f < 0 and diluent_f > -1e-18:
            diluent_f = 0.0
        if stock_f < 0 or diluent_f < 0:
            return _empty_result(
                "invalid",
                f"Computed volumes for target {token!r} are invalid.",
            )
        steps.append(
            {
                "id": str(index + 1),
                "target": token,
                "stock_volume_ul": stock_f,
                "diluent_volume_ul": diluent_f,
                "total_volume_ul": total_f,
            }
        )

    try:
        final_volume_float = float(volume_ul)
    except (OverflowError, ValueError):
        return _empty_result(
            "invalid",
            "final_volume is out of the supported range.",
        )
    if not math.isfinite(final_volume_float):
        return _empty_result(
            "invalid",
            "final_volume is nonfinite and cannot be represented.",
        )

    assumptions = [
        "Direct dilution: each target is prepared in a separate tube from stock; "
        "no serial dilutions are used.",
        "Stock volume = target / stock × final volume; diluent = final volume − stock volume.",
        "Concentrations are converted to common units and volumes to µL.",
        "Verify tube capacity, pipette range, mixing and hardware before use; "
        "the preparation is not laboratory validated.",
    ]

    return {
        "status": "ready",
        "message": (
            f"Dilution plan ready for {len(steps)} target(s) at "
            f"{final_volume_float} uL final volume per tube."
        ),
        "stock": stock_text,
        "final_volume_ul": final_volume_float,
        "steps": steps,
        "assumptions": assumptions,
    }


def _protocol_empty(status: str, message: str) -> dict:
    base = _empty_result(status, message)
    return {
        **base,
        "mode": "direct",
        "min_transfer_ul": 1.0,
        "target_count": 0,
        "intermediate_steps": [],
        "target_steps": [],
        "transfers": [],
        "multi_dispense_groups": [],
        "totals": {
            "stock_ul": 0.0,
            "diluent_ul": 0.0,
            "single_mode_tips": 0,
            "multi_dispense_tips": 0,
            "tips_saved": 0,
        },
        "warnings": [],
    }


def _format_base_conc(base: Decimal, family: str) -> str:
    if family == "molar":
        choices = [("M", Decimal("1")), ("mM", Decimal("0.001")),
                   ("uM", Decimal("0.000001")), ("nM", Decimal("0.000000001"))]
    else:
        choices = [("mg/mL", Decimal("1")), ("ug/mL", Decimal("0.001"))]
    for unit, scale in choices:
        scaled = base / scale
        if scaled >= Decimal("1") or unit == choices[-1][0]:
            text = f"{scaled.normalize():f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return f"{text} {unit}"
    return f"{base} {choices[0][0]}"


def _round_ul(value: Decimal) -> float:
    return round(float(value), 4)


def _group_multi_dispense(
    transfers: list[dict],
    *,
    max_pipette_ul: Decimal,
    disposal_volume_ul: Decimal,
    min_transfer_ul: Decimal,
) -> tuple[list[dict], int]:
    """Batch compatible clean diluent transfers into aspirate-once multi-dispense groups."""
    groups: list[dict] = []
    tips_used = 0
    # Split diluent transfers by pipette class so every dispense stays inside valid pipette range.
    p20_items = [t for t in transfers if Decimal(str(t["volume_ul"])) < Decimal("20")]
    p300_items = [
        t for t in transfers
        if Decimal("20") <= Decimal(str(t["volume_ul"])) <= Decimal("300")
    ]
    p1000_items = [t for t in transfers if Decimal(str(t["volume_ul"])) > Decimal("300")]

    p300_cap = min(max_pipette_ul, Decimal("300"))
    for bracket, cap, min_disp, disp_pad, items in (
        ("p20", Decimal("20"), min_transfer_ul, Decimal("1"), p20_items),
        ("p300", p300_cap, Decimal("20"), disposal_volume_ul, p300_items),
        ("p1000", Decimal("1000"), Decimal("100"), disposal_volume_ul, p1000_items),
    ):
        _ = min_disp
        current: list[dict] = []
        running = Decimal("0")
        for item in items:
            vol = Decimal(str(item["volume_ul"]))
            usable_cap = cap - disp_pad
            if vol > usable_cap:
                if current:
                    tips_used += 1
                    groups.append(
                        {
                            "group_id": f"md-{len(groups) + 1}",
                            "pipette_class": bracket,
                            "source_role": "diluent",
                            "source_well": "A1",
                            "aspirate_ul": _round_ul(running + disp_pad),
                            "disposal_ul": _round_ul(disp_pad),
                            "dispenses": current,
                        }
                    )
                    current = []
                    running = Decimal("0")
                tips_used += 1
                groups.append(
                    {
                        "group_id": f"md-{len(groups) + 1}",
                        "pipette_class": bracket,
                        "source_role": "diluent",
                        "source_well": "A1",
                        "aspirate_ul": _round_ul(vol),
                        "disposal_ul": 0.0,
                        "dispenses": [item],
                    }
                )
                continue
            if current and running + vol > usable_cap:
                tips_used += 1
                groups.append(
                    {
                        "group_id": f"md-{len(groups) + 1}",
                        "pipette_class": bracket,
                        "source_role": "diluent",
                        "source_well": "A1",
                        "aspirate_ul": _round_ul(running + disp_pad),
                        "disposal_ul": _round_ul(disp_pad),
                        "dispenses": current,
                    }
                )
                current = []
                running = Decimal("0")
            current.append(item)
            running += vol
        if current:
            tips_used += 1
            pad = disp_pad if len(current) > 1 else Decimal("0")
            groups.append(
                {
                    "group_id": f"md-{len(groups) + 1}",
                    "pipette_class": bracket,
                    "source_role": "diluent",
                    "source_well": "A1",
                    "aspirate_ul": _round_ul(running + pad),
                    "disposal_ul": _round_ul(pad),
                    "dispenses": current,
                }
            )
    return groups, tips_used


def create_protocol_dilution_plan(
    stock_concentration: str | None,
    target_concentrations: str | None,
    final_volume: str | None,
    *,
    max_targets: int = MAX_PROTOCOL_TARGETS,
    min_transfer_ul: float = 1.0,
    max_pipette_ul: float = 300.0,
    allow_serial: bool = True,
    allow_multi_dispense: bool = True,
    disposal_volume_ul: float = 5.0,
    well_ordering: list[str] | None = None,
) -> dict:
    """Create a 1..96-target dilution protocol supporting serial dilutions and multi-dispense."""
    if not 1 <= max_targets <= MAX_PROTOCOL_TARGETS:
        raise ValueError(f"max_targets must be between 1 and {MAX_PROTOCOL_TARGETS}.")
    if not math.isfinite(min_transfer_ul) or min_transfer_ul <= 0:
        raise ValueError("min_transfer_ul must be a finite positive number.")
    if not math.isfinite(max_pipette_ul) or max_pipette_ul < min_transfer_ul:
        raise ValueError("max_pipette_ul must be >= min_transfer_ul.")
    if not math.isfinite(disposal_volume_ul) or disposal_volume_ul < 0:
        raise ValueError("disposal_volume_ul must be nonnegative.")

    missing = [
        name
        for name, val in (
            ("stock_concentration", stock_concentration),
            ("target_concentrations", target_concentrations),
            ("final_volume", final_volume),
        )
        if val is None or (isinstance(val, str) and val.strip() == "")
    ]
    if missing:
        return _protocol_empty(
            "incomplete",
            "Missing required parameters: "
            + ", ".join(missing)
            + ". Provide stock_concentration, target_concentrations, and final_volume.",
        )

    for name, val in (
        ("stock_concentration", stock_concentration),
        ("target_concentrations", target_concentrations),
        ("final_volume", final_volume),
    ):
        if not isinstance(val, str):
            return _protocol_empty("invalid", f"{name} must be text such as '10 mM'.")
        limit = MAX_PROTOCOL_INPUT_CHARS if name == "target_concentrations" else MAX_INPUT_CHARS
        if len(val) > limit:
            return _protocol_empty("invalid", f"{name} exceeds the {limit} character limit.")

    assert isinstance(stock_concentration, str)
    assert isinstance(target_concentrations, str)
    assert isinstance(final_volume, str)

    try:
        stock_text = normalize_concentration(stock_concentration.strip())
        targets_text = normalize_targets(
            target_concentrations.strip(),
            max_targets=max_targets,
            max_chars=MAX_PROTOCOL_INPUT_CHARS,
        )
        volume_text = normalize_volume(final_volume.strip())
        _, _, stock_family, stock_base = _parse_concentration(stock_text, "stock_concentration")
        if stock_base <= 0:
            raise ValueError("stock_concentration must be greater than zero.")
        _, volume_ul = _parse_volume(volume_text)
        if volume_ul <= 0:
            raise ValueError("final_volume must be greater than zero.")
    except ValueError as exc:
        return _protocol_empty("invalid", str(exc))

    target_tokens = [p.strip() for p in re.split(r"[;,]", targets_text) if p.strip()]
    if len(target_tokens) > max_targets:
        return _protocol_empty("invalid", f"Too many targets (maximum {max_targets}).")

    parsed_targets: list[tuple[str, Decimal]] = []
    for token in target_tokens:
        try:
            _, _, family, base = _parse_concentration(token, f"target {token!r}")
        except ValueError as exc:
            return _protocol_empty("invalid", str(exc))
        if family != stock_family:
            return _protocol_empty(
                "invalid",
                f"Incompatible units: stock is {stock_family} but target {token!r} is {family}.",
            )
        if base < 0:
            return _protocol_empty("invalid", f"Target {token!r} must not be negative.")
        if base > stock_base:
            return _protocol_empty(
                "invalid",
                f"Target {token!r} exceeds stock_concentration {stock_text!r}.",
            )
        parsed_targets.append((token, base))

    slots = list(well_ordering or PLATE_96_WELL_ORDER)
    if len(parsed_targets) > len(slots):
        return _protocol_empty(
            "invalid",
            f"Target count ({len(parsed_targets)}) exceeds available wells ({len(slots)}).",
        )

    min_t = Decimal(str(min_transfer_ul))
    max_p = Decimal(str(max_pipette_ul))
    disposal_d = Decimal(str(disposal_volume_ul))
    if volume_ul >= min_t * Decimal("10"):
        step_factor = Decimal("10")
    else:
        ratio = (volume_ul / min_t).to_integral_value(rounding=ROUND_FLOOR)
        step_factor = max(Decimal("2"), ratio)

    # Determine required serial intermediate index k for each target.
    target_k: list[int] = []
    warnings: list[str] = []
    for token, base in parsed_targets:
        if base == 0:
            target_k.append(0)
            continue
        k = 0
        with localcontext() as ctx:
            ctx.prec = 80
            while True:
                int_conc = stock_base / (step_factor ** k)
                transfer = (base / int_conc) * volume_ul
                if transfer >= min_t:
                    break
                if k >= 12:
                    return _protocol_empty(
                        "invalid",
                        f"Target {token!r} requires more than 12 serial dilution stages.",
                    )
                k += 1
                if base > stock_base / (step_factor ** k):
                    # Step back and accept best reachable transfer volume if step_factor overshoots.
                    k -= 1
                    break
        if k > 0 and not allow_serial:
            with localcontext() as ctx:
                ctx.prec = 80
                direct = (base / stock_base) * volume_ul
            return _protocol_empty(
                "invalid",
                f"Target {token!r} requires {float(direct):.4g} uL direct stock transfer "
                f"(< min_transfer_ul={min_transfer_ul} uL). Enable serial dilution.",
            )
        target_k.append(k)

    max_k = max(target_k, default=0)
    mode = "serial" if max_k > 0 else "direct"

    # Allocate intermediate Falcon-rack tubes (slot 2: A1=stock, B1.. = intermediates).
    int_tube_slots = [
        f"{row}{col}"
        for col in range(1, 6)
        for row in ("A", "B", "C")
        if not (row == "A" and col == 1)
    ]
    if max_k > len(int_tube_slots):
        return _protocol_empty(
            "invalid",
            f"Serial chain needs {max_k} tubes; source rack holds {len(int_tube_slots)}.",
        )

    # Compute exact target transfers first so backward intermediate demand is exact.
    intermediate_demand: dict[int, Decimal] = {k: Decimal("0") for k in range(1, max_k + 1)}
    target_steps: list[dict] = []
    for index, ((token, base), k) in enumerate(zip(parsed_targets, target_k)):
        well = slots[index]
        with localcontext() as ctx:
            ctx.prec = 80
            src_conc = stock_base / (step_factor ** k)
            src_vol = (base / src_conc) * volume_ul
            dil_vol = volume_ul - src_vol
        if dil_vol > 0 and dil_vol < min_t:
            warnings.append(
                f"Target {token} ({well}) has diluent volume {_round_ul(dil_vol)} uL "
                f"< min_transfer_ul={min_transfer_ul} uL."
            )
        src_role = "stock" if k == 0 else f"intermediate_{k}"
        src_well = "A1" if k == 0 else int_tube_slots[k - 1]
        if k > 0:
            intermediate_demand[k] += src_vol
        target_steps.append(
            {
                "id": str(index + 1),
                "target": token,
                "well_name": well,
                "source_role": src_role,
                "source_well": src_well,
                "serial_stage": k,
                "source_volume_ul": _round_ul(src_vol),
                "stock_volume_ul": _round_ul(src_vol if k == 0 else Decimal("0")),
                "diluent_volume_ul": _round_ul(dil_vol),
                "total_volume_ul": _round_ul(volume_ul),
                "_src_vol_dec": src_vol,
                "_dil_vol_dec": dil_vol,
            }
        )

    intermediate_steps: list[dict] = []
    upstream_pull = Decimal("0")
    raw_ints: dict[int, dict] = {}
    for k in range(max_k, 0, -1):
        with localcontext() as ctx:
            ctx.prec = 80
            drawn = intermediate_demand[k] + upstream_pull
            dead_vol = Decimal("100")
            prep_vol = max(Decimal("1000"), drawn + dead_vol)
            if prep_vol > Decimal("13500"):
                return _protocol_empty(
                    "invalid",
                    f"Intermediate stage {k} volume ({float(prep_vol):.1f} uL) exceeds 15 mL tube.",
                )
            src_vol = prep_vol / step_factor
            dil_vol = prep_vol - src_vol
            conc_base = stock_base / (step_factor ** k)
        upstream_pull = src_vol
        parent_role = "stock" if k == 1 else f"intermediate_{k - 1}"
        parent_well = "A1" if k == 1 else int_tube_slots[k - 2]
        raw_ints[k] = {
            "stage": k,
            "id": f"INT_{k}",
            "concentration": _format_base_conc(conc_base, stock_family),
            "tube_well": int_tube_slots[k - 1],
            "source_role": parent_role,
            "source_well": parent_well,
            "dilution_factor": float(step_factor),
            "source_volume_ul": _round_ul(src_vol),
            "diluent_volume_ul": _round_ul(dil_vol),
            "prepared_volume_ul": _round_ul(prep_vol),
            "drawn_volume_ul": _round_ul(drawn),
            "_src_vol_dec": src_vol,
            "_dil_vol_dec": dil_vol,
        }

    for k in range(1, max_k + 1):
        intermediate_steps.append(raw_ints[k])

    # Build ordered executable transfer operations:
    # Phase 1: all clean diluent transfers into empty vessels (eligible for multi-dispense).
    transfers: list[dict] = []
    diluent_transfers: list[dict] = []
    total_diluent = Decimal("0")
    for ist in intermediate_steps:
        dil = ist["_dil_vol_dec"]
        if dil > 0:
            total_diluent += dil
            op = {
                "phase": "diluent",
                "source_role": "diluent",
                "source_well": "A1",
                "dest_role": ist["id"],
                "dest_well": ist["tube_well"],
                "volume_ul": _round_ul(dil),
                "mix_after": False,
                "target": f"Intermediate {ist['stage']} ({ist['concentration']})",
            }
            transfers.append(op)
            diluent_transfers.append(op)

    for tst in target_steps:
        dil = tst["_dil_vol_dec"]
        if dil > 0:
            total_diluent += dil
            op = {
                "phase": "diluent",
                "source_role": "diluent",
                "source_well": "A1",
                "dest_role": "destination_plate",
                "dest_well": tst["well_name"],
                "volume_ul": _round_ul(dil),
                "mix_after": False,
                "target": tst["target"],
            }
            transfers.append(op)
            diluent_transfers.append(op)

    # Phase 2: serial intermediates in ascending order k=1..max_k (with mix).
    total_stock = Decimal("0")
    for ist in intermediate_steps:
        src = ist["_src_vol_dec"]
        if ist["stage"] == 1:
            total_stock += src
        transfers.append(
            {
                "phase": "serial_intermediate",
                "source_role": ist["source_role"],
                "source_well": ist["source_well"],
                "dest_role": ist["id"],
                "dest_well": ist["tube_well"],
                "volume_ul": _round_ul(src),
                "mix_after": True,
                "mix_volume_ul": _round_ul(
                    min(Decimal(str(ist["prepared_volume_ul"])) * Decimal("0.5"), Decimal("300"))
                ),
                "target": f"Intermediate {ist['stage']} ({ist['concentration']})",
            }
        )

    # Phase 3: stock/intermediate transfers into destination wells (with mix).
    for tst in target_steps:
        src = tst["_src_vol_dec"]
        if src <= 0:
            continue
        if tst["serial_stage"] == 0:
            total_stock += src
        transfers.append(
            {
                "phase": "sample_transfer",
                "source_role": tst["source_role"],
                "source_well": tst["source_well"],
                "dest_role": "destination_plate",
                "dest_well": tst["well_name"],
                "volume_ul": _round_ul(src),
                "mix_after": Decimal(str(tst["diluent_volume_ul"])) > 0,
                "mix_volume_ul": _round_ul(min(volume_ul * Decimal("0.5"), Decimal("300"))),
                "target": tst["target"],
            }
        )

    # Clean internal Decimal keys from serialized output.
    clean_intermediates = [
        {k: v for k, v in ist.items() if not k.startswith("_")}
        for ist in intermediate_steps
    ]
    clean_targets = [
        {k: v for k, v in tst.items() if not k.startswith("_")}
        for tst in target_steps
    ]

    non_diluent_tips = sum(1 for t in transfers if t["phase"] != "diluent")
    single_mode_tips = len(transfers)
    if allow_multi_dispense and diluent_transfers:
        md_groups, md_tips = _group_multi_dispense(
            diluent_transfers,
            max_pipette_ul=max_p,
            disposal_volume_ul=disposal_d,
            min_transfer_ul=min_t,
        )
        multi_dispense_tips = md_tips + non_diluent_tips
    else:
        md_groups = []
        multi_dispense_tips = single_mode_tips

    final_vol_f = _round_ul(volume_ul)
    assumptions = [
        f"Mode: {mode} across {len(clean_targets)} targets (min_transfer={min_transfer_ul:g} uL).",
        "All clean diluent transfers run before sample additions to permit safe multi-dispense.",
        "Concentrations obey C1 * V1 = C2 * V2 in high-precision Decimal arithmetic.",
        "Deck coordinates, tip racks and pipette calibration require hardware verification.",
    ]
    return {
        "status": "ready",
        "mode": mode,
        "message": (
            f"Protocol dilution plan ready ({mode} mode, {len(clean_targets)} targets, "
            f"{len(clean_intermediates)} serial intermediate(s), "
            f"{multi_dispense_tips} vs {single_mode_tips} single-transfer tips)."
        ),
        "stock": stock_text,
        "final_volume_ul": final_vol_f,
        "min_transfer_ul": float(min_transfer_ul),
        "target_count": len(clean_targets),
        "intermediate_steps": clean_intermediates,
        "target_steps": clean_targets,
        "steps": [
            {
                "id": t["id"],
                "target": t["target"],
                "well_name": t["well_name"],
                "stock_volume_ul": t["stock_volume_ul"],
                "diluent_volume_ul": t["diluent_volume_ul"],
                "total_volume_ul": t["total_volume_ul"],
            }
            for t in clean_targets
        ],
        "transfers": transfers,
        "multi_dispense_groups": md_groups,
        "totals": {
            "stock_ul": _round_ul(total_stock),
            "diluent_ul": _round_ul(total_diluent),
            "single_mode_tips": single_mode_tips,
            "multi_dispense_tips": multi_dispense_tips,
            "tips_saved": max(0, single_mode_tips - multi_dispense_tips),
        },
        "assumptions": assumptions,
        "warnings": warnings,
    }
