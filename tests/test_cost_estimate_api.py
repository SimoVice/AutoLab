"""Revision-checked cost reads and refreshes use the same local purchasing evidence."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from test_custom_project_routes import client as client
from test_custom_project_routes import install, project_fixture
from test_price_estimation import declare

import app.api as api
from app.domain import new_project


def test_get_and_concurrent_refresh_agree_without_mutating_project(client):
    project = declare(project_fixture(), "concept:rail_base")
    route = install(project) + "/cost-estimate"
    before = deepcopy(project)
    payload = {"project_revision": project["revision"], "force": True}
    read = client.get(route, params={"revision": project["revision"]}).json()["estimate"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        replies = list(pool.map(lambda _: client.post(route, json=payload), range(6)))
    for reply in replies:
        assert reply.status_code == 200
        data = reply.json()["estimate"]
        assert data["rows"] == read["rows"] and data["summary"] == read["summary"]
        assert data["project_revision"] == project["revision"]
    assert before == api._sessions[project["id"]].project
    assert read["summary"]["documented_subtotal"] == 50.12
    assert read["summary"]["planning_low"] == 80


def test_read_ignores_untrusted_cached_report_and_rejects_old_revision(client):
    project = project_fixture()
    route = install(project) + "/cost-estimate"
    api._sessions[project["id"]].cost_estimate = {"rows": [{"low_usd": 99999}]}
    assert "99999" not in client.get(route, params={"revision": project["revision"]}).text
    assert client.get(route, params={"revision": project["revision"] + 1}).status_code == 409
    assert client.post(route, json={"project_revision": project["revision"] + 1}).status_code == 409


@pytest.mark.parametrize("extra", [{"force": "true"}, {"model": "untrusted"}, {"planning_low": 1}])
def test_refresh_rejects_untrusted_cost_inputs(client, extra):
    project = project_fixture()
    route = install(project) + "/cost-estimate"
    assert (
        client.post(route, json={"project_revision": project["revision"], **extra}).status_code
        == 422
    )


def test_no_design_cannot_request_purchase_costs(client):
    project = new_project()
    route = install(project) + "/cost-estimate"
    assert client.post(route, json={"project_revision": project["revision"]}).status_code == 409
    report = client.get(route, params={"revision": project["revision"]}).json()["estimate"]
    assert report["summary"]["documented_subtotal"] is None and report["rows"] == []
