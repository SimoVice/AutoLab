"""Offline tests for DigiKey v4, Mouser v2, Shopify JSON, and ECB multi-currency FX APIs."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx
import pytest

from app import procurement
from app.catalog import build_sourcing
from app.domain import new_project
from app.supplier_apis import (
    DigiKeyClient,
    MouserClient,
    SupplierApiError,
    clear_supplier_api_caches,
    convert_currency,
    fetch_fx_rates,
    fetch_official_quote,
    fetch_shopify_product_json,
    supplier_api_capabilities,
)
from app.supplier_catalogs import PRODUCTS


@pytest.fixture(autouse=True)
def clean_caches(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTOLAB_DISABLE_DOTENV_SECRETS", "1")
    clear_supplier_api_caches()
    procurement._cache.clear()
    yield
    clear_supplier_api_caches()
    procurement._cache.clear()


def test_part_number_search_distinguishes_related_ambiguous_and_unavailable_offers(monkeypatch):
    monkeypatch.setenv("MOUSER_API_KEY", "fixture")
    base = {"Description": "Optical detector", "AvailabilityInStock": 20,
            "PriceBreaks": [{"Quantity": 1, "Price": "$2.00", "Currency": "USD"}]}
    related = {**base, "ManufacturerPartNumber": "OPT-AA", "MouserPartNumber": "CODE-AA"}
    correct = {**base, "ManufacturerPartNumber": "OPT-A", "MouserPartNumber": "CODE-A"}
    second = {**correct, "MouserPartNumber": "CODE-A-REEL"}
    rows = [related, correct]
    transport = httpx.MockTransport(lambda _: httpx.Response(
        200, json={"SearchResults": {"Parts": rows}}
    ))
    async def lookup(number):
        async with httpx.AsyncClient(transport=transport) as client:
            return await MouserClient().fetch_part_number(number, client=client)

    assert asyncio.run(lookup("opt-a"))["sku"] == "CODE-A"
    rows.append(second)
    with pytest.raises(SupplierApiError, match="unique exact"):
        asyncio.run(lookup("OPT-A"))
    assert asyncio.run(lookup("CODE-A-REEL"))["sku"] == "CODE-A-REEL"
    rows[:] = [related]
    with pytest.raises(SupplierApiError, match="unique exact"):
        asyncio.run(lookup("OPT-A"))
    rows[:] = [{**correct, "AvailabilityInStock": 0, "PriceBreaks": []}]
    observed = asyncio.run(lookup("CODE-A"))
    assert observed["availability"] == "OutOfStock" and observed["price_breaks"] == []


def test_digikey_matches_the_distributor_pack_in_a_multivariant_part():
    product = {
        "ManufacturerProductNumber": "OPT-A", "QuantityAvailable": 20,
        "ProductVariations": [
            {"DigiKeyProductNumber": sku,
             "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": price}]}
            for sku, price in (("REEL-A", 50), ("SINGLE-A", 2))
        ],
    }

    def lookup(number):
        return DigiKeyClient()._parse_product(
            product, currency="USD", sha="abc", part_number=number
        )

    assert lookup("SINGLE-A")["price_breaks"][0]["pack_price"] == "2.00"
    assert lookup("OPT-A") is None and lookup("SINGLE-AB") is None


def test_fx_rates_fetch_cache_and_conversion():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "amount": 1.0,
                "base": "USD",
                "date": "2026-11-23",
                "rates": {"CHF": 0.88, "EUR": 0.92},
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await fetch_fx_rates("USD", client=client)
            second = await fetch_fx_rates("USD", client=client)
            return first, second

    fx1, fx2 = asyncio.run(run())
    assert len(calls) == 1
    assert fx1["rates"] == {"USD": 1.0, "CHF": 0.88, "EUR": 0.92}
    assert fx2["rate_date"] == "2026-11-23"
    assert convert_currency(Decimal("100.00"), "USD", "CHF", fx1) == 88.0
    assert convert_currency(Decimal("100.00"), "USD", "EUR", fx1) == 92.0
    with pytest.raises(SupplierApiError):
        convert_currency(Decimal("10.00"), "USD", "JPY", fx1)


def test_digikey_v4_oauth_product_details_and_keyword_search(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "dk-test-id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "dk-test-secret")
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        url = str(request.url)
        if url == "https://api.digikey.com/v1/oauth2/token":
            token_calls += 1
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 600})
        if "/products/v4/search/2183-1209-ND/productdetails" in url:
            assert request.headers["Authorization"] == "Bearer tok-123"
            assert request.headers["X-DIGIKEY-Locale-Site"] == "CH"
            assert request.headers["X-DIGIKEY-Locale-Currency"] == "CHF"
            return httpx.Response(
                200,
                json={
                    "Product": {
                        "ManufacturerProductNumber": "SY35ST36-1004A",
                        "Description": {"ProductDescription": "STEPPER MOTOR NEMA 14"},
                        "ProductUrl": "https://www.digikey.ch/product/2183-1209-ND",
                        "DatasheetUrl": "https://example.org/ds.pdf",
                        "QuantityAvailable": 42,
                        "ManufacturerLeadWeeks": 3,
                        "ProductStatus": {"Status": "Active"},
                        "Classifications": {"RohsStatus": "ROHS3 Compliant"},
                        "ProductVariations": [
                            {
                                "DigiKeyProductNumber": "2183-1209-ND",
                                "MinimumOrderQuantity": 1,
                                "StandardPricing": [
                                    {"BreakQuantity": 1, "UnitPrice": 31.5},
                                    {"BreakQuantity": 10, "UnitPrice": 28.0},
                                ],
                            }
                        ],
                    }
                },
            )
        if "/products/v4/search/keyword" in url:
            return httpx.Response(
                200,
                json={
                    "Products": [
                        {
                            "ManufacturerProductNumber": "LRS-150-24",
                            "Description": {"ProductDescription": "AC/DC CONVERTER 24V 156W"},
                            "ProductUrl": "https://www.digikey.com/product/LRS-150-24",
                            "QuantityAvailable": 120,
                            "ProductVariations": [
                                {
                                    "DigiKeyProductNumber": "1866-3316-ND",
                                    "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 24.5}],
                                }
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            dk = DigiKeyClient()
            detail = await dk.fetch_product_details("2183-1209-ND", currency="CHF", client=client)
            found = await dk.search_keyword("24V power supply", currency="USD", client=client)
            return detail, found

    detail, found = asyncio.run(run())
    assert token_calls == 1
    assert detail["supplier"] == "DigiKey"
    assert detail["currency"] == "CHF"
    assert detail["stock_quantity"] == 42
    assert detail["lead_time_days"] == 21
    assert detail["price_breaks"][0]["pack_price"] == "31.50"
    assert len(found) == 1 and found[0]["mpn"] == "LRS-150-24"


def test_mouser_v2_part_search_and_currency_conversion(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOUSER_API_KEY", "mouser-secret-key")
    fx = {
        "base": "USD",
        "rates": {"USD": 1.0, "CHF": 0.9, "EUR": 0.95},
        "rate_date": "2026-11-23",
        "source": "ECB",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "apiKey=mouser-secret-key" in str(request.url)
        return httpx.Response(
            200,
            json={
                "SearchResults": {
                    "Parts": [
                        {
                            "ManufacturerPartNumber": "SC0915",
                            "MouserPartNumber": "485-4864",
                            "Description": "Raspberry Pi Pico RP2040",
                            "AvailabilityInStock": "350 In Stock",
                            "LeadTime": "2 Weeks",
                            "Min": "1",
                            "ProductDetailUrl": "https://www.mouser.com/ProductDetail/485-4864",
                            "DataSheetUrl": "https://datasheets.raspberrypi.com/pico.pdf",
                            "ROHSStatus": "RoHS Compliant",
                            "LifecycleStatus": "New Product",
                            "PriceBreaks": [{"Quantity": 1, "Price": "$4.00", "Currency": "USD"}],
                        }
                    ]
                }
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await MouserClient().fetch_part_number(
                "485-4864", currency="EUR", fx_rates=fx, client=client
            )

    quote = asyncio.run(run())
    assert quote["supplier"] == "Mouser"
    assert quote["stock_quantity"] == 350
    assert quote["lead_time_days"] == 14
    assert quote["currency"] == "EUR"
    assert quote["price_breaks"] == [{"minimum_packs": 1, "pack_price": "3.80"}]


def test_shopify_product_json_validation_and_rejection():
    prod = PRODUCTS["jubilee-crossbar"]
    handle = prod.url.rsplit("/", 1)[-1]

    def good_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"https://mandalaroseworks.com/products/{handle}.json"
        return httpx.Response(
            200,
            json={
                "product": {
                    "id": int(prod.product_id),
                    "handle": handle,
                    "title": "Mandala Jubilee Crossbar",
                    "variants": [
                        {
                            "id": int(prod.variant_id),
                            "sku": prod.sku,
                            "title": prod.variant_title,
                            "price": "59.00",
                            "available": True,
                            "inventory_quantity": 8,
                        }
                    ],
                }
            },
        )

    async def run_good():
        async with httpx.AsyncClient(transport=httpx.MockTransport(good_handler)) as client:
            return await fetch_shopify_product_json(
                prod.url,
                supplier=prod.supplier,
                product_id=prod.product_id,
                variant_id=prod.variant_id,
                sku=prod.sku,
                variant_title=prod.variant_title,
                client=client,
            )

    res = asyncio.run(run_good())
    assert res["adapter"] == "shopify_json_v1"
    assert res["availability"] == "InStock"
    assert res["stock_quantity"] == 8
    assert res["price_breaks"] == [{"minimum_packs": 1, "pack_price": "59.00"}]

    def mismatch_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"product": {"id": 999, "handle": handle, "variants": []}},
        )

    async def run_bad():
        async with httpx.AsyncClient(transport=httpx.MockTransport(mismatch_handler)) as client:
            return await fetch_shopify_product_json(
                prod.url, product_id=prod.product_id, variant_id=prod.variant_id, client=client
            )

    with pytest.raises(SupplierApiError):
        asyncio.run(run_bad())


def test_missing_api_keys_reported_cleanly_and_procurement_refresh_supports_chf(
    monkeypatch: pytest.MonkeyPatch,
):
    caps = supplier_api_capabilities()
    assert caps["digikey_v4"]["configured"] is False
    assert caps["mouser_v2"]["configured"] is False
    assert caps["shopify_json"]["configured"] is True

    monkeypatch.setenv("MOUSER_API_KEY", "live-mock")
    project = new_project()
    sourcing = build_sourcing({}, False)
    pico_group = next(g for g in sourcing["groups"] if g["id"] == "controller")
    project["sourcing"]["groups"] = [pico_group]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.frankfurter.dev" in url:
            return httpx.Response(
                200,
                json={"base": "USD", "date": "2026-11-23", "rates": {"CHF": 0.9, "EUR": 0.95}},
            )
        if "api.mouser.com" in url:
            return httpx.Response(
                200,
                json={
                    "SearchResults": {
                        "Parts": [
                            {
                                "ManufacturerPartNumber": "SC0915",
                                "MouserPartNumber": "485-4864",
                                "Description": "Raspberry Pi Pico",
                                "AvailabilityInStock": "100 In Stock",
                                "LeadTime": "7 Days",
                                "ProductDetailUrl": "https://www.mouser.com/485-4864",
                                "PriceBreaks": [
                                    {"Quantity": 1, "Price": "$4.00", "Currency": "USD"}
                                ],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(404)

    fx_sample = {
        "base": "USD",
        "rates": {"USD": 1.0, "CHF": 0.9, "EUR": 0.95},
        "rate_date": "2026-11-23",
        "source": "ECB",
    }

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            q = await fetch_official_quote(
                {"option_id": "pico", "mouser_pn": "485-4864"},
                currency="CHF",
                fx_rates=fx_sample,
                client=client,
            )
            rep = await procurement.refresh_procurement(
                project, client=client, currency="CHF", use_official_apis=True
            )
            return q, rep

    q, rep = asyncio.run(run())
    assert q["price_breaks"] == [{"minimum_packs": 1, "pack_price": "3.60"}]
    assert rep["currency"] == "CHF"
    assert rep["fx_rate_used"] == 0.9
    assert rep["rows"][0]["unit_price"] == 3.6
    assert rep["rows"][0]["stock_quantity"] == 100
    assert rep["rows"][0]["lead_time_days"] == 7
