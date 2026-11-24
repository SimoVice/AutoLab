"""Strict AST, physical-constraint, and subprocess verification for generated robot protocols."""

from __future__ import annotations

import ast
import json
import math
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.dilution import PLATE_96_WELL_ORDER
from app.protocol_generators import PIPETTE_SPECS, SUPPORTED_OT2_LABWARE

_FORBIDDEN_IMPORTS = {
    "os",
    "sys",
    "subprocess",
    "shutil",
    "socket",
    "http",
    "urllib",
    "pathlib",
    "ctypes",
    "importlib",
}
_FORBIDDEN_CALLS = {"eval", "exec", "open", "compile", "__import__", "breakpoint"}


@dataclass
class _SimWell:
    label: str
    capacity_ul: float
    current_volume_ul: float = 0.0
    is_tiprack: bool = False
    has_tip: bool = False

    def top(self, z: float = 0.0) -> _SimWell:  # noqa: ARG002
        return self

    def bottom(self, z: float = 0.0) -> _SimWell:  # noqa: ARG002
        return self


class _SimLabware:
    def __init__(self, load_name: str, slot: str) -> None:
        if load_name not in SUPPORTED_OT2_LABWARE:
            raise ValueError(f"Unknown labware load name: {load_name!r}.")
        spec = SUPPORTED_OT2_LABWARE[load_name]
        self.load_name = load_name
        self.slot = slot
        self.is_tiprack = spec["kind"] == "tiprack"
        if "wells" in spec:
            order = list(spec["wells"])
        elif spec["well_count"] == 96:
            order = list(PLATE_96_WELL_ORDER)
        else:
            order = [f"A{i}" for i in range(1, spec["well_count"] + 1)]
        cap = float(spec.get("well_capacity_ul", spec.get("max_volume_ul", 300.0)))
        self._order = order
        self._wells: dict[str, _SimWell] = {
            name: _SimWell(
                label=name,
                capacity_ul=cap,
                is_tiprack=self.is_tiprack,
                has_tip=self.is_tiprack,
            )
            for name in order
        }

    def __getitem__(self, key: str) -> _SimWell:
        if key not in self._wells:
            raise ValueError(f"Well {key!r} does not exist on {self.load_name} (slot {self.slot}).")
        return self._wells[key]

    def wells(self) -> list[_SimWell]:
        return [self._wells[name] for name in self._order]

    def next_tip(self) -> _SimWell | None:
        for name in self._order:
            well = self._wells[name]
            if well.has_tip:
                return well
        return None


@dataclass
class _SimInstrumentContext:
    name: str
    mount: str
    min_volume: float
    max_volume: float
    tip_racks: list[_SimLabware]
    protocol: _SimProtocolContext
    has_tip: bool = False
    current_volume: float = 0.0
    tips_picked: int = 0
    total_aspirated_ul: float = 0.0
    total_dispensed_ul: float = 0.0
    steps: int = 0

    def _require_volume(self, volume: float, *, action: str) -> float:
        if (
            isinstance(volume, bool)
            or not isinstance(volume, (int, float))
            or not math.isfinite(volume)
        ):
            raise ValueError(f"{self.name} {action} volume must be finite numeric, got {volume!r}.")
        val = float(volume)
        if val < self.min_volume or val > self.max_volume:
            raise ValueError(
                f"{self.name} {action} volume {val:g} uL outside range "
                f"[{self.min_volume:g}, {self.max_volume:g}] uL."
            )
        return val

    def pick_up_tip(self, location: _SimWell | None = None) -> None:
        if self.has_tip:
            raise ValueError(f"{self.name} already holds a tip; drop_tip() before pick_up_tip().")
        if location is not None:
            if not location.is_tiprack or not location.has_tip:
                raise ValueError(f"No tip available at {location.label}.")
            location.has_tip = False
        else:
            chosen = None
            for rack in self.tip_racks:
                chosen = rack.next_tip()
                if chosen is not None:
                    break
            if chosen is None:
                raise ValueError(f"{self.name} ran out of tips across all loaded tip racks.")
            chosen.has_tip = False
        self.has_tip = True
        self.tips_picked += 1
        self.steps += 1

    def drop_tip(self) -> None:
        if not self.has_tip:
            raise ValueError(f"{self.name} cannot drop_tip() without an attached tip.")
        self.has_tip = False
        self.current_volume = 0.0
        self.steps += 1

    def aspirate(self, volume: float, location: _SimWell) -> None:
        if not self.has_tip:
            raise ValueError(f"{self.name} cannot aspirate() without a tip attached.")
        val = self._require_volume(volume, action="aspirate")
        if self.current_volume + val > self.max_volume + 1e-6:
            raise ValueError(
                f"{self.name} aspirate {val:g} uL exceeds capacity ({self.max_volume:g} uL)."
            )
        self.current_volume += val
        location.current_volume_ul = max(0.0, location.current_volume_ul - val)
        self.total_aspirated_ul += val
        self.steps += 1

    def dispense(self, volume: float, location: _SimWell) -> None:
        if not self.has_tip:
            raise ValueError(f"{self.name} cannot dispense() without a tip attached.")
        val = self._require_volume(volume, action="dispense")
        if val > self.current_volume + 1e-6:
            raise ValueError(
                f"{self.name} dispense {val:g} uL > aspirated ({self.current_volume:g} uL)."
            )
        if location.current_volume_ul + val > location.capacity_ul + 1e-6:
            raise ValueError(
                f"Well {location.label} overflow: {location.current_volume_ul + val:g} uL "
                f"> capacity {location.capacity_ul:g} uL."
            )
        self.current_volume -= val
        location.current_volume_ul += val
        self.total_dispensed_ul += val
        self.steps += 1

    def mix(self, repetitions: int, volume: float, location: _SimWell) -> None:
        if not self.has_tip:
            raise ValueError(f"{self.name} cannot mix() without a tip attached.")
        self._require_volume(volume, action="mix")
        if repetitions < 1 or repetitions > 50:
            raise ValueError(f"mix repetitions must be 1..50, got {repetitions}.")
        self.steps += int(repetitions)

    def blow_out(self, location: _SimWell | None = None) -> None:
        _ = location
        if not self.has_tip:
            raise ValueError(f"{self.name} cannot blow_out() without a tip attached.")
        self.current_volume = 0.0
        self.steps += 1

    def touch_tip(self, location: _SimWell | None = None) -> None:
        _ = location
        if not self.has_tip:
            raise ValueError(f"{self.name} cannot touch_tip() without a tip attached.")
        self.steps += 1


