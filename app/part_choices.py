"""Project-bound hardware selections and comparisons against explicit requirements."""

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Text = Annotated[str, Field(min_length=1, max_length=160)]
Unit = Annotated[str, Field(max_length=40)]
Evidence = Annotated[str, Field(min_length=1, max_length=1200)]
Amount = (
    Annotated[int, Field(strict=True, ge=0, le=100000)]
    | Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0, le=100000)]
)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SpecValue(Record):
    key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    value: Text
    unit: Unit = ""


class Criterion(SpecValue):
    comparison: Literal["equals", "at_least", "at_most"] = "equals"

    @model_validator(mode="after")
    def comparable(self):
        if self.comparison != "equals" and _number(self.value) is None:
            raise ValueError("A minimum or maximum requires a finite numeric value.")
        return self


class DeclaredCriterion(Criterion):
    evidence: Evidence


class Candidate(Record):
    basis: Literal["catalog", "supplier_search", "user_record"]
    issuer: Text
    part_number: Text
    catalog_id: Text | None = None
    unit: Unit | None = None
    source_url: Annotated[str, Field(max_length=1200)] | None = None
    mpn: Text | None = None
    source_observed_at: Annotated[str, Field(max_length=40)] | None = None
    evidence_sha256: Annotated[str, Field(pattern="^[a-f0-9]{64}$")] | None = None
    facts: list[SpecValue] = Field(default_factory=list, max_length=24)
    evidence: Evidence

    @field_validator("source_url")
    @classmethod
    def public_link(cls, value):
        if value is not None:
            url = urlsplit(value)
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or any(c.isspace() for c in value)
                or url.username
                or url.password
            ):
                raise ValueError("Use an HTTP(S) source URL without credentials.")
        return value

    @model_validator(mode="after")
    def valid_identity(self):
        if bool(self.catalog_id) != (self.basis == "catalog"):
            raise ValueError("A reviewed catalog selection requires its catalog identifier.")
        if len({f.key for f in self.facts}) != len(self.facts):
            raise ValueError("Provide one part value per specification.")
        return self

    @property
    def identity(self):
        return (self.catalog_id, self.issuer.casefold(), self.part_number.casefold())


class PartChoice(Record):
    requirement_signature: Annotated[str, Field(pattern="^[a-f0-9]{64}$")]
    candidates: list[Candidate] = Field(default_factory=list, max_length=12)
    accepted: Candidate | None = None
    owned_quantity: Amount = 0
    inventory_evidence: Evidence | None = None
    decision: Evidence | None = None
    selection_cleared: bool = False
    required_specs: list[DeclaredCriterion] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def consistent(self):
        if len({c.identity for c in self.candidates}) != len(self.candidates):
            raise ValueError("Duplicate part candidates.")
        if self.accepted and not self.decision:
            raise ValueError("A selection requires its decision record.")
        if self.accepted and (self.accepted not in self.candidates or self.selection_cleared):
            raise ValueError("The accepted part must match one of the recorded candidates.")
        if self.owned_quantity and not (self.accepted and self.inventory_evidence):
            raise ValueError("Owned quantities require an identified part and inventory evidence.")
        return self


class ChoosePartArgs(Record):
    line_id: Annotated[str, Field(min_length=1, max_length=500)]
    action: Literal["propose", "select", "clear", "specify"]
    catalog_id: Text | None = None
    issuer: Text | None = None
    part_number: Text | None = None
    source_url: Annotated[str, Field(max_length=1200)] | None = None
    unit: Unit | None = None
    facts: list[SpecValue] = Field(default_factory=list, max_length=24)
    required_specs: list[Criterion] = Field(default_factory=list, max_length=24)
    owned_quantity: Amount | None = None
    inventory_quote: Evidence | None = None
    user_quote: Evidence | None = None


