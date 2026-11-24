"""Official supplier catalog API integrations (DigiKey v4, Mouser v2, Shopify JSON) and ECB FX."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
FETCH_TIMEOUT_SECONDS = 12.0
MAX_API_BYTES = 2 * 1024 * 1024
QUOTE_CACHE_TTL_SECONDS = 900
UNAVAILABLE_STATES = frozenset({"OutOfStock", "SoldOut", "Discontinued"})
FX_CACHE_TTL_SECONDS = 6 * 3600

SUPPORTED_CURRENCIES = ("USD", "CHF", "EUR")
DIGIKEY_LOCALES = {
    "USD": ("US", "USD", "en"),
    "CHF": ("CH", "CHF", "en"),
    "EUR": ("DE", "EUR", "en"),
}
ALLOWED_SUPPLIER_API_HOSTS = frozenset(
    {
        "api.digikey.com",
        "sandbox-api.digikey.com",
        "api.mouser.com",
        "api.frankfurter.dev",
        "www.filastruder.com",
        "mandalaroseworks.com",
        "www.pololu.com",
        "www.adafruit.com",
    }
)

_token_cache: dict[tuple[str, str], tuple[str, datetime]] = {}
_fx_cache: dict[str, tuple[dict[str, Any], datetime]] = {}
_quote_cache: dict[tuple[str, str, str], tuple[dict[str, Any], datetime]] = {}


class SupplierApiError(Exception):
    """Safe, user-readable supplier API or currency retrieval failure."""


def clear_supplier_api_caches() -> None:
    """Clear in-memory OAuth token, FX, and supplier quote caches."""
    _token_cache.clear()
    _fx_cache.clear()
    _quote_cache.clear()


def cached_official_quotes(currency: str, fx_rates: dict | None = None) -> dict[str, dict]:
    """Read the latest observation per catalog item without renewing it or contacting suppliers."""
    target = _validate_currency(currency)
    latest = {}
    for (item_id, observed_currency, _), (observation, expires) in list(_quote_cache.items()):
        # Prefer the latest check, then the requested currency for simultaneous observations.
        rank = (expires, observed_currency == target)
        if item_id not in latest or rank > latest[item_id][0]:
            latest[item_id] = (rank, observation)
    results = {}
    now = _now()
    for item_id, ((expires, _), observation) in latest.items():
        fresh = now < expires
        if fresh and observation.get("availability") not in UNAVAILABLE_STATES:
            try:
                observation = _convert_quote_currency(observation, target, fx_rates)
            except SupplierApiError:
                continue
        results[item_id] = {**deepcopy(observation), "freshness": "fresh" if fresh else "stale"}
    return results


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _secret(name: str) -> str | None:
    env_val = os.environ.get(name, "").strip()
    if env_val:
        return env_val
    if os.environ.get("AUTOLAB_DISABLE_DOTENV_SECRETS") == "1":
        return None
    env_path = ROOT / ".env"
    if not env_path.exists():
        return None
    val = str(dotenv_values(env_path).get(name) or "").strip()
    return val or None


def _validate_currency(currency: str) -> str:
    code = str(currency or "").strip().upper()
    if code not in SUPPORTED_CURRENCIES:
        raise SupplierApiError(
            f"Unsupported currency {currency!r}. Choose one of: {', '.join(SUPPORTED_CURRENCIES)}."
        )
    return code


def _validate_api_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise SupplierApiError("Supplier API URL must be non-empty.")
    try:
        parsed = urlsplit(url.strip())
    except ValueError as exc:
        raise SupplierApiError("Invalid supplier URL.") from exc
    if parsed.scheme != "https":
        raise SupplierApiError("Only HTTPS supplier endpoints are permitted.")
    if parsed.username is not None or parsed.password is not None:
        raise SupplierApiError("Credentials in supplier URLs are forbidden.")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_SUPPLIER_API_HOSTS:
        raise SupplierApiError(f"Supplier host {host!r} is not on the reviewed API allowlist.")
    if parsed.port not in (None, 443):
        raise SupplierApiError("Only HTTPS port 443 is permitted.")
    return url.strip()


def _money_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise SupplierApiError("Missing or invalid price value.")
    if isinstance(value, (int, float)):
        raw = str(value)
    elif isinstance(value, Decimal):
        raw = str(value)
    else:
        raw = str(value).strip()
        raw = re.sub(r"^(?:US\$|USD|CHF|EUR|\$|€)\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*(?:USD|CHF|EUR|\$|€)$", "", raw, flags=re.IGNORECASE).strip()
        # Handle European comma-decimal formats (e.g. "12,34" vs "1,234.56").
        if re.fullmatch(r"\d+(?:\.\d{3})*,\d{1,4}", raw):
            raw = raw.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d+,\d{1,4}", raw):
            raw = raw.replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
            raw = raw.replace(",", "")
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise SupplierApiError(f"Unparseable price value: {value!r}") from exc
    if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000"):
        raise SupplierApiError("Price must be positive, finite, and within catalog bounds.")
    return amount


def _money_str(amount: Decimal) -> str:
    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _money_float(amount: Decimal) -> float:
    return float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _parse_lead_time_days(text: object) -> int | None:
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)) and text > 0:
        return int(text)
    raw = str(text).strip()
    if not raw:
        return None
    week_match = re.findall(r"(\d+)\s*(?:weeks?|wks?|w)\b", raw, flags=re.IGNORECASE)
    if week_match:
        return max(int(w) for w in week_match) * 7
    day_match = re.findall(r"(\d+)\s*(?:days?|d)\b", raw, flags=re.IGNORECASE)
    if day_match:
        days = max(int(d) for d in day_match)
        if "business" in raw.lower():
            return max(1, (days * 7 + 4) // 5)
        return days
    return None


def _parse_stock_qty(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    match = re.search(r"(\d[\d,]*)", str(value))
    if not match:
        return None
    return max(0, int(match.group(1).replace(",", "")))


def _normalize_availability(stock_qty: int | None, raw_status: object = None) -> str:
    text = str(raw_status or "").lower()
    if any(k in text for k in ("obsolete", "discontinued", "eol")):
        return "Discontinued"
    if any(k in text for k in ("backorder", "factory", "special order")):
        return "Backorder"
    if stock_qty is not None:
        if stock_qty >= 10:
            return "InStock"
        if stock_qty > 0:
            return "LowStock"
        return "OutOfStock"
    if "in stock" in text or "instock" in text or text == "true":
        return "InStock"
    if "out of stock" in text or "outofstock" in text or "none" in text or text == "false":
        return "OutOfStock"
    return "unknown"


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    form_data: dict[str, str] | None = None,
) -> tuple[int, Any, str]:
    safe_url = _validate_api_url(url)
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            response = await client.request(
                method,
                safe_url,
                headers=headers,
                params=params,
                json=json_body,
                data=form_data,
                follow_redirects=False,
            )
    except (httpx.HTTPError, TimeoutError) as exc:
        raise SupplierApiError(f"Supplier API request failed: {exc}") from None

    if 300 <= response.status_code < 400:
        raise SupplierApiError("Supplier API endpoint attempted a blocked HTTP redirect.")
    raw = response.content or b""
    if len(raw) > MAX_API_BYTES:
        raise SupplierApiError("Supplier API response exceeded the 2 MB size ceiling.")
    sha = hashlib.sha256(raw).hexdigest()
    try:
        data = response.json() if raw else None
    except ValueError:
        data = None
    return response.status_code, data, sha


def supplier_api_capabilities() -> dict[str, Any]:
    """Report which official supplier APIs are configured without exposing credentials."""
    dk_ready = bool(_secret("DIGIKEY_CLIENT_ID") and _secret("DIGIKEY_CLIENT_SECRET"))
    mouser_ready = bool(_secret("MOUSER_API_KEY"))
    return {
        "digikey_v4": {
            "configured": dk_ready,
            "requires_api_key": True,
            "missing_env": [
                k for k in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET") if not _secret(k)
            ],
            "sandbox": _secret("DIGIKEY_SANDBOX") == "1",
        },
        "mouser_v2": {
            "configured": mouser_ready,
            "requires_api_key": True,
            "missing_env": [] if mouser_ready else ["MOUSER_API_KEY"],
        },
        "shopify_json": {"configured": True, "requires_api_key": False, "missing_env": []},
        "frankfurter_fx": {
            "configured": True,
            "requires_api_key": False,
            "currencies": list(SUPPORTED_CURRENCIES),
        },
        "reviewed_html": {"configured": True, "requires_api_key": False, "missing_env": []},
    }


async def fetch_fx_rates(
    base: str = "USD",
    *,
    client: httpx.AsyncClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Fetch official ECB reference FX rates for USD, CHF, and EUR via Frankfurter."""
    base_code = _validate_currency(base)
    now = _now()
    cached = _fx_cache.get(base_code)
    if cached and not force and now < cached[1]:
        return deepcopy(cached[0])

    symbols = ",".join(c for c in SUPPORTED_CURRENCIES if c != base_code)
    url = "https://api.frankfurter.dev/v1/latest"
    params = {"base": base_code, "symbols": symbols}

    async def _run(active_client: httpx.AsyncClient) -> dict[str, Any]:
        status, data, sha = await _request_json(
            active_client,
            "GET",
            url,
            headers={"Accept": "application/json", "User-Agent": "AutoLab-Sourcing/0.1"},
            params=params,
        )
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("rates"), dict):
            raise SupplierApiError(f"Official FX API unavailable (HTTP {status}).")
        rates: dict[str, float] = {base_code: 1.0}
        for target in SUPPORTED_CURRENCIES:
            if target == base_code:
                continue
            raw_rate = data["rates"].get(target)
            dec = _money_decimal(raw_rate)
            rates[target] = float(dec)
        payload = {
            "base": base_code,
            "rates": rates,
            "rate_date": str(data.get("date") or now.date().isoformat()),
            "fetched_at": _iso(now),
            "source": "ECB via api.frankfurter.dev",
            "evidence_sha256": sha,
        }
        _fx_cache[base_code] = (deepcopy(payload), now + timedelta(seconds=FX_CACHE_TTL_SECONDS))
        return deepcopy(payload)

    if client is not None:
        return await _run(client)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10, connect=5), follow_redirects=False
    ) as active:
        return await _run(active)


