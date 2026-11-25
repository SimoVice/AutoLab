"""Compare proposed ways to implement a brief, retaining the evidence and the user's choice."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from app.bench_station import MODULES, SOURCES, StationProposal, compile_station
from app.part_choices import Candidate, Record, SpecValue, _affirmative, _number, _stated_value
from app.workflows import current_kind

Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")]
Note = Annotated[str, Field(min_length=1, max_length=420)]
SourceId = Annotated[str, Field(min_length=1, max_length=360)]
Strategy = Literal["reuse", "buy", "fabricate"]
Adapter = Literal[
    "develop_bench_station",
    "configure_standalone_fixture",
    "develop_custom_equipment",
    "ingest_open_hardware_project",
    "select_platform",
    "choose_requirement_part",
]

# These describe the output of the existing implementation tool. Operation IDs in a
# user's brief are open-ended. A manual step can accompany any of these design routes.
CAPABILITIES = {
    "develop_bench_station": {op: mod["role"] for op, mod in MODULES.items()},
    "configure_standalone_fixture": {
        "locating_base": "Parametric locating pocket and fabrication drawings.",
        "sample_holder": "Printable support fitted to a measured or named object.",
        "imaging_rig": "Printable camera support and object holder. Acquisition is separate.",
    },
    "develop_custom_equipment": {
        "custom_geometry": "Compose and revise original parametric components and connections.",
    },
    "ingest_open_hardware_project": {
        "pinned_cad": "Import a licensed, commit-pinned CAD part for inspection and adaptation.",
    },
    "select_platform": {
        "jubilee": "Original Jubilee foundation with a proposed workstation layout.",
        "opentrons-ot2": "Schematic OT-2 workstation with sourced deck information.",
    },
    "choose_requirement_part": {
        "identify_part": "Bind an exact part or an inventory item to an existing BOM requirement.",
    },
}


class Operation(Record):
    key: Key
    action: Note
    material: Note | None = None
    performance: Note | None = None
    interface: Note | None = None


class Coverage(Record):
    operation: Key
    capability: Key


class Measurement(SpecValue):
    quote: Note


class Option(Record):
    key: Key
    label: Annotated[str, Field(min_length=1, max_length=100)]
    strategy: Strategy
    adapter: Adapter
    coverage: list[Coverage] = Field(min_length=1, max_length=10)
    source_ids: list[SourceId] = Field(default_factory=list, max_length=6)
    cad_file_path: Annotated[str, Field(min_length=1, max_length=400)] | None = None
    requirement_line_id: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    inventory_quote: Note | None = None
    measurements: list[Measurement] = Field(default_factory=list, max_length=10)
    benefit: Note
    tradeoff: Note
    measurement_question: Note | None = None
    measurement_key: Key | None = Field(
        default=None, description="Parameter this question resolves."
    )
    blocker: Note | None = None


class CompareRoutes(Record):
    operations: list[Operation] = Field(min_length=1, max_length=10)
    options: list[Option] = Field(min_length=3, max_length=8)

    @model_validator(mode="after")
    def consistent(self):
        operations = {op.key for op in self.operations}
        if len(operations) != len(self.operations) or len({o.key for o in self.options}) != len(
            self.options
        ):
            raise ValueError("Use unique operation and option keys.")
        if {o.strategy for o in self.options} != {"reuse", "buy", "fabricate"}:
            raise ValueError(
                "Compare reuse, purchase and fabrication. Record blockers for unavailable routes."
            )
        for opt in self.options:
            covered = [c.operation for c in opt.coverage]
            if not set(covered) <= operations or len(set(covered)) != len(covered):
                raise ValueError(
                    "Each coverage entry must identify a different requested operation."
                )
            if any(
                c.capability not in {*CAPABILITIES[opt.adapter], "manual"} for c in opt.coverage
            ):
                raise ValueError(f"Choose a supported {opt.adapter} capability or manual.")
            if bool(opt.measurement_question) != bool(opt.measurement_key):
                raise ValueError("Pair a deciding measurement question with its parameter key.")
            if len({m.key for m in opt.measurements}) != len(opt.measurements):
                raise ValueError("Record one current measurement per parameter.")
            if len(set(opt.source_ids)) != len(opt.source_ids):
                raise ValueError("Use each source only once per option.")
        return self


class Source(Record):
    key: SourceId
    label: Note
    kind: Literal["manufacturer", "catalog", "supplier", "platform", "open_hardware"]
    url: Annotated[str, Field(max_length=1500)] | None = None
    detail: Note
    files: list[Annotated[str, Field(min_length=1, max_length=400)]] = Field(
        default_factory=list, max_length=8
    )
    license_files: list[Annotated[str, Field(min_length=1, max_length=400)]] = Field(
        default_factory=list, max_length=4
    )
    importable: bool = False
    supplier_identity: Candidate | None = None

    @model_validator(mode="after")
    def supplier_binding(self):
        c = self.supplier_identity
        if self.kind == "supplier":
            if (
                not c
                or c.basis != "supplier_search"
                or c.source_url != self.url
                or (self.key != f"supplier:{c.issuer}:{c.part_number}")
            ):
                raise ValueError("The supplier identity must match the observed source.")
        elif c:
            raise ValueError("A supplier observation requires a supplier source.")
        return self

    @field_validator("url")
    @classmethod
    def http_url(cls, value):
        if value is not None:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Expected an HTTP source URL without credentials.")
        return value


class Decision(Record):
    key: Key
    user_quote: Note
    applied_revision: Annotated[int, Field(ge=0)] | None = None


class WorkspaceRequest(Record):
    workspace_width_mm: Annotated[float, Field(gt=0, le=5000)] | None = None
    workspace_depth_mm: Annotated[float, Field(gt=0, le=5000)] | None = None
    workspace_height_mm: Annotated[float, Field(gt=0, le=5000)] | None = None
    brief_objective: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    brief_process: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    brief_materials: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    brief_equipment: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    brief_constraints: Annotated[str, Field(min_length=1, max_length=1000)] | None = None


class DesignPlan(CompareRoutes):
    pending_workspace: WorkspaceRequest | None = None
    brief_signature: Annotated[str, Field(pattern="^[a-f0-9]{64}$")]
    sources: list[Source] = Field(max_length=48)
    blockers: dict[Key, list[Note]]
    decision: Decision | None = None

    @model_validator(mode="after")
    def bound_records(self):
        keys = {o.key for o in self.options}
        refs = {s.key for s in self.sources}
        if set(self.blockers) != keys or len(refs) != len(self.sources):
            raise ValueError("The design comparison contains inconsistent option or source keys.")
        if {s for o in self.options for s in o.source_ids} != refs:
            raise ValueError("Every design source must belong to a compared option.")
        if self.decision and (self.decision.key not in keys or self.blockers[self.decision.key]):
            raise ValueError("A selected route must identify an available option.")
        return self


class ChooseRoute(Record):
    key: Key
    user_quote: Note


def planning_inputs(project):
    pending = (project.get("design_plan") or {}).get("pending_workspace") or {}
    proposed = {k: v for k, v in pending.items() if v is not None}
    brief = dict(project.get("brief") or {})
    for key in tuple(proposed):
        if key.startswith("brief_"):
            brief[key.removeprefix("brief_")] = proposed.pop(key)
    return {**project, "brief": brief, "summary": {**project["summary"], **proposed}}


def brief_signature(project):
    project = planning_inputs(project)
    summary = project.get("summary") or {}
    dims = [
        float(summary[k]) if summary.get(k) is not None else None
        for k in ("workspace_width_mm", "workspace_depth_mm", "workspace_height_mm")
    ]
    brief = [
        project.get("brief") or {},
        current_kind(project),
        dims,
        summary.get("cooling_enabled"),
        float(summary["cooling_target_c"]) if summary.get("cooling_target_c") is not None else None,
    ]
    return hashlib.sha256(
        json.dumps(brief, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def needs_review(project):
    plan = project.get("design_plan")
    return bool(plan and plan["brief_signature"] != brief_signature(project))


def open_source(candidate):
    host, repo, pin = (candidate.get(k) for k in ("source_host", "repo_full_name", "commit_sha"))
    if not all((host, repo, pin)):
        return None
    allowed = candidate.get("license_allowed") is True
    files = [
        p
        for p in (candidate.get("cad_files") or [])[:8]
        if p.lower().endswith((".step", ".stp", ".stl"))
    ]
    return Source(
        key=f"open:{host}:{repo}@{pin}",
        kind="open_hardware",
        label=repo,
        url=(
            candidate["repo_url"].rstrip("/") + ("/tree/" if host == "github" else "/-/tree/") + pin
            if host in ("github", "gitlab") and candidate.get("repo_url")
            else candidate.get("repo_url")
        ),
        files=files,
        license_files=(candidate.get("license_files") or [])[:4],
        importable=bool(
            allowed
            and files
            and host in ("github", "gitlab", "nih3d")
            and (host == "nih3d" or re.fullmatch("[a-fA-F0-9]{40}", pin))
        ),
        detail=f"{candidate.get('spdx_id') or 'Unknown license'} at {pin}. "
        "CAD fit and performance require verification.",
    )


def supplier_source(hit):
    if not hit.get("supplier") or not hit.get("sku"):
        return None
    return Source(
        key=f"supplier:{hit['supplier']}:{hit['sku']}",
        label=f"{hit['supplier']} {hit['sku']}",
        kind="supplier",
        url=hit.get("source_url") or None,
        supplier_identity=Candidate(
            basis="supplier_search",
            issuer=hit["supplier"],
            part_number=hit["sku"],
            mpn=hit.get("mpn"),
            source_url=hit.get("source_url") or None,
            source_observed_at=hit.get("fetched_at"),
            evidence_sha256=hit.get("evidence_sha256"),
            unit="unit" if hit.get("pack_size") == 1 else None,
            evidence=f"{hit['supplier']} supplier search, SKU {hit['sku']}",
        ),
        detail=f"Supplier match for {hit.get('mpn') or hit['sku']}. "
        "Check the ordering variant and current availability.",
    )


def resolve_sources(ids, project, observed):
    from app.bom_engine import load_purchasable_catalog
    from app.platform_profiles import platform_profiles

    known = {s["key"]: s for s in (project.get("design_plan") or {}).get("sources", [])}
    known.update(observed)
    result = {}
    for key in dict.fromkeys(ids):
        prefix, _, name = key.partition(":")
        if prefix == "bench" and name in SOURCES:
            label, url = SOURCES[name]
            source = Source(
                key=key, kind="manufacturer", label=label, url=url, detail=MODULES[name]["guidance"]
            )
        elif prefix == "catalog":
            item = load_purchasable_catalog()["items"].get(name)
            if not item:
                raise ValueError(f"Unknown purchasing catalog ID {name}.")
            source = Source(
                key=key,
                kind="catalog",
                label=item["label"],
                url=item.get("source_url"),
                detail=f"{item['supplier']} ordering reference {item.get('sku') or item['mpn']}.",
            )
        elif prefix == "platform":
            platform = next((p for p in platform_profiles() if p["id"] == name), None)
            if not platform:
                raise ValueError("Unknown platform reference.")
            source = Source(
                key=key,
                kind="platform",
                label=platform["label"],
                url=platform["source_url"],
                detail=platform["limits"],
            )
        elif key in known:
            source = Source.model_validate(known[key])
        else:
            raise ValueError(
                f"Unknown source {key}. Use a source ID returned by discovery or supplier search."
            )
        result[key] = source
    return result


def option_blockers(option, operations, sources, project):
    reasons = [s for s in (option.blocker, option.measurement_question) if s]
    covered = {c.operation for c in option.coverage}
    if missing := [op.action for op in operations if op.key not in covered]:
        reasons.append("Missing operation coverage: " + ", ".join(missing)[:350])
    caps = [c.capability for c in option.coverage if c.capability != "manual"]
    if not caps:
        reasons.append("The proposed adapter has no assigned design operation.")
    refs = [sources[k] for k in option.source_ids]
    if option.strategy == "buy" and not any(
        s.kind in ("catalog", "supplier", "manufacturer", "platform") for s in refs
    ):
        reasons.append("Identify a supplier or manufacturer candidate.")
    if option.strategy == "reuse" and not (
        option.inventory_quote or any(s.kind in ("open_hardware", "platform") for s in refs)
    ):
        reasons.append("Identify available equipment or a reusable source design.")
    if option.adapter == "choose_requirement_part":
        reasons.extend(purchasing_blockers(option, refs, project))
    if option.adapter == "ingest_open_hardware_project":
        available = [(s.key, path) for s in refs if s.importable for path in s.files]
        if not available:
            reasons.append("Locate a pinned STEP or STL with an accepted license.")
        elif sum(option.cad_file_path in (None, path) for _, path in available) != 1:
            reasons.append("Identify the specific CAD file to adopt from the observed candidates.")
    if option.adapter == "select_platform":
        if len(caps) != 1 or ("platform:" + caps[0]) not in option.source_ids:
            reasons.append("Identify one supported workstation and its reviewed source.")
        elif current_kind(project) != "liquid_preparation":
            reasons.append("The workstation layout adapter requires a liquid-preparation brief.")
        else:
            from app.spatial_fit import evaluate_spatial_fit

            fit = evaluate_spatial_fit(project, platform_id=caps[0])
            reasons.extend(v["message"] for v in fit.get("violations", []))
    if option.adapter == "develop_bench_station" and caps:
        try:
            dims = {
                k: project["summary"].get(k)
                for k in ("workspace_width_mm", "workspace_depth_mm", "workspace_height_mm")
            }
            compile_station(StationProposal(operations=list(dict.fromkeys(caps))), **dims)
        except ValueError as exc:
            reasons.append(str(exc)[:420])
    return list(dict.fromkeys(r[:420] for r in reasons))


def purchasing_blockers(option, refs, project):
    from app.bom_engine import build_complete_bom, load_purchasable_catalog
    from app.part_choices import candidate_view, catalog_candidate

    identities = [s for s in refs if s.kind in ("catalog", "supplier")]
    if len(identities) != 1 or not option.requirement_line_id:
        return ["Match one catalog or supplier identity to a project BOM requirement."]
    row = next(
        (
            r
            for r in build_complete_bom(project)["lines"]
            if r["line_id"] == option.requirement_line_id
        ),
        None,
    )
    if not row or not row.get("requirement"):
        return ["Identify a current BOM requirement for this candidate."]
    catalog = load_purchasable_catalog()["items"]
    source = identities[0]
    candidate = (
        source.supplier_identity
        if source.kind == "supplier"
        else catalog_candidate(source.key.removeprefix("catalog:"), catalog)
    )
    checks = candidate_view(candidate, row["requirement"], catalog)["checks"]
    return [
        f"{c['key']} requires {c['comparison']} {c['required']} {c['unit']}, "
        f"but the candidate supplies {c['actual']}."
        for c in checks
        if c["status"] == "mismatch"
    ]


def _inventory_stated(quote, ctx):
    if quote in (ctx.lab_profile or {}).get("owned_equipment", []):
        return True
    declarations = [ctx.user_message, *(h["content"] for h in ctx.history if h["role"] == "user")]
    prior = ctx.project.get("design_plan") or {}
    if any(o.get("inventory_quote") == quote for o in prior.get("options", [])):
        return True
    if re.search(
        r"\b(no|none|need|wish|want|buy|would|manca|vorrei|comprare)\b|"
        r"\bhave to\b|\bhas to\b",
        quote,
        re.I,
    ):
        return False
    return any(quote in text and _affirmative(quote, text) for text in declarations) and bool(
        re.search(
            r"\b(own|owns|have|has|already available|in stock|"
            r"ho|abbiamo|possiedo|disponiamo|già disponibile)\b",
            quote,
            re.I,
        )
    )


def compare_routes(args, ctx):
    if not any((ctx.project.get("brief") or {}).get(k) for k in ("objective", "process")):
        raise ValueError("Record the user's objective or process in the project brief first.")
    sources = resolve_sources(
        (k for o in args.options for k in o.source_ids), ctx.project, ctx.design_sources
    )
    previous = ctx.project.get("design_plan") or {}
    prior = {o["key"]: o for o in previous.get("options", [])}
    statements = [ctx.user_message, *(h["content"] for h in ctx.history if h["role"] == "user")]
    for option in args.options:
        old = prior.get(option.key, {})
        for m in option.measurements:
            if m.model_dump() in old.get("measurements", []):
                continue
            if not any(
                m.quote in s and _affirmative(m.quote, s) for s in statements
            ) or not _stated_value(m, m.quote):
                raise ValueError("Record the measured value and units from the user's statement.")
        if (
            old.get("measurement_question")
            and not option.measurement_question
            and not option.blocker
        ):
            if not any(
                m.key == old["measurement_key"] and m.model_dump() not in old["measurements"]
                for m in option.measurements
            ):
                raise ValueError("Record the measurement that resolved the previous question.")
        if option.inventory_quote and not _inventory_stated(option.inventory_quote, ctx):
            raise ValueError(
                "Use the lab inventory or the user's affirmative ownership statement verbatim."
            )
    effective = planning_inputs(ctx.project)
    plan = DesignPlan(
        **args.model_dump(),
        pending_workspace=previous.get("pending_workspace"),
        brief_signature=brief_signature(ctx.project),
        sources=list(sources.values()),
        blockers={
            o.key: option_blockers(o, args.operations, sources, effective) for o in args.options
        },
    ).model_dump()
    if previous and {**previous, "decision": None} == plan:
        return previous
    return plan


def require_available(project, plan, option):
    reasons = plan["blockers"][option["key"]]
    if not reasons:
        sources = {s["key"]: Source.model_validate(s) for s in plan["sources"]}
        reasons = option_blockers(
            Option.model_validate(option),
            [Operation.model_validate(o) for o in plan["operations"]],
            sources,
            planning_inputs(project),
        )
    if reasons:
        raise ValueError("Resolve this route first. " + " ".join(reasons))


def choose_route(args, ctx):
    plan = ctx.project.get("design_plan")
    if not plan or needs_review(ctx.project):
        raise ValueError("Compare routes against the current brief before choosing one.")
    option = next((o for o in plan["options"] if o["key"] == args.key), None)
    if option is None:
        raise ValueError("Choose one of the compared options.")
    require_available(ctx.project, plan, option)
    quote = args.user_quote
    if quote not in ctx.user_message or not _affirmative(quote, ctx.user_message):
        raise ValueError("Use the user's current affirmative choice verbatim.")
    # Resolve the user's wording to one displayed option. An ordinal refers to display order.
    ordinals = (
        ("first", "prima", "primo"),
        ("second", "seconda", "secondo"),
        ("third", "terza", "terzo"),
        ("fourth", "quarta", "quarto"),
        ("fifth", "quinta", "quinto"),
        ("sixth", "sesta", "sesto"),
        ("seventh", "settima", "settimo"),
        ("eighth", "ottava", "ottavo"),
    )
    terms = {
        "reuse": ("reuse", "riuso", "riusa", "riutilizza"),
        "buy": ("buy", "acquista", "acquisto"),
        "fabricate": ("fabricate", "fabbrica", "fabbricazione"),
    }

    def names_for(index, opt):
        names = [opt["label"], opt["key"].replace("-", " "), opt["key"]]
        if sum(o["strategy"] == opt["strategy"] for o in plan["options"]) == 1:
            names.extend(terms[opt["strategy"]])
        if index < len(ordinals):
            names.extend(ordinals[index])
        return names

    terms_by_option = {o["key"]: names_for(i, o) for i, o in enumerate(plan["options"])}
    matches = [
        key
        for key, words in terms_by_option.items()
        if any(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", quote, re.I) for word in words)
    ]
    direct = re.sub(r"^(?:the|il|la|l'opzione|option)\s+", "", quote.strip(" .!"), flags=re.I)
    direct = re.sub(r"[, ]+(?:please|grazie)$", "", direct, flags=re.I)
    direct_choice = direct.casefold() in {word.casefold() for word in terms_by_option[args.key]}
    instruction = re.search(
        r"\b(choose|use|go with|proceed|select|build|make|fabricate|buy|reuse|pick|keep|"
        r"scelgo|scegli|usa|usiamo|procedi|adotta|costruisci|stampa|riutilizza|acquista|mantieni)\b",
        quote,
        re.I,
    )
    if matches != [args.key] or not (direct_choice or instruction):
        raise ValueError("Ask the user to identify one option to adopt.")
    if plan["decision"] and plan["decision"]["key"] == args.key:
        return plan
    return {**plan, "decision": Decision(key=args.key, user_quote=quote).model_dump()}


def stage_adoption(name, args, project):
    from app.domain import apply_update
    from app.lab_profile import record_dimension

    plan = project["design_plan"]
    pending = plan.get("pending_workspace") or {}
    patch = {k: v for k, v in pending.items() if v is not None}
    staged = deepcopy(project)
    hosts = {
        "configure_standalone_fixture": "standalone_fixture",
        "develop_bench_station": "bench_station",
        "develop_custom_equipment": "concept_design",
        "ingest_open_hardware_project": "upstream_project",
    }
    if name in hosts and not getattr(args, "instance_id", None):
        for old in set(hosts.values()) - {hosts[name]}:
            if staged.get(old):
                if staged.get("cad_instances") or staged.get("build_logs"):
                    raise ValueError(
                        "The current host has attached parts or build records. "
                        "Resolve their placement before replacing the host."
                    )
                del staged[old]
                staged.pop("machine_approval", None)
                staged["status"] = "draft"
    if patch:
        staged = apply_update(staged, patch)
        for k, v in patch.items():
            if k.startswith("workspace_"):
                staged = record_dimension(
                    staged,
                    k.replace("workspace_", "bench_"),
                    v,
                    provenance="user_measured",
                    source="user_chat_specification",
                )
    return staged


def pending_adoption(name, args, project):
    plan = project.get("design_plan")
    if (
        not plan
        or name not in CAPABILITIES
        or (name == "choose_requirement_part" and args.action != "select")
    ):
        return None
    decision = plan["decision"]
    if needs_review(project):
        raise ValueError("The brief changed. Compare the options against its current constraints.")
    if decision and decision["applied_revision"] is not None:
        return None  # Existing designs still accept ordinary follow-up edits and attached parts.
    if not decision:
        raise ValueError("Ask which compared design to adopt before changing the hardware.")
    opt = next(o for o in plan["options"] if o["key"] == decision["key"])
    require_available(project, plan, opt)
    if name != opt["adapter"]:
        raise ValueError("Use the implementation tool for the selected design route.")
    caps = list(
        dict.fromkeys(c["capability"] for c in opt["coverage"] if c["capability"] != "manual")
    )
    if name == "develop_bench_station" and args.operations != caps:
        raise ValueError("The station operations must match the selected route in order.")
    if name == "configure_standalone_fixture" and args.fixture_kind not in caps:
        raise ValueError("Use the fixture kind compared in the selected route.")
    if name == "configure_standalone_fixture":
        for measurement in opt["measurements"]:
            key = measurement["key"]
            if key.endswith("_mm") and key in type(args).model_fields:
                value = getattr(args, key)
                if value is None:
                    value = ((project.get("standalone_fixture") or {}).get("spec") or {}).get(key)
                if measurement["unit"] != "mm" or _number(value) != _number(measurement["value"]):
                    raise ValueError(
                        f"The fixture parameter {key} must match the recorded measurement."
                    )
    if name == "select_platform" and args.platform_id not in caps:
        raise ValueError("Use the selected platform source.")
    if name == "ingest_open_hardware_project":
        key = f"open:{args.source_host}:{args.repo_full_name}@{args.commit_sha}"
        if opt.get("cad_file_path") and args.cad_file_path != opt["cad_file_path"]:
            raise ValueError("Use the CAD file chosen in the design comparison.")
        if not any(
            s["key"] == key
            and key in opt["source_ids"]
            and s["importable"]
            and args.cad_file_path in s["files"]
            for s in plan["sources"]
        ):
            raise ValueError("Use the selected discovery result, commit and inspected file path.")
    if name == "choose_requirement_part":
        if args.line_id != opt["requirement_line_id"]:
            raise ValueError("Apply the selected part to the compared BOM requirement.")
        key = (
            f"catalog:{args.catalog_id}"
            if args.catalog_id
            else f"supplier:{args.issuer}:{args.part_number}"
        )
        if key not in opt["source_ids"]:
            raise ValueError("Use the exact purchasing identity compared in the selected route.")
    return decision
