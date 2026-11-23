"""Account discovery and task-local model selection never rewrite project inputs."""

import asyncio
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

import app.api as api
import app.model_catalog as catalog
import app.model_client as model_client
from app.model_client import (
    DEFAULT_MODEL,
    ExtractedTurn,
    ModelClientError,
    ModelSettings,
    using_model,
)


@pytest.fixture(autouse=True)
def local_configuration(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", DEFAULT_MODEL)
    catalog._cache.clear()
    api._sessions.clear()
    yield
    catalog._cache.clear()
    api._sessions.clear()


def test_discovery_cache_and_credential_isolation():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"id": "gpt-6.2-sol"}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            settings = ModelSettings("test-key-1", "https://api.openai.com/v1", DEFAULT_MODEL)
            first = await catalog.discover_catalog(settings=settings, client=client)
            first["ids"].clear()
            second = await catalog.discover_catalog(settings=settings, client=client)
            assert second["ids"] == ["gpt-6.2-sol"]
            await catalog.discover_catalog(
                settings=ModelSettings("test-key-2", settings.base_url, DEFAULT_MODEL),
                client=client,
            )

    asyncio.run(run())
    assert len(calls) == 2
    assert all(request.method == "GET" for request in calls)
    assert all("test-key" not in fingerprint for fingerprint in catalog._cache)


def test_legacy_provider_configuration_cannot_supply_openai_credentials(monkeypatch):
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(key)
    monkeypatch.setattr(model_client, "dotenv_values", lambda path: {
        "LEGACY_API_KEY": "old-provider-secret",
        "LEGACY_API_BASE_URL": "https://legacy.invalid/v1",
        "LEGACY_MODEL": "legacy-unsupported-model",
    })
    with pytest.raises(ModelClientError, match="OPENAI_API_KEY"):
        ModelSettings.load()
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-key")
    settings = ModelSettings.load()
    assert (settings.api_key, settings.base_url, settings.model) == (
        "synthetic-openai-key", "https://api.openai.com/v1", DEFAULT_MODEL,
    )


def test_old_server_session_changes_future_model_and_retains_provenance(monkeypatch):
    from evolution_fixtures import layout_project

    project = layout_project()
    project["engineering_design"]["provenance"]["model"] = "legacy-unsupported-model"
    saved = {"project": deepcopy(project), "history": [], "model": "legacy-unsupported-model"}
    monkeypatch.setattr(api.project_library, "load", lambda project_id: deepcopy(saved))
    session = api.session_for(project["id"])
    assert session.project == project
    assert session.model == DEFAULT_MODEL


def test_failed_discovery_is_honest_and_sanitized():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(403, json={"secret": "never expose this"})
            )
        ) as client:
            result = await catalog.discover_catalog(client=client)
        assert result["verified"] is False
        assert result["ids"] == []
        assert "never expose" not in str(result)
        assert catalog.availability(result, DEFAULT_MODEL)["available"] is False

    asyncio.run(run())


def test_selection_preserves_project_history_and_scope(monkeypatch):
    async def discover():
        return {"ids": list(catalog.AGENT_MODELS), "reason": None, "verified": True}

    monkeypatch.setattr(api, "discover_catalog", discover)
    monkeypatch.setattr(catalog, "discover_catalog", discover)
    observed = []

    async def generate(project, message, history):
        observed.append(ModelSettings.load().model)
        return ExtractedTurn(assistant_message="What equipment do you have?"), {
            "model": observed[-1]
        }

    monkeypatch.setattr(api, "generate_turn", generate)
    with TestClient(api.app) as client:
        original = client.post("/api/projects").json()["project"]
        other = client.post("/api/projects").json()["project"]
        pid = original["id"]
        session = api._sessions[pid]
        before = deepcopy(session.project), deepcopy(session.history)
        selected = client.post(f"/api/projects/{pid}/model", json={"model": "gpt-6.2-sol"})
        assert selected.status_code == 200
        assert selected.json()["project"] == original
        assert (session.project, session.history) == before
        models = client.get("/api/models", params={"project_id": pid}).json()
        assert models["active_model"] == "gpt-6.2-sol"
        assert models["default_model"] == DEFAULT_MODEL
        assert models["capabilities"]["image"]["available"] is False
        assert "synthetic-test-key" not in str(models)
        assert (
            client.post(f"/api/projects/{pid}/messages", json={"message": "Help"}).status_code
            == 200
        )
        client.post(f"/api/projects/{other['id']}/messages", json={"message": "Help"})
        assert observed == ["gpt-6.2-sol", DEFAULT_MODEL]
        assert ModelSettings.load().model == DEFAULT_MODEL