def cached_fx_rates(base: str = "USD") -> dict[str, Any] | None:
    """Return unexpired cached FX rates without making a network call."""
    cached = _fx_cache.get(_validate_currency(base))
    if not cached or _now() >= cached[1]:
        return None
    return deepcopy(cached[0])


def convert_currency(
    amount: float | Decimal | int,
    from_currency: str,
    to_currency: str,
    fx_rates: dict[str, Any] | None = None,
) -> float:
    """Convert monetary values deterministically using verified FX rates."""
    src = _validate_currency(from_currency)
    dst = _validate_currency(to_currency)
    dec = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if src == dst:
        return _money_float(dec)
    if not isinstance(fx_rates, dict) or not isinstance(fx_rates.get("rates"), dict):
        raise SupplierApiError(f"FX rates are required to convert {src} to {dst}.")
    rates = fx_rates["rates"]
    base = _validate_currency(fx_rates.get("base", "USD"))
    if src not in rates or dst not in rates:
        raise SupplierApiError(f"FX rate table missing {src}->{dst} (base {base}).")
    usd_equiv = dec / Decimal(str(rates[src]))
    converted = usd_equiv * Decimal(str(rates[dst]))
    return _money_float(converted)


def _convert_quote_currency(
    quote_obj: dict[str, Any], target_currency: str, fx_rates: dict[str, Any] | None
) -> dict[str, Any]:
    target = _validate_currency(target_currency)
    src = _validate_currency(quote_obj.get("currency", "USD"))
    if src == target:
        return quote_obj
    out = deepcopy(quote_obj)
    rate = (
        Decimal(str(fx_rates["rates"][target])) / Decimal(str(fx_rates["rates"][src]))
        if isinstance(fx_rates, dict)
        and isinstance(fx_rates.get("rates"), dict)
        and src in fx_rates["rates"]
        and target in fx_rates["rates"]
        else None
    )
    if rate is None:
        raise SupplierApiError(f"FX rates are required to convert {src} quote into {target}.")
    for tier in out.get("price_breaks") or []:
        dec = _money_decimal(tier["pack_price"])
        tier["pack_price"] = _money_str(dec * rate)
    out["currency"] = target
    out["original_currency"] = src
    out["fx_rate_applied"] = round(float(rate), 6)
    out["fx_source"] = fx_rates.get("source")
    out["fx_rate_date"] = fx_rates.get("rate_date")
    return out


