"""Offline bench purchase scopes and model allowances, never supplier evidence."""

import asyncio
from copy import deepcopy
from itertools import combinations

import pytest
from test_price_estimation import declare

from app import price_estimation as prices
from app import procurement
from app.bench_station import StationProposal, compile_station
from app.domain import new_project

OPERATIONS = ['weigh', 'print_label', 'read_barcode', 'inspect']


def station_project(operations=OPERATIONS):
    project = new_project()
    project['bench_station'] = compile_station(StationProposal(operations=operations))
    project.pop('engineering_design', None)
    return project


@pytest.mark.parametrize('operations', [list(c) for n in range(1, 5)
                                         for c in combinations(OPERATIONS, n)])
def test_purchase_groups_follow_only_current_station_operations(operations):
    project = station_project(operations)
    original = deepcopy(project)
    rows = procurement.initial_procurement_rows(project)
    assert project == original
    assert len(rows) == 2 * len(operations)
    assert len({row['group_id'] for row in rows}) == len(rows)
    assert {cid for row in rows for cid in row['component_ids']} == set(operations)
    assert all(row['group_id'].startswith('bench-') and row['is_candidate'] for row in rows)
    assert all(row['quantity'] == 1 and row['line_total'] is None for row in rows)
    assert not ({'pipette', 'controller', 'upstream-platform'} &
                {row['group_id'] for row in rows})


def test_only_reviewed_camera_candidate_can_fetch_catalog(monkeypatch):
    project = station_project()
    original = deepcopy(project)
    calls = []

    async def unavailable(product, client):
        calls.append(product.option_id)
        raise procurement.CatalogError('Offline synthetic unavailable catalog')

    monkeypatch.setattr(procurement, 'fetch_product', unavailable)
    monkeypatch.setattr(procurement, '_cache', {})
    report = asyncio.run(procurement.refresh_procurement(project))
    assert calls == ['camera-module-3']
    assert project == original
    assert report['incomplete'] and report['known_partial_subtotal'] == 0
    assert report['coverage']['supported_catalog_count'] == 1
    assert all(row['line_total'] is None for row in report['rows'])
    for row in report['rows']:
        if row['group_id'] in {'bench-weigh-equipment', 'bench-print_label-equipment',
                               'bench-read_barcode-equipment'}:
            assert row['status'] == 'quote_needed'
            assert row['freshness'] == 'unverified'


def test_station_prices_follow_selected_equipment_and_declared_supplies():
    project = station_project(["weigh", "inspect"])
    base = prices.purchase_cost_report(project)
    assert (base["summary"]["documented_subtotal"], base["summary"]["planning_low"]) == (25, None)
    assert base["summary"]["printed_line_count"] == 0
    project = declare(project, "bench-inspect-supplies")
    result = prices.purchase_cost_report(project)
    assert result["summary"]["documented_subtotal"] == 25
    assert result["summary"]["planning_low"] == 80 and result["summary"]["planning_high"] == 120
    assert result["summary"]["missing_price_count"] == 3
    assert all(row["group_id"].startswith("bench-") for row in result["rows"])


def test_station_without_observations_or_declarations_has_no_amount():
    result = prices.purchase_cost_report(station_project(["weigh", "read_barcode"]))
    assert result["summary"]["documented_subtotal"] is result["summary"]["planning_low"] is None
    assert result["summary"]["missing_price_count"] == 4