@dataclass
class _SimProtocolContext:
    deck: dict[str, _SimLabware] = field(default_factory=dict)
    instruments: dict[str, _SimInstrumentContext] = field(default_factory=dict)
    comments: list[str] = field(default_factory=list)

    def comment(self, msg: str) -> None:
        self.comments.append(str(msg))

    def load_labware(self, load_name: str, location: str | int) -> _SimLabware:
        slot = str(location)
        if slot not in {str(i) for i in range(1, 12)}:
            raise ValueError(f"Invalid OT-2 deck slot {slot!r}; expected 1..11.")
        if slot in self.deck:
            raise ValueError(f"Deck slot {slot!r} already occupied by {self.deck[slot].load_name}.")
        lw = _SimLabware(load_name, slot)
        self.deck[slot] = lw
        return lw

    def load_instrument(
        self,
        instrument_name: str,
        mount: str,
        tip_racks: list[_SimLabware] | None = None,
    ) -> _SimInstrumentContext:
        if instrument_name not in PIPETTE_SPECS:
            raise ValueError(f"Unsupported pipette model {instrument_name!r}.")
        if mount not in ("left", "right"):
            raise ValueError(f"Invalid pipette mount {mount!r}; expected 'left' or 'right'.")
        if mount in self.instruments:
            raise ValueError(f"Mount {mount!r} already has {self.instruments[mount].name}.")
        spec = PIPETTE_SPECS[instrument_name]
        inst = _SimInstrumentContext(
            name=instrument_name,
            mount=mount,
            min_volume=spec["min_volume_ul"],
            max_volume=spec["max_volume_ul"],
            tip_racks=list(tip_racks or []),
            protocol=self,
        )
        self.instruments[mount] = inst
        return inst


class _OpentronsModuleStub:
    class ProtocolContext:
        pass


def _ast_safety_check(source_code: str, *, require_run_fn: bool = True) -> dict:
    tree = ast.parse(source_code)
    has_run = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in _FORBIDDEN_IMPORTS:
                    raise ValueError(f"Forbidden import in protocol script: {alias.name!r}.")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in _FORBIDDEN_IMPORTS:
                raise ValueError(f"Forbidden import in protocol script: {node.module!r}.")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_CALLS:
                raise ValueError(f"Forbidden call in protocol script: {node.func.id!r}.")
        elif isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
    if require_run_fn and not has_run:
        raise ValueError("Opentrons protocol must define `def run(protocol):`.")
    return {"ast_valid": True, "has_run": has_run}