class DigiKeyClient:
    """Official DigiKey Product Information API v4 client (OAuth2 client_credentials)."""

    def __init__(self) -> None:
        self.client_id = _secret("DIGIKEY_CLIENT_ID")
        self.client_secret = _secret("DIGIKEY_CLIENT_SECRET")
        self.base_url = (
            "https://sandbox-api.digikey.com"
            if _secret("DIGIKEY_SANDBOX") == "1"
            else "https://api.digikey.com"
        )

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if not self.is_configured():
            raise SupplierApiError(
                "DigiKey API credentials (DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET) are not set."
            )
        key = (self.base_url, self.client_id or "")
        cached = _token_cache.get(key)
        now = _now()
        if cached and now < cached[1]:
            return cached[0]

        status, data, _ = await _request_json(
            client,
            "POST",
            f"{self.base_url}/v1/oauth2/token",
            headers={"Accept": "application/json"},
            form_data={
                "client_id": self.client_id or "",
                "client_secret": self.client_secret or "",
                "grant_type": "client_credentials",
            },
        )
        if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
            raise SupplierApiError(f"DigiKey OAuth2 authentication failed (HTTP {status}).")
        expires_in = max(120, int(data.get("expires_in") or 600))
        token = str(data["access_token"])
        _token_cache[key] = (token, now + timedelta(seconds=expires_in - 60))
        return token

    def _headers(self, token: str, currency: str) -> dict[str, str]:
        site, curr, lang = DIGIKEY_LOCALES[_validate_currency(currency)]
        return {
            "Authorization": f"Bearer {token}",
            "X-DIGIKEY-Client-Id": self.client_id or "",
            "X-DIGIKEY-Locale-Site": site,
            "X-DIGIKEY-Locale-Currency": curr,
            "X-DIGIKEY-Locale-Language": lang,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _parse_product(
        self, product: dict[str, Any], *, currency: str, sha: str, part_number: str | None = None
    ) -> dict[str, Any] | None:
        if not isinstance(product, dict):
            return None
        variations = product.get("ProductVariations") or []
        if part_number:
            number = part_number.strip().casefold()
            matching = [v for v in variations
                        if str(v.get("DigiKeyProductNumber") or "").strip().casefold() == number]
            if len(matching) == 1:
                variations = matching
            elif (len(variations) > 1
                  or str(product.get("ManufacturerProductNumber") or "").casefold() != number):
                return None
        chosen_var = variations[0] if isinstance(variations, list) and variations else {}
        pricing_list = chosen_var.get("StandardPricing") or product.get("StandardPricing") or []
        breaks: list[dict[str, Any]] = []
        for tier in pricing_list:
            if not isinstance(tier, dict):
                continue
            qty = tier.get("BreakQuantity")
            price = tier.get("UnitPrice")
            if isinstance(qty, int) and qty >= 1 and price is not None:
                try:
                    breaks.append(
                        {"minimum_packs": qty, "pack_price": _money_str(_money_decimal(price))}
                    )
                except SupplierApiError:
                    continue
        if not breaks and product.get("UnitPrice") is not None:
            breaks.append(
                {
                    "minimum_packs": 1,
                    "pack_price": _money_str(_money_decimal(product["UnitPrice"])),
                }
            )
        breaks.sort(key=lambda item: item["minimum_packs"])
        stock_qty = _parse_stock_qty(product.get("QuantityAvailable"))
        lead_weeks = product.get("ManufacturerLeadWeeks")
        lead_days = _parse_lead_time_days(f"{lead_weeks} Weeks") if lead_weeks else None
        status_obj = product.get("ProductStatus")
        raw_status = status_obj.get("Status") if isinstance(status_obj, dict) else status_obj
        availability = _normalize_availability(stock_qty, raw_status)
        if not breaks and availability not in UNAVAILABLE_STATES:
            return None
        mpn = str(product.get("ManufacturerProductNumber") or "").strip()
        dk_sku = str(chosen_var.get("DigiKeyProductNumber") or "").strip()
        desc = product.get("Description")
        title = (
            desc.get("ProductDescription")
            if isinstance(desc, dict)
            else str(desc or mpn or dk_sku)
        )
        return {
            "supplier": "DigiKey",
            "adapter": "digikey_v4",
            "mpn": mpn or dk_sku,
            "sku": dk_sku or mpn,
            "product_name": str(title)[:300],
            "currency": currency,
            "pack_size": 1,
            "min_order_qty": int(chosen_var.get("MinimumOrderQuantity") or 1),
            "price_breaks": breaks,
            "availability": availability,
            "stock_quantity": stock_qty,
            "lead_time_days": lead_days,
            "lead_time_text": f"{lead_weeks} weeks" if lead_weeks else None,
            "source_url": str(product.get("ProductUrl") or ""),
            "datasheet_url": str(product.get("DatasheetUrl") or "") or None,
            "rohs_status": str(product.get("Classifications", {}).get("RohsStatus") or "") or None,
            "lifecycle_status": str(raw_status or "") or None,
            "evidence_sha256": sha,
            "parser": "digikey-productdetails-v4",
            "fetched_at": _iso(_now()),
        }

    async def fetch_product_details(
        self,
        part_number: str,
        *,
        currency: str = "USD",
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        curr = _validate_currency(currency)
        encoded = quote(part_number.strip(), safe="")
        url = f"{self.base_url}/products/v4/search/{encoded}/productdetails"

        async def _run(active_client: httpx.AsyncClient) -> dict[str, Any]:
            token = await self._get_token(active_client)
            status, data, sha = await _request_json(
                active_client, "GET", url, headers=self._headers(token, curr)
            )
            if status != 200 or not isinstance(data, dict) or not data.get("Product"):
                raise SupplierApiError(
                    f"DigiKey part {part_number!r} lookup failed (HTTP {status})."
                )
            parsed = self._parse_product(data["Product"], currency=curr, sha=sha,
                                         part_number=part_number)
            if parsed is None:
                raise SupplierApiError(f"DigiKey returned no priced offer for {part_number!r}.")
            return parsed

        if client is not None:
            return await _run(client)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12, connect=5), follow_redirects=False
        ) as active:
            return await _run(active)

    async def search_keyword(
        self,
        query: str,
        *,
        currency: str = "USD",
        limit: int = 6,
        client: httpx.AsyncClient | None = None,
    ) -> list[dict[str, Any]]:
        curr = _validate_currency(currency)
        url = f"{self.base_url}/products/v4/search/keyword"
        body = {"Keywords": query.strip()[:160], "RecordCount": max(1, min(limit, 12))}

        async def _run(active_client: httpx.AsyncClient) -> list[dict[str, Any]]:
            token = await self._get_token(active_client)
            status, data, sha = await _request_json(
                active_client, "POST", url, headers=self._headers(token, curr), json_body=body
            )
            if status != 200 or not isinstance(data, dict):
                raise SupplierApiError(f"DigiKey keyword search failed (HTTP {status}).")
            results = []
            for item in (data.get("Products") or [])[:limit]:
                parsed = self._parse_product(item, currency=curr, sha=sha)
                if parsed is not None:
                    results.append(parsed)
            return results

        if client is not None:
            return await _run(client)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12, connect=5), follow_redirects=False
        ) as active:
            return await _run(active)


