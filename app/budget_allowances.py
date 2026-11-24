"""Explicit whole-line USD budgets and their binding to the active design."""

import hashlib
import json
import re
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

Money = Annotated[float, Field(strict=True, ge=0, le=10_000_000, allow_inf_nan=False)]


class LineBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    low_usd: Money
    high_usd: Money
    user_quote: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def declared_amounts(self):
        if re.search(r"\b(?:CAD|AUD|NZD|HKD|SGD|EUR|GBP|CHF)\b|[€£]", self.user_quote, re.I):
            raise ValueError("Specify an unambiguous nonnegative USD budget.")
        number = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
        prefix = r"(?:USD\s*\$?|\$)\s*"
        span = rf"(?P<low>{number})(?:\s*(?:[-–—−]|to|and)\s*(?:{prefix})?(?P<high>{number}))?"
        # Accept the conventional US currency prefix or a trailing ISO code.
        for pattern in (rf"{prefix}{span}(?!\d|[.,]\d)", rf"(?<![\d.,]){span}\s*USD\b"):
            for match in re.finditer(pattern, self.user_quote, re.I):
                if self.user_quote[: match.start()].rstrip().endswith(("-", "−")):
                    continue
                low = Decimal(match["low"].replace(",", ""))
                high = Decimal((match["high"] or match["low"]).replace(",", ""))
                if (low, high) == (
                    Decimal(str(self.low_usd)),
                    Decimal(str(self.high_usd)),
                ) and low <= high:
                    return self
        raise ValueError("Quote the declared whole-line USD amount or range exactly.")


class SavedBudget(LineBudget):
    scope_signature: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class SetLineBudgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_id: Annotated[str, Field(min_length=1, max_length=500)]
    budget: LineBudget | None


def budget_signature(project, line):
    from app.procurement import purchase_scope

    identity = (
        "line_id",
        "group_id",
        "catalog_id",
        "component_ids",
        "quantity",
        "unit",
        "purchase_quantity",
        "owned_quantity",
        "offer_id",
        "scope_selection",
    )
    requirement = {k: v for k, v in (line.get("requirement") or {}).items() if k != "notes"}
    scope = purchase_scope(project, line)
    geometry = {k: scope.get(k) for k in ("geometry", "modules", "listed_parts")}
    excluded = (
        sorted(g["id"] for g in (project.get("sourcing") or {}).get("groups", []))
        if line["group_id"] == "upstream-platform"
        else []
    )
    values = [[line.get(key) for key in identity], requirement, geometry, excluded]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def reconcile_budgets(project, *, restoring=False):
    if not project.get("budget_allowances"):
        return
    from app.bom_engine import build_complete_bom

    scopes = {
        line["line_id"]: line["budget_scope_signature"]
        for line in build_complete_bom(project)["lines"]
    }
    records = project["budget_allowances"]
    valid = {
        key: record
        for key, record in records.items()
        if record["scope_signature"] == scopes.get(key)
    }
    if restoring and len(valid) != len(records):
        raise ValueError("A saved budget does not match its purchase scope.")
    project["budget_allowances"] = valid
