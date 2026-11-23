"""Tests for Task 3.4 incremental CAD caching, per-part export, and RSS/time worker bounds."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from test_concept_mechanical_features import _mechanical_proposal

import app.api as api
import app.cad_worker as worker
from app.cad_cache import cache_stats, clear_cache
from app.cad_exports import build_cad_files, export_single_part
from app.cad_worker import _process_rss_bytes, execute_cad, execute_cad_part
from app.concept_cad import concept_step_with_metrics
from app.concept_design import compile_concept
from app.domain import new_project

FIXTURE = Path(__file__).with_name("cad_worker_fixture.py")


def test_incremental_cad_cache_accelerates_part_and_concept_exports(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOLAB_CAD_CACHE_DIR", str(tmp_path / "cad-cache"))
    clear_cache()

    design = compile_concept(_mechanical_proposal())
    cold_step, cold_meta = concept_step_with_metrics(design)
    warm_step, warm_meta = concept_step_with_metrics(design)
    assert cold_step == warm_step and len(cold_step) > 500
    assert cold_meta["cache_hit"] is False
    assert cold_meta["elapsed_ms"] < 30_000.0
    assert warm_meta["cache_hit"] is True
    assert warm_meta["elapsed_ms"] < 150.0

    # Workstation build_cad_files reports performance metrics and hits warm cache on 2nd run.
    proj = new_project()
    first_files = build_cad_files(proj)
    first_report = json.loads(first_files["cad/geometry-report.json"])
    assert first_report["performance"]["total_ms"] < 30_000.0
    assert first_report["performance"]["cache_misses"] >= 3

    second_files = build_cad_files(proj)
    second_report = json.loads(second_files["cad/geometry-report.json"])
    assert second_report["performance"]["cache_hits"] >= 3
    assert second_report["performance"]["cache_misses"] == 0
    assert second_files["cad/workstation.step"] == first_files["cad/workstation.step"]

    # Incremental single-part export works cleanly with cold miss -> warm hit.
    clear_cache("single_part")
    part_cold, meta_cold = export_single_part(proj, "base", fmt="step")
    part_warm, meta_warm = export_single_part(proj, "base", fmt="step")
    assert part_cold == part_warm and len(part_cold) > 500
    assert meta_cold["cache_hit"] is False and meta_warm["cache_hit"] is True
    assert meta_warm["elapsed_ms"] < 100.0
    stats = cache_stats()
    assert stats["disk_entries"] >= 4 and stats["hits"] >= 4


def test_worker_rss_measurement_and_memory_limit_enforcement(monkeypatch):
    import os

    rss = _process_rss_bytes(os.getpid())
    assert rss is not None and rss > 5 * 1024 * 1024

    processes = []
    original_popen = worker.subprocess.Popen
    monkeypatch.setattr(
        worker,
        "_worker_command",
        lambda: (sys.executable, "-I", str(FIXTURE), "--mode", "memory-hog"),
    )

    def spawn(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        processes.append((proc, args[0]))
        return proc

    monkeypatch.setattr(worker.subprocess, "Popen", spawn)
    monkeypatch.setattr(worker, "MAX_WORKER_RSS_BYTES", 32 * 1024 * 1024)

    t0 = time.monotonic()
    with pytest.raises(worker.CadWorkerError, match="memory limit"):
        asyncio.run(execute_cad({"id": "memory-hog-test"}))
    assert time.monotonic() - t0 < 10.0
    assert not worker._children
    for proc, cmd in processes:
        assert proc.poll() is not None
        assert not Path(cmd[-3]).parent.exists()


def test_single_part_worker_and_http_cad_route(tmp_path, monkeypatch):
    build123d = pytest.importorskip("build123d")
    import_step = build123d.import_step
    monkeypatch.setenv("AUTOLAB_CAD_CACHE_DIR", str(tmp_path / "route-cache"))
    clear_cache()

    proj = new_project()
    api._sessions[proj["id"]] = api.ProjectSession(project=deepcopy(proj))

    async def run():
        part_bytes, meta = await execute_cad_part(proj, "base", fmt="step")
        assert len(part_bytes) > 500 and meta["component_id"] == "base"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app), base_url="http://test"
        ) as client:
            r_step = await client.get(f"/api/projects/{proj['id']}/parts/base/cad?format=step")
            assert r_step.status_code == 200
            step_path = tmp_path / "base.step"
            step_path.write_bytes(r_step.content)
            shape = import_step(str(step_path))
            assert shape.is_valid and len(shape.solids()) == 1

            r_stl = await client.get(f"/api/projects/{proj['id']}/parts/base/cad?format=stl")
            assert r_stl.status_code == 200 and len(r_stl.content) > 84

            r_bad = await client.get(f"/api/projects/{proj['id']}/parts/base/cad?format=obj")
            assert r_bad.status_code == 422

    try:
        asyncio.run(run())
    finally:
        api._sessions.pop(proj["id"], None)
