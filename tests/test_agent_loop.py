"""Tests for autonomous multi-step OpenAI tool calling, self-repair, budgets, and live UI."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

from app import api
from app.agent_loop import AgentBudget, run_agent_turn
from app.chat_activity import _activities
from app.domain import apply_update, new_project
from app.model_client import DEFAULT_MODEL, ModelSettings
from app.openai_models import REASONING_MODEL

TEST_SETTINGS = ModelSettings(
    api_key="synthetic-key",
    base_url="https://api.openai.com/v1",
    model=DEFAULT_MODEL,
)


def _tool_reply(tool_calls: list[dict], *, content: str | None = None, model: str = DEFAULT_MODEL):
    return {
        "model": model,
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
    }


def _text_reply(content: str, *, model: str = DEFAULT_MODEL, tokens: int = 40):
    return {
        "model": model,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": tokens},
    }


def _tc(call_id: str, name: str, args: dict | str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": args if isinstance(args, str) else json.dumps(args),
        },
    }


@pytest.fixture(autouse=True)
def reset_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", DEFAULT_MODEL)
    monkeypatch.delenv("AUTOLAB_AGENT_MODE", raising=False)
    monkeypatch.setenv("AUTOLAB_LAB_PROFILE_PATH", str(tmp_path / "lab-profile.json"))
    api._sessions.clear()
    _activities.clear()
    yield
    api._sessions.clear()
    _activities.clear()


def test_multistep_bilingual_chain_and_session_model_propagation(monkeypatch):
    project = new_project()
    session = api.ProjectSession(project=project, model=REASONING_MODEL)
    api._sessions[project["id"]] = session
    payloads: list[dict] = []

    steps = iter(
        [
            _tool_reply(
                [
                    _tc(
                        "call_1",
                        "update_project_brief",
                        {
                            "workflow_kind": "general",
                            "objective": "Stazione da banco per pesata e ispezione visiva",
                            "workspace_width_mm": 900.0,
                            "workspace_depth_mm": 600.0,
                            "workspace_height_mm": 500.0,
                        },
                    )
                ],
                model=REASONING_MODEL,
            ),
            _tool_reply(
                [_tc("call_2", "develop_bench_station", {"operations": ["weigh", "inspect"]})],
                model=REASONING_MODEL,
            ),
            _tool_reply(
                [_tc("call_3", "check_spatial_fit", {"include_printable_parts": False})],
                model=REASONING_MODEL,
            ),
            _text_reply(
                "Stazione da banco configurata con pesatura e ispezione visiva (900x600x500 mm).",
                model=REASONING_MODEL,
            ),
        ]
    )

    def handler(request):
        body = json.loads(request.content)
        payloads.append(body)
        return httpx.Response(200, json=next(steps))

    client_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    msg = (
        "Progetta una bench station per weigh campioni e inspect visualmente "
        "su un banco 900 x 600 x 500 mm."
    )
    with TestClient(api.app) as client:
        resp = client.post(f"/api/projects/{project['id']}/messages", json={"message": msg})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["assistant_message_origin"] == "agent_loop"
    assert [t["tool"] for t in data["tool_trace"]] == [
        "update_project_brief",
        "develop_bench_station",
        "check_spatial_fit",
    ]
    assert data["project"]["bench_station"]["proposal"]["operations"] == ["weigh", "inspect"]
    assert data["project"]["summary"]["workspace_depth_mm"] == 600.0
    assert data["usage"]["model"] == REASONING_MODEL
    assert all(p["model"] == REASONING_MODEL and p["reasoning_effort"] == "low" for p in payloads)
    assert all(p["store"] is False and p["parallel_tool_calls"] is False for p in payloads[:-1])


def test_autonomous_self_repair_recovers_from_3d_depth_overflow():
    project = apply_update(
        new_project(),
        {
            "workflow_kind": "liquid_preparation",
            "brief_process": "Dilution",
            "brief_materials": "Tubes",
            "workspace_width_mm": 1200.0,
            "workspace_depth_mm": 700.0,
            "workspace_height_mm": 750.0,
        },
    )
    seen_tool_messages: list[dict] = []

    steps = iter(
        [
            # First step selects OT-2, which overflows depth (970 > 700) and height (810 > 750).
            _tool_reply([_tc("c1", "select_platform", {"platform_id": "opentrons-ot2"})]),
            # Second step receives recoverable diagnostics and checks spatial alternatives cleanly.
            _tool_reply([_tc("c2", "lookup_reviewed_references", {"query": "opentrons"})]),
            _text_reply(
                "Opentrons OT-2 requires 970 mm depth and 810 mm height, exceeding your "
                "700 x 750 mm depth/height envelope by 270 mm and 60 mm."
            ),
        ]
    )

    def handler(request):
        body = json.loads(request.content)
        tool_msgs = [m for m in body["messages"] if m.get("role") == "tool"]
        if tool_msgs:
            seen_tool_messages.append(json.loads(tool_msgs[-1]["content"]))
        return httpx.Response(200, json=next(steps))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_agent_turn(
                project,
                "Put an OT-2 on my 1200 x 700 x 750 mm bench.",
                [],
                client=client,
                settings=TEST_SETTINGS,
            )

    res = asyncio.run(run())
    assert res["repairs_used"] == 1
    assert res["tool_trace"][0]["ok"] is False
    assert res["tool_trace"][0]["recoverable"] is True
    assert res["tool_trace"][1]["ok"] is True
    assert not res["project"].get("design", {}).get("platform")
    assert seen_tool_messages[0]["ok"] is False
    assert "970" in seen_tool_messages[0]["summary"]


def test_budget_caps_and_atomic_rollback_on_unrecoverable_failure():
    # Hard conflict: liquid_preparation project with engineering_design -> develop_bench_station
    # is unrecoverable (recoverable=False), so any earlier mutations in the turn must roll back.
    from foundation_fixtures import liquid_project, source_proposal

    from app.design_spec import apply_candidate
    from app.foundation_design import adopt_foundation

    liquid = apply_candidate(adopt_foundation(liquid_project()), source_proposal(), {"model": "x"})
    original = deepcopy(liquid)

    steps = iter(
        [
            _tool_reply(
                [
                    _tc(
                        "c1",
                        "update_project_brief",
                        {"objective": "Attempted mutation in same turn"},
                    )
                ]
            ),
            _tool_reply(
                [_tc("c2", "develop_bench_station", {"operations": ["weigh"]})]
            ),
            _text_reply("Please start a new project for a separate bench station."),
        ]
    )

    def handler(request):
        return httpx.Response(200, json=next(steps))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_agent_turn(
                liquid,
                "Design a weighing station here.",
                [],
                client=client,
                settings=TEST_SETTINGS,
            )

    res = asyncio.run(run())
    # Objective update from step 1 was rolled back cleanly because step 2 failed unrecoverably.
    assert res["project"] == original

    # Enforce strict max_steps budget (at most 2 LLM requests).
    calls = 0

    def looping_handler(request):
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 2:
            assert {"tools", "parallel_tool_calls", "tool_choice"}.isdisjoint(payload)
        return httpx.Response(
            200,
            json=_tool_reply([_tc(f"loop_{calls}", "inspect_project_state", {})]),
        )

    async def run_budget():
        async with httpx.AsyncClient(transport=httpx.MockTransport(looping_handler)) as client:
            return await run_agent_turn(
                new_project(),
                "Keep checking",
                [],
                budget=AgentBudget(max_steps=2, max_tool_calls=5, max_repairs=1),
                client=client,
                settings=TEST_SETTINGS,
            )

    budget_res = asyncio.run(run_budget())
    assert calls == 2
    assert budget_res["usage"]["llm_calls"] == 2
    assert budget_res["assistant_message"]


def test_human_input_pause_stops_loop_immediately():
    from foundation_fixtures import liquid_project, source_proposal

    from app.design_spec import apply_candidate
    from app.foundation_design import adopt_foundation

    jubilee = apply_candidate(adopt_foundation(liquid_project()), source_proposal(), {"model": "x"})
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=_tool_reply(
                [
                    _tc(
                        "c1",
                        "configure_labware",
                        {"action": "configure_tube_rack", "count": 12, "columns": 4},
                    )
                ]
            ),
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_agent_turn(
                jubilee,
                "Use 12 tubes in 4 columns.",
                [],
                client=client,
                settings=TEST_SETTINGS,
            )

    res = asyncio.run(run())
    assert calls == 1
    assert "missing tube dimensions" in res["assistant_message"].lower()
    assert res["project"]["labware_brief"]["missing_fields"]


def test_live_activity_endpoint_streams_autonomous_steps_and_trace(monkeypatch):
    project = new_project()
    api._sessions[project["id"]] = api.ProjectSession(project=project)
    gate = asyncio.Event()
    step_seen = asyncio.Event()

    async def scripted_agent(proj, message, history, **kwargs):
        from app.chat_activity import report_activity

        report_activity(
            "designing",
            detail="Compiling bench station layout",
            tool="develop_bench_station",
            step=2,
            max_steps=6,
        )
        step_seen.set()
        await gate.wait()
        return {
            "project": proj,
            "assistant_message": "Done.",
            "usage": {"model": DEFAULT_MODEL, "total_tokens": 10},
            "recommendations": [],
            "action_runs": [],
            "tool_trace": [],
            "repairs_used": 0,
            "assistant_message_origin": "agent_loop",
        }

    monkeypatch.setattr(api, "run_agent_turn", scripted_agent)

    async def journey():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app), base_url="http://test"
        ) as client:
            req_id = "agent-live-request-12345"
            post = asyncio.create_task(
                client.post(
                    f"/api/projects/{project['id']}/messages",
                    headers={"X-Chat-Request-Id": req_id},
                    json={"message": "Build station"},
                )
            )
            await step_seen.wait()
            status = await client.get(f"/api/projects/{project['id']}/activity/{req_id}")
            assert status.status_code == 200
            body = status.json()
            assert body["active"] is True
            assert body["stage"] == "designing"
            assert body["tool"] == "develop_bench_station"
            assert body["step"] == 2 and body["max_steps"] == 6
            assert body["trace"][0]["tool"] == "develop_bench_station"
            gate.set()
            done = await post
            assert done.status_code == 200

    asyncio.run(journey())


def test_agent_loop_standalone_fixture_bed_repair_and_build_feedback(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOLAB_LAB_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.delenv("AUTOLAB_DATABASE_PATH", raising=False)
    monkeypatch.delenv("AUTOLAB_LIBRARY_DIRECTORY", raising=False)

    from app.lab_profile import save_lab_profile

    save_lab_profile(
        {
            "printer": {
                "name": "Bambu Lab A1 mini",
                "build_volume_mm": [180.0, 180.0, 180.0],
                "nozzle_mm": 0.4,
                "materials": ["PLA", "PETG"],
            }
        }
    )

    project = apply_update(new_project(), {"workflow_kind": "general"})
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            # First attempt sets 220 mm camera distance -> overflows 180 mm mast bed length.
            return httpx.Response(
                200,
                json=_tool_reply(
                    [
                        _tc(
                            "c1",
                            "configure_standalone_fixture",
                            {
                                "fixture_kind": "imaging_rig",
                                "object_reference_id": "slas_96_well_plate",
                                "camera_working_distance_mm": 220.0,
                            },
                        )
                    ]
                ),
            )
        if calls == 2:
            # Self-repair lowers working distance so camera-mast fits cleanly on 180 mm bed.
            return httpx.Response(
                200,
                json=_tool_reply(
                    [
                        _tc(
                            "c2",
                            "configure_standalone_fixture",
                            {
                                "fixture_kind": "imaging_rig",
                                "object_reference_id": "slas_96_well_plate",
                                "camera_working_distance_mm": 135.0,
                            },
                        )
                    ]
                ),
            )
        if calls == 3:
            return httpx.Response(
                200,
                json=_tool_reply(
                    [
                        _tc(
                            "c3",
                            "record_build_log",
                            {
                                "parts_printed": [
                                    "locating-base",
                                    "camera-mast",
                                    "camera-arm",
                                    "tolerance-coupon",
                                ],
                                "fit_result": "too_tight",
                                "measured_features": [
                                    {
                                        "feature": "pocket_length",
                                        "axis": "length",
                                        "nominal_mm": 128.66,
                                        "measured_mm": 128.35,
                                    }
                                ],
                            },
                        )
                    ]
                ),
            )
        if calls == 4:
            return httpx.Response(
                200,
                json=_tool_reply([_tc("c4", "apply_build_feedback", {})]),
            )
        if calls == 5:
            return httpx.Response(
                200,
                json=_tool_reply([_tc("c5", "check_spatial_fit", {})]),
            )
        return httpx.Response(
            200,
            json=_text_reply(
                "Repaired camera working distance to 135 mm to fit A1 mini, logged the "
                "128.35 mm caliper measurement, and revised tolerances for locating-base."
            ),
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_agent_turn(
                project,
                "Design a 96-well imaging rig and adjust after my 128.35 mm print.",
                [],
                client=client,
                settings=TEST_SETTINGS,
            )

    result = asyncio.run(run())
    assert calls == 6
    assert result["repairs_used"] == 1
    assert result["project"]["standalone_fixture"]["derived"]["pocket_length_mm"] == 128.97
    diff = result["project"]["build_feedback"]
    assert {p["id"] for p in diff["parts_to_reprint"]} == {"locating-base", "tolerance-coupon"}
    assert {p["id"] for p in diff["parts_unchanged"]} == {"camera-mast", "camera-arm"}