class MouserClient:
    """Official Mouser Electronics Search API v2 client."""

    def __init__(self) -> None:
        self.api_key = _secret("MOUSER_API_KEY")
        self.base_url = "https://api.mouser.com/api/v2"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _parse_part(
        self, part: dict[str, Any], *, sha: str
    ) -> dict[str, Any] | None:
        if not isinstance(part, dict):
            return None
        breaks: list[dict[str, Any]] = []
        raw_currency = "USD"
        for pb in part.get("PriceBreaks") or []:
            if not isinstance(pb, dict):
                continue
            qty = pb.get("Quantity")
            price_val = pb.get("Price")
            if isinstance(pb.get("Currency"), str) and pb["Currency"].strip().upper() in (
                SUPPORTED_CURRENCIES
            ):
                raw_currency = pb["Currency"].strip().upper()
            elif isinstance(price_val, str):
                if "€" in price_val:
                    raw_currency = "EUR"
                elif "CHF" in price_val.upper():
                    raw_currency = "CHF"
            if isinstance(qty, int) and qty >= 1 and price_val is not None:
                try:
                    breaks.append(
                        {"minimum_packs": qty, "pack_price": _money_str(_money_decimal(price_val))}
                    )
                except SupplierApiError:
                    continue
        breaks.sort(key=lambda item: item["minimum_packs"])
        stock_value = part.get("AvailabilityInStock")
        stock_qty = _parse_stock_qty(
            stock_value if stock_value is not None else part.get("Availability")
        )
        availability = _normalize_availability(
            stock_qty, part.get("LifecycleStatus") or part.get("Availability")
        )
        if not breaks and availability not in UNAVAILABLE_STATES:
            return None
        lead_text = str(part.get("LeadTime") or "").strip() or None
        lead_days = _parse_lead_time_days(lead_text)
        mpn = str(part.get("ManufacturerPartNumber") or "").strip()
        mouser_pn = str(part.get("MouserPartNumber") or "").strip()
        return {
            "supplier": "Mouser",
            "adapter": "mouser_v2",
            "mpn": mpn or mouser_pn,
            "sku": mouser_pn or mpn,
            "product_name": str(part.get("Description") or mpn or mouser_pn)[:300],
            "currency": raw_currency,
            "pack_size": 1,
            "min_order_qty": _parse_stock_qty(part.get("Min")) or 1,
            "price_breaks": breaks,
            "availability": availability,
            "stock_quantity": stock_qty,
            "lead_time_days": lead_days,
            "lead_time_text": lead_text,
            "source_url": str(part.get("ProductDetailUrl") or ""),
            "datasheet_url": str(part.get("DataSheetUrl") or "") or None,
            "rohs_status": str(part.get("ROHSStatus") or "") or None,
            "lifecycle_status": str(part.get("LifecycleStatus") or "") or None,
            "evidence_sha256": sha,
            "parser": "mouser-search-v2",
            "fetched_at": _iso(_now()),
        }

    async def fetch_part_number(
        self,
        part_number: str,
        *,
        currency: str = "USD",
        fx_rates: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        if not self.is_configured():
            raise SupplierApiError("Mouser API key (MOUSER_API_KEY) is not configured.")
        curr = _validate_currency(currency)
        url = f"{self.base_url}/search/partnumber"
        body = {"SearchByPartRequest": {"mouserPartNumber": part_number.strip()[:80]}}

        async def _run(active_client: httpx.AsyncClient) -> dict[str, Any]:
            status, data, sha = await _request_json(
                active_client,
                "POST",
                url,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                params={"apiKey": self.api_key or ""},
                json_body=body,
            )
            if status != 200 or not isinstance(data, dict):
                raise SupplierApiError(f"Mouser part search failed (HTTP {status}).")
            parts = ((data.get("SearchResults") or {}).get("Parts")) or []
            number = part_number.strip().casefold()
            matches = [part for part in parts if isinstance(part, dict)
                       and number in {str(part.get(k) or "").strip().casefold()
                                      for k in ("ManufacturerPartNumber", "MouserPartNumber")}]
            if len(matches) == 1:
                parsed = self._parse_part(matches[0], sha=sha)
                if parsed is not None:
                    return _convert_quote_currency(parsed, curr, fx_rates)
            raise SupplierApiError(f"Mouser returned no unique exact offer for {part_number!r}.")

        if client is not None:
            return await _run(client)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12, connect=5), follow_redirects=False
        ) as active:
            return await _run(active)

    async def search_keyword(
        self,
        query: str,
        *,
        currency: str = "USD",
        fx_rates: dict[str, Any] | None = None,
        limit: int = 6,
        client: httpx.AsyncClient | None = None,
    ) -> list[dict[str, Any]]:
        if not self.is_configured():
            raise SupplierApiError("Mouser API key (MOUSER_API_KEY) is not configured.")
        curr = _validate_currency(currency)
        url = f"{self.base_url}/search/keyword"
        body = {
            "SearchByKeywordRequest": {
                "keyword": query.strip()[:160],
                "records": max(1, min(limit, 12)),
                "startingRecord": 0,
            }
        }

        async def _run(active_client: httpx.AsyncClient) -> list[dict[str, Any]]:
            status, data, sha = await _request_json(
                active_client,
                "POST",
                url,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                params={"apiKey": self.api_key or ""},
                json_body=body,
            )
            if status != 200 or not isinstance(data, dict):
                raise SupplierApiError(f"Mouser keyword search failed (HTTP {status}).")
            parts = ((data.get("SearchResults") or {}).get("Parts")) or []
            out = []
            for part in parts[:limit]:
                parsed = self._parse_part(part, sha=sha)
                if parsed is not None:
                    out.append(_convert_quote_currency(parsed, curr, fx_rates))
            return out

        if client is not None:
            return await _run(client)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12, connect=5), follow_redirects=False
        ) as active:
            return await _run(active)