def requirement_signature(line: dict) -> str:
    requirement = dict(line["requirement"])
    requirement.pop("quantity", None)
    payload = [line["line_id"], requirement, line["component_ids"], line.get("design_binding")]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _number(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def required_amount(quantity):
    value = _number(quantity)
    return value if value is not None and 0 < value <= 1_000_000_000 else None


_COUNT_UNITS = {"item", "items", "unit", "units", "each", "ea", "pc", "pcs", "piece", "pieces"}
_CATALOG_UNITS = {"mm": "mm", "nm": "nm", "v": "V", "a": "A", "ma": "mA", "w": "W", "g": "g"}


def unit_matches(expected: str, actual: str) -> bool:
    return expected == actual or {expected.casefold(), actual.casefold()} <= _COUNT_UNITS


def catalog_candidate(catalog_id, catalog):
    entry = catalog.get(catalog_id)
    if not entry:
        raise ValueError("Unknown catalog part. Look up an exact catalog identifier.")
    return Candidate(
        basis="catalog",
        catalog_id=catalog_id,
        issuer=entry["supplier"],
        part_number=entry.get("sku") or entry["mpn"],
        mpn=entry.get("mpn"),
        unit=entry.get("unit") or "unit",
        source_url=entry.get("source_url"),
        evidence="Reviewed AutoLab catalog entry " + catalog_id,
    )


def candidate_view(candidate: Candidate, requirement: dict, catalog: dict) -> dict:
    entry = catalog.get(candidate.catalog_id, {})
    if candidate.catalog_id:
        candidate = catalog_candidate(candidate.catalog_id, catalog)
    checks = []

    def check(key, comparison, required, actual, unit, status):
        checks.append(
            {
                "key": key,
                "comparison": comparison,
                "required": required,
                "actual": actual,
                "unit": unit,
                "status": status,
            }
        )

    unit = requirement["unit"]
    unit_status = (
        "unknown"
        if not candidate.unit
        else "match"
        if unit_matches(unit, candidate.unit)
        else "mismatch"
    )
    check("purchasing_unit", "equals", unit, candidate.unit, "", unit_status)
    facts = {fact.key: fact for fact in candidate.facts}
    for raw in requirement.get("critical_specs", []):
        criterion = Criterion.model_validate(
            {k: raw[k] for k in Criterion.model_fields if k in raw}
        )
        key, value, unit = criterion.key, criterion.value, criterion.unit
        fact = facts.get(key)
        actual = fact.value if fact else (entry.get("specs") or {}).get(key)
        actual_unit = fact.unit if fact else _CATALOG_UNITS.get(key.rsplit("_", 1)[-1], "")
        status = "unknown"
        if actual is not None and actual_unit is not None:
            if not unit_matches(unit, actual_unit):
                # Different physical units require a documented conversion.
                status = "unknown"
            else:
                a, b = _number(actual), _number(value)
                if a is not None and b is not None:
                    matched = {"equals": a == b, "at_least": a >= b, "at_most": a <= b}
                    status = "match" if matched[criterion.comparison] else "mismatch"
                elif criterion.comparison == "equals":
                    status = "match" if str(actual).casefold() == value.casefold() else "mismatch"
        display = None if actual is None else " ".join(filter(None, [str(actual), actual_unit]))
        check(key, criterion.comparison, value, display, unit, status)
    statuses = {c["status"] for c in checks}
    review = (
        "mismatch"
        if "mismatch" in statuses
        else "unverified"
        if "unknown" in statuses or len(checks) == 1
        else "matched"
    )
    return {
        **candidate.model_dump(),
        "label": entry.get("label") or f"{candidate.issuer} {candidate.part_number}",
        "checks": checks,
        "interface_status": review,
    }


def _mentioned(term: str | None, text: str) -> bool:
    return bool(term and re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", text, re.I))


def _stated_value(spec, quote):
    if spec.unit and _number(spec.value) is not None:
        value = re.escape(spec.value).replace(r"\.", "[.,]")
        return bool(
            re.search(r"(?<![\w.-])" + value + r"\s*" + re.escape(spec.unit) + r"(?!\w)", quote)
        )
    return _mentioned(spec.value, quote) and (not spec.unit or _mentioned(spec.unit, quote))


def _affirmative(quote, message):
    # Inspect the whole clause so a clipped quote cannot turn "do not select" into "select".
    start = message.index(quote)
    prefix = re.split(r"[.!?;\n]", message[:start])[-1]
    suffix = message[start + len(quote) :].lstrip()
    clause = (prefix + quote).strip()
    return not (
        clause.endswith("?")
        or suffix.startswith("?")
        or re.search(
            r"\b(?:not|don't|do\s+not|never|non|evita|senza|if|what if|forse|whether)\b",
            clause.replace("’", "'"),
            re.I,
        )
    )


_SPOKEN_COUNTS = [
    ("zero", "none", "nessuno", "nessuna"),
    ("one", "uno", "una", "un"),
    ("two", "due"),
    ("three", "tre"),
    ("four", "quattro"),
    ("five", "cinque"),
    ("six", "sei"),
    ("seven", "sette"),
    ("eight", "otto"),
    ("nine", "nove"),
    ("ten", "dieci"),
]


def _inventory_evidence(args, message, owned, demand):
    quote = args.inventory_quote or args.user_quote or ""
    if not quote or quote not in message:
        raise ValueError("Quote the user's declaration of units on hand.")
    if not args.inventory_quote and not re.search(
        r"\b(own(?:ed)?|have|stock|inventory|already|on hand|in the lab|"
        r"disponibil\w*|magazzino|possiedo|abbiamo|in laboratorio|già)\b",
        quote,
        re.I,
    ):
        raise ValueError("Quote the specific user statement identifying the lab inventory.")
    words = (
        _SPOKEN_COUNTS[int(owned)] if owned == int(owned) and owned < len(_SPOKEN_COUNTS) else ()
    )
    counts = [f"{owned:g}", f"{owned:g}".replace(".", ","), *words]
    if not any(_mentioned(n, quote) for n in counts) and not (demand == owned == 1):
        raise ValueError("Include the declared number of units on hand.")
    return quote


def add_declared_specs(requirement, specs):
    return {
        **requirement,
        "critical_specs": [
            *[s for s in requirement.get("critical_specs", []) if "evidence" not in s],
            *specs,
        ],
    }


def _aligned_candidate(args, record, catalog, supplier_hits, message):
    if args.catalog_id:
        if args.issuer or args.part_number or args.facts or args.source_url:
            raise ValueError(
                "Use the catalog ID alone. Its identity and evidence come from the catalog."
            )
        return catalog_candidate(args.catalog_id, catalog)
    known = [*record.candidates]
    if record.accepted and record.accepted.identity not in [c.identity for c in known]:
        known.append(record.accepted)
    if not args.part_number and not args.issuer:
        if len(known) == 1 and not (args.facts or args.source_url or args.unit):
            return known[0]
        raise ValueError("Specify the exact catalog ID or issuer and part number to disambiguate.")
    if not args.issuer or not args.part_number:
        raise ValueError("Identify the manufacturer or supplier as well as the exact part number.")
    identity = (args.issuer.casefold(), args.part_number.casefold())
    previous = next((c for c in known if c.identity[1:] == identity), None)
    if previous and not (args.facts or args.source_url or args.unit):
        return previous
    hits = [
        h
        for h in supplier_hits
        if (str(h.get("supplier")).casefold(), str(h.get("sku")).casefold()) == identity
    ]
    if len(hits) == 1 and not (args.facts or args.source_url or args.unit):
        hit = hits[0]
        return Candidate(
            basis="supplier_search",
            issuer=hit["supplier"],
            part_number=hit["sku"],
            source_url=hit.get("source_url") or None,
            mpn=hit.get("mpn"),
            source_observed_at=hit.get("fetched_at"),
            evidence_sha256=hit.get("evidence_sha256"),
            unit="unit" if hit.get("pack_size") == 1 else None,
            evidence=f"{hit['supplier']} supplier search, SKU {hit['sku']}",
        )
    quote = args.user_quote or ""
    urls = {url.rstrip(".,;:!?") for url in re.findall(r"https?://[^\s<>\"()]+", quote)}
    if (
        not quote
        or quote not in message
        or not _mentioned(args.issuer, quote)
        or not _mentioned(args.part_number, quote)
        or (args.source_url and args.source_url not in urls)
        or (args.unit and not _mentioned(args.unit, quote))
        or any(not _stated_value(f, quote) for f in args.facts)
    ):
        raise ValueError("Supply the exact identity, part values and source as stated by the user.")
    return Candidate(
        basis="user_record",
        issuer=args.issuer,
        part_number=args.part_number,
        source_url=args.source_url,
        unit=args.unit,
        facts=args.facts,
        evidence=quote,
    )


def update_choice(args: ChoosePartArgs, line, previous, catalog, supplier_hits, message):
    """Stage a single requirement edit. A rejected candidate cannot overwrite a selection."""
    signature = line["requirement_signature"]
    record = (
        PartChoice.model_validate(previous)
        if previous and previous.get("requirement_signature") == signature
        else PartChoice(requirement_signature=signature)
    )
    if args.action != "select" and (args.owned_quantity is not None or args.inventory_quote):
        raise ValueError("Inventory belongs to an accepted component.")
    if args.action != "specify" and args.required_specs:
        raise ValueError("Record the required interface first with the specify action.")
    if args.action in {"select", "clear", "specify"}:
        quote = args.user_quote or ""
        if not quote or quote not in message:
            raise ValueError(
                "Include the user's current selection or inventory instruction verbatim."
            )
        if not _affirmative(quote, message):
            raise ValueError("A question or conditional statement does not change a requirement.")
    if args.action == "specify":
        if not args.required_specs:
            raise ValueError("Specify the critical values and units from the user's instruction.")
        specs = {(s.key, s.comparison): s for s in record.required_specs}
        for spec in args.required_specs:
            if not _stated_value(spec, quote):
                raise ValueError("Each required value and unit must appear in the user's words.")
            prior = specs.get((spec.key, spec.comparison))
            if prior and (prior.value, prior.unit) == (spec.value, spec.unit):
                continue
            specs[(spec.key, spec.comparison)] = DeclaredCriterion(
                **spec.model_dump(), evidence=quote
            )
        record.required_specs = list(specs.values())
        requirement = add_declared_specs(
            line["requirement"], [s.model_dump() for s in specs.values()]
        )
        if (
            record.accepted
            and candidate_view(record.accepted, requirement, catalog)["interface_status"]
            == "mismatch"
        ):
            record.accepted, record.decision, record.owned_quantity = None, None, 0
            record.inventory_evidence = None
        return PartChoice.model_validate(record.model_dump())
    if args.action == "clear":
        record.selection_cleared = True
        record.accepted, record.decision, record.owned_quantity = None, quote, 0
        record.inventory_evidence = None
        return record
    candidate = _aligned_candidate(args, record, catalog, supplier_hits, message)
    view = candidate_view(candidate, line["requirement"], catalog)
    if (
        args.action == "propose"
        and record.accepted
        and record.accepted.identity == candidate.identity
        and record.accepted != candidate
    ):
        raise ValueError("Use select to revise the evidence for an accepted part.")
    if args.action == "select":
        if view["interface_status"] == "mismatch":
            conflicts = ", ".join(c["key"] for c in view["checks"] if c["status"] == "mismatch")
            raise ValueError(
                f"Part conflicts with the requirement ({conflicts}). Compare another candidate."
            )
        same = record.accepted is not None and record.accepted.identity == candidate.identity
        alternatives = {c.identity for c in record.candidates}
        if record.accepted:
            alternatives.add(record.accepted.identity)
        exact = any(
            _mentioned(t, quote)
            for t in (candidate.catalog_id, candidate.part_number, candidate.mpn, view["label"])
        )
        if not exact and alternatives != {candidate.identity}:
            raise ValueError(
                "Choose an exact part or confirm a single previously proposed candidate."
            )
        for other in record.candidates:
            if other.identity == candidate.identity:
                continue
            shared = {s.casefold() for s in (other.part_number, other.mpn) if s}
            identifiers = (candidate.part_number, candidate.mpn, candidate.catalog_id)
            if exact and all(
                not _mentioned(s, quote) or s.casefold() in shared for s in identifiers if s
            ):
                if not (
                    _mentioned(candidate.issuer, quote)
                    and other.issuer.casefold() != candidate.issuer.casefold()
                ):
                    raise ValueError(
                        "Multiple candidates share this code. Choose a unique issuer and SKU."
                    )
        quantity = required_amount(line["quantity"])
        owned = args.owned_quantity
        if not same:
            record.inventory_evidence = None
        if owned is not None:
            if owned and (quantity is None or Decimal(str(owned)) > quantity):
                raise ValueError("Allocate at most the quantity this requirement needs.")
            if owned != int(owned) and line["unit"].casefold() in _COUNT_UNITS:
                raise ValueError("Allocate whole pieces for a requirement counted in items.")
            if owned or (same and record.owned_quantity):
                record.inventory_evidence = _inventory_evidence(args, message, owned, quantity)
        record.owned_quantity = (
            owned if owned is not None else (record.owned_quantity if same else 0)
        )
        if not same or record.accepted != candidate or owned is not None:
            record.decision = quote
        record.accepted = candidate
        record.selection_cleared = False
    options = {c.identity: c for c in record.candidates}
    options[candidate.identity] = candidate
    record.candidates = list(options.values())
    return PartChoice.model_validate(record.model_dump())