def verify_opentrons_protocol(source_code: str, *, try_cli: bool = True) -> dict:
    """Validate and dry-run an Opentrons API v2 protocol using CLI (if available) + simulator."""
    errors: list[str] = []
    warnings: list[str] = []
    cli_path = shutil.which("opentrons_simulate") if try_cli else None
    cli_result: dict | None = None

    try:
        _ast_safety_check(source_code, require_run_fn=True)
    except (SyntaxError, ValueError) as exc:
        return {
            "ok": False,
            "verifier": "autolab_opentrons_api_v2_simulator",
            "opentrons_cli_available": bool(cli_path),
            "cli_result": None,
            "api_level": None,
            "steps_executed": 0,
            "tips_used": 0,
            "total_aspirated_ul": 0.0,
            "total_dispensed_ul": 0.0,
            "deck_slots_used": {},
            "pipettes_loaded": {},
            "well_final_volumes_ul": {},
            "run_log": [],
            "errors": [str(exc)],
            "warnings": [],
        }

    if cli_path:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                script_file = Path(tmp) / "protocol.py"
                script_file.write_text(source_code, encoding="utf-8")
                done = subprocess.run(
                    [cli_path, "-o", "nothing", str(script_file)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                cli_result = {
                    "returncode": done.returncode,
                    "stdout": done.stdout[-1500:],
                    "stderr": done.stderr[-1500:],
                }
                if done.returncode != 0:
                    errors.append(
                        f"opentrons_simulate exited with {done.returncode}: {done.stderr.strip()}"
                    )
        except Exception as exc:
            warnings.append(f"opentrons_simulate invocation warning: {exc}")

    namespace: dict = {
        "__builtins__": {
            "range": range,
            "len": len,
            "min": min,
            "max": max,
            "round": round,
            "sum": sum,
            "enumerate": enumerate,
            "list": list,
            "dict": dict,
            "tuple": tuple,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
        },
        "opentrons": type("opentrons", (), {"protocol_api": _OpentronsModuleStub})(),
        "protocol_api": _OpentronsModuleStub,
    }

    # Support `from opentrons import protocol_api` cleanly inside the sandboxed namespace.
    def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ARG001
        if name == "opentrons":
            return namespace["opentrons"]
        if name == "math":
            return math
        raise ValueError(f"Unsupported protocol import: {name!r}.")

    namespace["__builtins__"]["__import__"] = _restricted_import
    ctx = _SimProtocolContext()
    api_level = None
    try:
        exec(compile(source_code, "<opentrons_protocol>", "exec"), namespace)
        reqs = namespace.get("requirements") or namespace.get("metadata") or {}
        if isinstance(reqs, dict):
            api_level = reqs.get("apiLevel")
        if api_level is None:
            warnings.append("Missing requirements['apiLevel'] declaration.")
        namespace["run"](ctx)
        for inst in ctx.instruments.values():
            if inst.has_tip:
                warnings.append(f"Pipette {inst.name} ({inst.mount}) ended holding a tip.")
    except Exception as exc:
        errors.append(str(exc))

    steps_executed = sum(inst.steps for inst in ctx.instruments.values())
    tips_used = sum(inst.tips_picked for inst in ctx.instruments.values())
    total_asp = round(sum(inst.total_aspirated_ul for inst in ctx.instruments.values()), 4)
    total_disp = round(sum(inst.total_dispensed_ul for inst in ctx.instruments.values()), 4)
    dest_lw = ctx.deck.get("1")
    well_vols = (
        {
            w.label: round(w.current_volume_ul, 4)
            for w in dest_lw.wells()
            if w.current_volume_ul > 0
        }
        if dest_lw is not None and not dest_lw.is_tiprack
        else {}
    )
    verifier_name = "opentrons_simulate" if (cli_result and cli_result["returncode"] == 0) else (
        "autolab_opentrons_api_v2_simulator"
    )
    return {
        "ok": len(errors) == 0,
        "verifier": verifier_name,
        "opentrons_cli_available": bool(cli_path),
        "cli_result": cli_result,
        "api_level": api_level,
        "steps_executed": steps_executed,
        "tips_used": tips_used,
        "total_aspirated_ul": total_asp,
        "total_dispensed_ul": total_disp,
        "deck_slots_used": {slot: lw.load_name for slot, lw in sorted(ctx.deck.items())},
        "pipettes_loaded": {mount: inst.name for mount, inst in sorted(ctx.instruments.items())},
        "well_final_volumes_ul": well_vols,
        "run_log": ctx.comments,
        "errors": errors,
        "warnings": warnings,
    }


def verify_jubilee_protocol(source_code: str) -> dict:
    """Syntax-check and execute a generated Science Jubilee script in --simulate mode."""
    errors: list[str] = []
    parsed: dict = {}
    try:
        ast.parse(source_code)
    except SyntaxError as exc:
        return {"ok": False, "verifier": "jubilee_protocol_simulate", "errors": [str(exc)]}

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "jubilee_protocol.py"
        target.write_text(source_code, encoding="utf-8")
        done = subprocess.run(
            [sys.executable, "-I", str(target), "--simulate"],
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if done.returncode != 0:
            errors.append(done.stderr.strip() or f"Exited with code {done.returncode}")
        else:
            try:
                parsed = json.loads(done.stdout)
                if parsed.get("status") != "simulated_ok":
                    errors.append(f"Unexpected simulation status: {parsed.get('status')!r}")
            except json.JSONDecodeError as exc:
                errors.append(f"Invalid simulation JSON output: {exc}")

    return {
        "ok": len(errors) == 0,
        "verifier": "jubilee_protocol_subprocess_simulate",
        "steps_executed": int(parsed.get("steps_executed", 0) or 0),
        "tips_used": int(parsed.get("tips_used", 0) or 0),
        "consumed_ul": parsed.get("consumed_ul", {}),
        "destination_wells": parsed.get("destination_wells", {}),
        "errors": errors,
        "warnings": [],
    }