async def fetch_shopify_product_json(
    product_url: str,
    *,
    supplier: str = "Shopify",
    product_id: str | None = None,
    variant_id: str | None = None,
    sku: str | None = None,
    variant_title: str | None = None,
    pack_size: int = 1,
    currency: str = "USD",
    fx_rates: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch public Shopify `/products/<handle>.json` metadata for reviewed kit storefronts."""
    safe = _validate_api_url(product_url)
    parsed_url = urlsplit(safe)
    handle = parsed_url.path.rstrip("/").rsplit("/", 1)[-1]
    if "/products/" not in parsed_url.path or not handle:
        raise SupplierApiError("URL does not reference a Shopify /products/<handle> page.")
    json_url = f"https://{parsed_url.hostname}/products/{handle}.json"
    curr = _validate_currency(currency)

    async def _run(active_client: httpx.AsyncClient) -> dict[str, Any]:
        status, data, sha = await _request_json(
            active_client,
            "GET",
            json_url,
            headers={"Accept": "application/json", "User-Agent": "AutoLab-Sourcing/0.1"},
        )
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("product"), dict):
            raise SupplierApiError(f"Shopify product JSON unavailable (HTTP {status}).")
        prod = data["product"]
        if prod.get("handle") != handle:
            raise SupplierApiError("Shopify JSON handle does not match registered product URL.")
        if product_id is not None and str(prod.get("id")) != str(product_id):
            raise SupplierApiError("Shopify JSON product ID differs from registered item.")
        variants = prod.get("variants") or []
        matches = [
            v
            for v in variants
            if isinstance(v, dict)
            and (variant_id is None or str(v.get("id")) == str(variant_id))
            and (sku is None or v.get("sku") == sku)
            and (variant_title is None or v.get("title") == variant_title)
        ]
        if len(matches) != 1:
            raise SupplierApiError(
                "Shopify JSON variant ID, SKU, or title differs from the registered kit."
            )
        var = matches[0]
        price_dec = _money_decimal(var.get("price"))
        avail_raw = var.get("available")
        inv_qty = _parse_stock_qty(var.get("inventory_quantity"))
        if not isinstance(avail_raw, bool):
            avail = "InStock" if (inv_qty is not None and inv_qty > 0) else "unknown"
        else:
            avail = "InStock" if avail_raw else "OutOfStock"
        quote_obj = {
            "supplier": supplier,
            "adapter": "shopify_json_v1",
            "mpn": str(var.get("sku") or sku or ""),
            "sku": str(var.get("sku") or sku or ""),
            "product_name": str(prod.get("title") or handle)[:300],
            "currency": "USD",
            "pack_size": max(1, int(pack_size)),
            "min_order_qty": 1,
            "price_breaks": [{"minimum_packs": 1, "pack_price": _money_str(price_dec)}],
            "availability": avail,
            "stock_quantity": inv_qty,
            "lead_time_days": None,
            "lead_time_text": None,
            "variant_id": str(var.get("id") or ""),
            "variant_title": str(var.get("title") or "") or None,
            "source_url": safe,
            "datasheet_url": None,
            "evidence_sha256": sha,
            "parser": "shopify-product-json-v1",
            "fetched_at": _iso(_now()),
        }
        return _convert_quote_currency(quote_obj, curr, fx_rates)

    if client is not None:
        return await _run(client)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(12, connect=5), follow_redirects=False
    ) as active:
        return await _run(active)


async def fetch_official_quote(
    item_spec: dict[str, Any],
    *,
    currency: str = "USD",
    fx_rates: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch a live supplier quote via DigiKey v4, Mouser v2, Shopify JSON, or reviewed HTML."""
    from app.supplier_catalogs import PRODUCTS, CatalogError, fetch_product

    curr = _validate_currency(currency)
    cache_id = str(
        item_spec.get("catalog_id")
        or item_spec.get("digikey_pn")
        or item_spec.get("mouser_pn")
        or item_spec.get("mpn")
        or item_spec.get("sku")
        or ""
    ).strip()
    now = _now()
    if cache_id:
        cached = _quote_cache.get((cache_id, curr, "official"))
        if cached and now < cached[1]:
            return {**deepcopy(cached[0]), "cache_hit": True}

    dk = DigiKeyClient()
    mouser = MouserClient()
    errors: list[str] = []

    dk_pn = item_spec.get("digikey_pn") or item_spec.get("mpn")
    if dk.is_configured() and dk_pn:
        try:
            q = await dk.fetch_product_details(str(dk_pn), currency=curr, client=client)
            if cache_id:
                _quote_cache[(cache_id, curr, "official")] = (
                    deepcopy(q),
                    now + timedelta(seconds=QUOTE_CACHE_TTL_SECONDS),
                )
            return q
        except SupplierApiError as exc:
            errors.append(str(exc))

    ms_pn = item_spec.get("mouser_pn") or item_spec.get("mpn")
    if mouser.is_configured() and ms_pn:
        try:
            q = await mouser.fetch_part_number(
                str(ms_pn), currency=curr, fx_rates=fx_rates, client=client
            )
            if cache_id:
                _quote_cache[(cache_id, curr, "official")] = (
                    deepcopy(q),
                    now + timedelta(seconds=QUOTE_CACHE_TTL_SECONDS),
                )
            return q
        except SupplierApiError as exc:
            errors.append(str(exc))

    option_id = item_spec.get("option_id") or item_spec.get("catalog_id")
    reg_product = PRODUCTS.get(str(option_id)) if option_id else None
    if reg_product is not None:
        if reg_product.adapter == "shopify":
            try:
                q = await fetch_shopify_product_json(
                    reg_product.url,
                    supplier=reg_product.supplier,
                    product_id=reg_product.product_id,
                    variant_id=reg_product.variant_id,
                    sku=reg_product.sku,
                    variant_title=reg_product.variant_title,
                    pack_size=reg_product.pack_size,
                    currency=curr,
                    fx_rates=fx_rates,
                    client=client,
                )
                if cache_id:
                    _quote_cache[(cache_id, curr, "official")] = (
                        deepcopy(q),
                        now + timedelta(seconds=QUOTE_CACHE_TTL_SECONDS),
                    )
                return q
            except SupplierApiError as exc:
                errors.append(str(exc))
        if client is not None:
            try:
                obs = await fetch_product(reg_product, client)
                obs.setdefault("mpn", reg_product.mpn or reg_product.sku)
                obs.setdefault("adapter", reg_product.adapter)
                obs.setdefault("fetched_at", _iso(now))
                q = _convert_quote_currency(obs, curr, fx_rates)
                if cache_id:
                    _quote_cache[(cache_id, curr, "official")] = (
                        deepcopy(q),
                        now + timedelta(seconds=QUOTE_CACHE_TTL_SECONDS),
                    )
                return q
            except CatalogError as exc:
                errors.append(str(exc))
        else:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10, connect=5),
                follow_redirects=False,
                headers={"User-Agent": "AutoLab/0.1 (read-only product price check)"},
            ) as active:
                try:
                    obs = await fetch_product(reg_product, active)
                    obs.setdefault("mpn", reg_product.mpn or reg_product.sku)
                    obs.setdefault("adapter", reg_product.adapter)
                    obs.setdefault("fetched_at", _iso(now))
                    q = _convert_quote_currency(obs, curr, fx_rates)
                    if cache_id:
                        _quote_cache[(cache_id, curr, "official")] = (
                            deepcopy(q),
                            now + timedelta(seconds=QUOTE_CACHE_TTL_SECONDS),
                        )
                    return q
                except CatalogError as exc:
                    errors.append(str(exc))

    raise SupplierApiError("; ".join(errors) or "No configured official supplier adapter matched.")


