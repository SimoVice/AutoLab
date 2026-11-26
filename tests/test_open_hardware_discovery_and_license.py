"""Tests for Phase 4.1 (open-hardware discovery) and Phase 4.2 (license & security gate)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.open_hardware_discovery import _classify_repo_paths, discover_open_hardware
from app.open_hardware_license import (
    build_provenance_record,
    detect_spdx_license,
    evaluate_license_gate,
    render_third_party_notices,
    scoped_license_path,
    sha256_bytes,
    validate_repo_file_path,
    validate_upstream_url,
)


def test_spdx_detection_and_license_gate_policies():
    assert detect_spdx_license("MIT", None) == "MIT"
    assert (
        detect_spdx_license(
            "NOASSERTION",
            "CERN Open Hardware Licence Version 2 - Strongly Reciprocal (CERN-OHL-S-2.0)",
        )
        == "CERN-OHL-S-2.0"
    )
    assert (
        detect_spdx_license(
            None, "Licensed under Creative Commons Attribution-NonCommercial 4.0 International"
        )
        == "CC-BY-NC-4.0"
    )

    permissive = evaluate_license_gate("CC-BY-4.0")
    assert permissive["allowed"] is True
    assert permissive["category"] == "permissive"
    assert permissive["commercial_ok"] is True

    copyleft = evaluate_license_gate("CERN-OHL-S-2.0")
    assert copyleft["allowed"] is True
    assert copyleft["copyleft"] is True
    assert "copyleft_share_alike" in copyleft["flags"]
    assert evaluate_license_gate("CERN-OHL-S-2.0", allow_copyleft=False)["allowed"] is False

    nc = evaluate_license_gate("CC-BY-NC-4.0")
    assert nc["allowed"] is False
    assert nc["commercial_ok"] is False
    assert "non_commercial_restricted" in nc["flags"]
    assert evaluate_license_gate("CC-BY-NC-4.0", allow_non_commercial=True)["allowed"] is True

    nd = evaluate_license_gate("CC-BY-ND-4.0")
    assert nd["allowed"] is False
    assert nd["derivatives_ok"] is False

    unknown = evaluate_license_gate(None, None)
    assert unknown["allowed"] is False
    assert evaluate_license_gate(None, None, allow_unknown=True)["allowed"] is True


def test_ssrf_url_path_traversal_and_executable_rejection():
    assert validate_upstream_url("https://raw.githubusercontent.com/org/repo/main/a.step")
    assert validate_upstream_url("https://api.github.com/search/repositories", for_download=False)

    for bad_url in (
        "http://github.com/org/repo/a.step",
        "https://127.0.0.1/secret",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/api",
        "https://user:pass@github.com/org/repo",
        "https://evil.example.com/payload.step",
        "https://github.com:8443/org/repo",
    ):
        with pytest.raises(ValueError):
            validate_upstream_url(bad_url, for_download=True)

    assert validate_repo_file_path("./cad/pump-body.STEP") == "cad/pump-body.STEP"
    assert validate_repo_file_path("LICENSE") == "LICENSE"

    for bad_path in (
        "../secret.step",
        "cad/../../etc/passwd",
        "/etc/passwd",
        "bin\\install.bat",
        "firmware/flash.sh",
        "installer/setup.exe",
        "native/libfoo.dll",
        "scripts/hack.ps1",
        "assets/blob.bin",
    ):
        with pytest.raises(ValueError):
            validate_repo_file_path(bad_path, allow_cad=True, allow_docs=True)


def test_render_third_party_notices_preserves_provenance_and_boundary():
    gate = evaluate_license_gate("CERN-OHL-S-2.0")
    raw = b"ISO-10303-21;mock-step;"
    prov = build_provenance_record(
        source_host="github",
        repo_full_name="openlab/syringe-pump",
        repo_url="https://github.com/openlab/syringe-pump",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        file_path="cad/base.step",
        raw_url="https://raw.githubusercontent.com/openlab/syringe-pump/0123456789abcdef0123456789abcdef01234567/cad/base.step",
        file_sha256=sha256_bytes(raw),
        file_bytes=len(raw),
        license_gate=gate,
    )
    notices = render_third_party_notices(
        {
            "provenance": prov,
            "license_gate": gate,
            "adaptation": {"mode": "mating"},
        }
    )
    assert "openlab/syringe-pump" in notices
    assert "0123456789abcdef0123456789abcdef01234567" in notices
    assert prov["file_sha256"] in notices
    assert "CERN-OHL-S-2.0" in notices
    assert "upstream/source/" in notices
    assert "`mating`" in notices


def test_discovery_across_sources_ranks_pins_commit_and_filters_licenses():
    sha_best = "a" * 40
    sha_nc = "b" * 40
    sha_gl = "c" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://api.github.com/search/repositories"):
            assert request.url.params["q"] == "syringe pump in:name,description,topics,readme"
            assert "sort" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "full_name": "openlab/precision-syringe-pump",
                            "description": "Open hardware programmable syringe pump with STEP CAD",
                            "html_url": "https://github.com/openlab/precision-syringe-pump",
                            "default_branch": "main",
                            "stargazers_count": 180,
                            "topics": ["open-hardware", "syringe-pump"],
                            "license": {"spdx_id": "NOASSERTION"},
                        },
                        {
                            "full_name": "academic/nc-syringe-pump-stl",
                            "description": "Syringe pump STL only",
                            "html_url": "https://github.com/academic/nc-syringe-pump-stl",
                            "default_branch": "main",
                            "stargazers_count": 40,
                            "topics": ["syringe-pump"],
                            "license": {"spdx_id": "CC-BY-NC-4.0"},
                        },
                    ]
                },
            )
        if "/repos/openlab/precision-syringe-pump/commits/main" in url:
            return httpx.Response(200, json={"sha": sha_best})
        if "/repos/academic/nc-syringe-pump-stl/commits/main" in url:
            return httpx.Response(200, json={"sha": sha_nc})
        if f"/repos/openlab/precision-syringe-pump/git/trees/{sha_best}" in url:
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"type": "blob", "path": "LICENSE"},
                        {"type": "blob", "path": "cad/pump-frame.step"},
                        {"type": "blob", "path": "bom/parts.csv"},
                        {"type": "blob", "path": "docs/ASSEMBLY.md"},
                        {"type": "blob", "path": "firmware/controller.py"},
                        {"type": "blob", "path": "tools/unsafe.exe"},
                    ]
                },
            )
        if f"/repos/academic/nc-syringe-pump-stl/git/trees/{sha_nc}" in url:
            return httpx.Response(
                200,
                json={"tree": [{"type": "blob", "path": "stl/body.stl"}]},
            )
        if f"raw.githubusercontent.com/openlab/precision-syringe-pump/{sha_best}/LICENSE" in url:
            return httpx.Response(
                200,
                text="CERN Open Hardware Licence Version 2 - Permissive (CERN-OHL-P-2.0)",
            )
        if url.startswith("https://gitlab.com/api/v4/projects?"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 99,
                        "path_with_namespace": "labgroup/open-pump",
                        "name_with_namespace": "LabGroup / Open Pump",
                        "description": "Syringe pump OpenSCAD",
                        "web_url": "https://gitlab.com/labgroup/open-pump",
                        "default_branch": "main",
                        "star_count": 12,
                        "topics": ["syringe-pump"],
                        "license": {"key": "mit"},
                    }
                ],
            )
        if "/api/v4/projects/99/repository/commits/main" in url:
            return httpx.Response(200, json={"id": sha_gl})
        if "/api/v4/projects/99/repository/tree" in url:
            return httpx.Response(
                200,
                json=[{"type": "blob", "path": "scad/pump.scad"}],
            )
        if url.startswith("https://api.openalex.org/works"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "https://openalex.org/W123",
                            "display_name": "Open-source precision syringe pump (https://github.com/openlab/precision-syringe-pump)",
                            "publication_year": 2025,
                            "doi": "https://doi.org/10.1016/j.ohx.2025.0001",
                            "primary_location": {"license": "cc-by"},
                        }
                    ]
                },
            )
        if url.startswith("https://www.ebi.ac.uk/europepmc/"):
            return httpx.Response(
                200,
                json={
                    "resultList": {
                        "result": [
                            {
                                "id": "PMC12345",
                                "pmcid": "PMC12345",
                                "title": "HardwareX pump study",
                                "pubYear": "2024",
                                "doi": "10.1016/j.ohx.2024.0002",
                                "abstractText": "Source: https://github.com/openlab/precision-syringe-pump",
                            }
                        ]
                    }
                },
            )
        if url.startswith("https://3d.nih.gov/"):
            return httpx.Response(503, text="Service unavailable")
        raise AssertionError(f"Unexpected URL queried: {url}")

    transport = httpx.MockTransport(handler)

    async def _run():
        async with httpx.AsyncClient(transport=transport) as client:
            full = await discover_open_hardware("syringe pump", max_results=6, client=client)
            comm = await discover_open_hardware(
                "syringe pump", max_results=6, commercial_use_ok=True, client=client
            )
            return full, comm

    full, comm = asyncio.run(_run())
    assert full["candidate_count"] == 3
    top = full["candidates"][0]
    assert top["repo_full_name"] == "openlab/precision-syringe-pump"
    assert top["commit_sha"] == sha_best
    assert top["spdx_id"] == "CERN-OHL-P-2.0"
    assert top["cad_files"] == ["cad/pump-frame.step"]
    assert top["bom_files"] == ["bom/parts.csv"]
    assert top["assembly_files"] == ["docs/ASSEMBLY.md"]
    assert top["software_files"] == ["firmware/controller.py"]
    assert len(top["publication_references"]) == 2
    assert any("NIH 3D" in w for w in full["source_warnings"])

    comm_names = [c["repo_full_name"] for c in comm["candidates"]]
    assert "academic/nc-syringe-pump-stl" not in comm_names
    assert "openlab/precision-syringe-pump" in comm_names
    assert json.dumps(full)  # JSON-serializable cleanly


def test_search_relevance_is_applied_before_the_cad_file_limit():
    unrelated = [f"assembly/panel-{n}.step" for n in range(75)]
    part = "attachments/indexingWheel.stl"
    files = _classify_repo_paths([*unrelated, part], "indexing wheel")["cad_files"]
    assert len(files) == 40
    assert files[0] == part


def test_license_scope_follows_the_asset_path():
    paths = ["assets/fonts/LICENSE.txt", "NOTICE", "mechanics/LICENCE.md", "LICENSE"]
    assert scoped_license_path(paths) == "LICENSE"
    assert scoped_license_path(paths, "mechanics/parts/ring.step") == "mechanics/LICENCE.md"
    assert scoped_license_path(paths, "electrical/board.stl") == "LICENSE"
    assert scoped_license_path(paths[:2], "parts/base.step") is None
    assert scoped_license_path(["COPYING-MIT", "COPYING-GPL"]) is None