@pytest.mark.parametrize(
    "model,status",
    [("arbitrary-provider-model", 422), ("gpt-image-2.6-flare", 422), ("gpt-6.2-sol", 409)],
)
def test_rejected_model_selection_does_not_mutate(monkeypatch, model, status):
    async def discover():
        return {"ids": [DEFAULT_MODEL], "reason": None, "verified": True}

    monkeypatch.setattr(api, "discover_catalog", discover)
    with TestClient(api.app) as client:
        project = client.post("/api/projects").json()["project"]
        response = client.post(f"/api/projects/{project['id']}/model", json={"model": model})
        assert response.status_code == status
        session = api._sessions[project["id"]]
        assert session.project == project
        assert session.history == []
        assert session.model == DEFAULT_MODEL


def test_model_failure_preserves_selected_model_and_state(monkeypatch):
    async def fail(*args):
        assert ModelSettings.load().model == "gpt-6.2-sol"
        raise ModelClientError("Synthetic provider failure")

    monkeypatch.setattr(api, "generate_turn", fail)
    with TestClient(api.app) as client:
        project = client.post("/api/projects").json()["project"]
        session = api._sessions[project["id"]]
        session.model = "gpt-6.2-sol"
        response = client.post(f"/api/projects/{project['id']}/messages", json={"message": "Help"})
        assert response.status_code == 502
        assert session.project == project
        assert session.history == []
        assert session.model == "gpt-6.2-sol"
        assert ModelSettings.load().model == DEFAULT_MODEL


def test_selection_is_task_local_and_exception_safe():
    async def read(model):
        with using_model(model):
            await asyncio.sleep(0)
            return ModelSettings.load().model

    async def run():
        assert await asyncio.gather(read("gpt-6.1-terra"), read("gpt-6.2-sol")) == [
            "gpt-6.1-terra",
            "gpt-6.2-sol",
        ]
        with pytest.raises(RuntimeError):
            with using_model("gpt-6.2-sol"):
                raise RuntimeError("synthetic failure")
        assert ModelSettings.load().model == DEFAULT_MODEL

    asyncio.run(run())


def test_missing_credentials_preserve_default_and_report_unknown(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setattr(model_client, "dotenv_values", lambda path: {})
    with TestClient(api.app) as client:
        models = client.get("/api/models").json()
        assert models["default_model"] == DEFAULT_MODEL
        assert models["active_model"] == DEFAULT_MODEL
        assert all(option["available"] is False for option in models["models"])
        assert all(
            "OPENAI_API_KEY" in option["unavailable_reason"] for option in models["models"]
        )
        assert models["capabilities"]["voice"]["available"] is False
        assert "successful inference is not guaranteed" in models["availability_basis"]


def test_advertised_catalog_does_not_claim_inference_success(monkeypatch):
    async def discover():
        return {
            "ids": [DEFAULT_MODEL, catalog.VOICE_MODEL, catalog.IMAGE_MODEL],
            "reason": None,
            "verified": True,
        }

    monkeypatch.setattr(catalog, "discover_catalog", discover)
    models = asyncio.run(catalog.model_options())
    assert models["models"][0]["available"] is True
    assert models["capabilities"]["voice"]["available"] is True
    assert models["capabilities"]["image"]["available"] is True
    assert models["availability_basis"].startswith("Account catalog;")