async def search_official_suppliers(
    query: str,
    *,
    suppliers: list[str] | None = None,
    currency: str = "USD",
    max_results: int = 6,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Search configured DigiKey/Mouser APIs plus local purchasable catalog references."""
    from app.bom_engine import load_purchasable_catalog

    curr = _validate_currency(currency)
    clean_query = " ".join(str(query or "").split())[:160]
    if not clean_query:
        raise SupplierApiError("Supplier search query must be non-empty.")

    requested = {s.lower().strip() for s in suppliers} if suppliers else {"digikey", "mouser"}
    caps = supplier_api_capabilities()
    api_results: list[dict[str, Any]] = []
    warnings: list[str] = []

    fx_rates: dict[str, Any] | None = None
    if curr != "USD" and ("mouser" in requested or True):
        try:
            fx_rates = await fetch_fx_rates("USD", client=client)
        except SupplierApiError as exc:
            warnings.append(str(exc))

    dk = DigiKeyClient()
    if "digikey" in requested:
        if dk.is_configured():
            try:
                api_results.extend(
                    await dk.search_keyword(
                        clean_query, currency=curr, limit=max_results, client=client
                    )
                )
            except SupplierApiError as exc:
                warnings.append(str(exc))
        else:
            warnings.append(
                "DigiKey API key not configured (DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET)."
            )

    mouser = MouserClient()
    if "mouser" in requested:
        if mouser.is_configured():
            try:
                api_results.extend(
                    await mouser.search_keyword(
                        clean_query,
                        currency=curr,
                        fx_rates=fx_rates,
                        limit=max_results,
                        client=client,
                    )
                )
            except SupplierApiError as exc:
                warnings.append(str(exc))
        else:
            warnings.append("Mouser API key not configured (MOUSER_API_KEY).")

    tokens = [tok for tok in re.findall(r"[a-z0-9]{2,}", clean_query.lower())]
    catalog_matches: list[dict[str, Any]] = []
    for item in (load_purchasable_catalog().get("items") or {}).values():
        hay = " ".join(
            [
                str(item.get("id", "")),
                str(item.get("label", "")),
                str(item.get("category", "")),
                str(item.get("mpn", "")),
                str(item.get("supplier", "")),
                json.dumps(item.get("specs") or {}),
            ]
        ).lower()
        hits = sum(1 for tok in tokens if tok in hay)
        if hits > 0:
            ref_usd = item.get("reference_unit_price_usd")
            converted_ref = (
                convert_currency(ref_usd, "USD", curr, fx_rates)
                if (ref_usd is not None and (curr == "USD" or fx_rates is not None))
                else None
            )
            catalog_matches.append(
                {
                    "catalog_id": item["id"],
                    "label": item["label"],
                    "category": item["category"],
                    "supplier": item.get("supplier"),
                    "mpn": item.get("mpn"),
                    "sku": item.get("sku"),
                    "pack_size": item.get("pack_size", 1),
                    "reference_unit_price": converted_ref,
                    "reference_currency": curr if converted_ref is not None else "USD",
                    "reference_unit_price_usd": ref_usd,
                    "typical_lead_time_days": item.get("typical_lead_time_days"),
                    "source_url": item.get("source_url"),
                    "specs": item.get("specs") or {},
                    "price_basis": "dated_curated_reference",
                    "_hits": hits,
                }
            )
    catalog_matches.sort(key=lambda m: (-m["_hits"], m["label"]))
    for entry in catalog_matches:
        entry.pop("_hits", None)

    return {
        "query": clean_query,
        "currency": curr,
        "fx_rates": fx_rates,
        "api_capabilities": caps,
        "live_api_results": api_results[:max_results],
        "catalog_matches": catalog_matches[:max_results],
        "warnings": warnings,
    }
