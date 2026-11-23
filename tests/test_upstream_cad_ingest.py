"""Tests for Phase 4.3 sandboxed CAD ingestion, inspection, and normalization."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from build123d import Box, Cylinder, Pos, Unit, export_step, export_stl, import_step

from app.open_hardware_license import sha256_bytes
from app.upstream_cad_ingest import (
    UpstreamCadError,
    download_verified_asset,
    ingest_upstream_cad,
    run_upstream_worker,
)


def _sample_step_bytes(tmp_path: Path) -> bytes:
    plate = (
        Box(80, 50, 12)
        - Pos(30, 18, 0) * Cylinder(1.7, 20)
        - Pos(-30, -18, 0) * Cylinder(1.7, 20)
    )
    out = tmp_path / "upstream_flange.step"
    export_step(plate, out, unit=Unit.MM)
    return out.read_bytes()


def _sample_stl_bytes(tmp_path: Path) -> bytes:
    block = Box(40, 30, 20)
    out = tmp_path / "upstream_block.stl"
    export_stl(block, out)
    return out.read_bytes()


def test_step_ingest_via_mock_http_normalizes_extracts_holes_and_writes_glb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    step_bytes = _sample_step_bytes(tmp_path)
    sha = sha256_bytes(step_bytes)
    commit = "d" * 40
    raw_url = f"https://raw.githubusercontent.com/openlab/stage/{commit}/cad/flange.step"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == raw_url:
            return httpx.Response(200, content=step_bytes)
        raise AssertionError(f"Unexpected download URL: {request.url}")

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ingest_upstream_cad(
                source_host="github",
                repo_full_name="openlab/stage",
                repo_url="https://github.com/openlab/stage",
                commit_sha=commit,
                cad_file_path="cad/flange.step",
                spdx_hint="MIT",
                expected_sha256=sha,
                client=client,
            )

    res = asyncio.run(_run())
    geom = res["geometry"]
    assert geom["cad_kind"] == "step_brep"
    assert geom["is_valid"] is True
    assert geom["solid_count"] == 1
    assert geom["cad_size_mm"] == pytest.approx([80.0, 50.0, 12.0], abs=0.1)
    assert geom["cad_min_mm"] == pytest.approx([-40.0, -25.0, 0.0], abs=0.1)
    assert len(geom["mounting_planes"]) >= 2
    assert len(geom["hole_features"]) == 2
    assert all(h["diameter_mm"] == pytest.approx(3.4, abs=0.05) for h in geom["hole_features"])

    norm_step = Path(res["artifacts"]["normalized_step_path"])
    preview_glb = Path(res["artifacts"]["preview_glb_path"])
    assert norm_step.is_file()
    assert import_step(norm_step).is_valid
    assert preview_glb.read_bytes()[:4] == b"glTF"
    assert res["provenance"]["file_sha256"] == sha


def test_inch_scaling_yup_rotation_and_stl_watertight_ingestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    # Build 2 x 1 x 3 raw model and ingest declaring inches + Y-up.
    raw_block = tmp_path / "tiny_inch.step"
    export_step(Box(2.0, 3.0, 1.0), raw_block, unit=Unit.MM)
    step_bytes = raw_block.read_bytes()
    stl_bytes = _sample_stl_bytes(tmp_path)

    commit = "e" * 40
    res_in = asyncio.run(
        ingest_upstream_cad(
            source_host="github",
            repo_full_name="openlab/inch-part",
            repo_url="https://github.com/openlab/inch-part",
            commit_sha=commit,
            cad_file_path="cad/part.step",
            spdx_hint="Apache-2.0",
            source_units="in",
            source_up_axis="y",
            local_bytes=step_bytes,
        )
    )
    # 2x3x1 in -> scale(25.4) -> 50.8 x 76.2 x 25.4 -> rotate_x(+90deg) -> 50.8 x 25.4 x 76.2
    assert res_in["geometry"]["cad_size_mm"] == pytest.approx([50.8, 25.4, 76.2], abs=0.2)
    assert res_in["geometry"]["cad_min_mm"][2] == pytest.approx(0.0, abs=0.05)

    res_stl = asyncio.run(
        ingest_upstream_cad(
            source_host="github",
            repo_full_name="openlab/mesh-part",
            repo_url="https://github.com/openlab/mesh-part",
            commit_sha=commit,
            cad_file_path="stl/block.stl",
            spdx_hint="CC-BY-4.0",
            local_bytes=stl_bytes,
        )
    )
    assert res_stl["geometry"]["watertight"] is True
    assert res_stl["geometry"]["cad_size_mm"] == pytest.approx([40.0, 30.0, 20.0], abs=0.1)
    assert res_stl["geometry"]["volume_mm3"] == pytest.approx(24000.0, rel=0.02)
    assert Path(res_stl["artifacts"]["preview_glb_path"]).read_bytes()[:4] == b"glTF"


def test_ingest_security_corrupt_files_and_worker_rss_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    commit = "f" * 40

    # SSRF redirect blocking
    def evil_redirect(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://127.0.0.1/internal.step"})

    async def _dl():
        async with httpx.AsyncClient(transport=httpx.MockTransport(evil_redirect)) as client:
            await download_verified_asset(
                f"https://raw.githubusercontent.com/org/repo/{commit}/a.step",
                client=client,
            )

    with pytest.raises(ValueError, match="IP address"):
        asyncio.run(_dl())

    # NC license blocked unless explicitly permitted
    with pytest.raises(PermissionError, match="restricts commercial use"):
        asyncio.run(
            ingest_upstream_cad(
                source_host="github",
                repo_full_name="org/nc-part",
                repo_url="https://github.com/org/nc-part",
                commit_sha=commit,
                cad_file_path="a.stl",
                spdx_hint="CC-BY-NC-4.0",
                local_bytes=_sample_stl_bytes(tmp_path),
            )
        )

    # Corrupt STEP rejected cleanly
    with pytest.raises(UpstreamCadError):
        asyncio.run(
            ingest_upstream_cad(
                source_host="github",
                repo_full_name="org/bad",
                repo_url="https://github.com/org/bad",
                commit_sha=commit,
                cad_file_path="bad.step",
                spdx_hint="MIT",
                local_bytes=b"NOT A STEP FILE",
            )
        )

    # RSS ceiling kills child importing build123d when capped artificially low (1 MB)
    bad_path = tmp_path / "valid.step"
    bad_path.write_bytes(_sample_step_bytes(tmp_path))
    with pytest.raises(UpstreamCadError, match="RSS ceiling|timed out|failed"):
        asyncio.run(
            run_upstream_worker(
                "inspect",
                {"source_path": str(bad_path), "artifact_dir": str(tmp_path / "art")},
                max_rss_bytes=1024 * 1024,
            )
        )
