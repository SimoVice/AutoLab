"""Tests for Phase 4.5 upstream BOM, assembly, software ingestion & ZIP package builder."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import zipfile
from pathlib import Path

import pytest
from build123d import Box, Unit, export_step

from app.domain import apply_update, new_project
from app.open_hardware_license import sha256_bytes
from app.upstream_cad_adapt import MatingAdaptationSpec, adapt_upstream_mating
from app.upstream_cad_ingest import ingest_upstream_cad
from app.upstream_docs import (
    build_upstream_package_zip,
    extract_software_references,
    parse_upstream_assembly,
    parse_upstream_bom,
)


def test_parse_csv_markdown_and_yaml_boms_with_source_vs_custom_separation():
    files = {
        "bom/hardware_bom.csv": (
            "Part Name,Qty,SKU,Supplier,Notes\n"
            "NEMA 17 Stepper,2,17HS4401,Pololu,40mm body\n"
            "=SUM(A1:A2),1,X-1,Vendor,=cmd\n"
        ),
        "docs/BOM.md": (
            "| Component | Quantity | Supplier | Link |\n"
            "| --- | --- | --- | --- |\n"
            "| MGN12H Linear Rail 250mm | 1 | Misumi | https://example.com |\n"
        ),
        "bom/parts.yaml": (
            "- name: 608ZZ Bearing\n"
            "  qty: 4\n"
            "  notes: Standard radial bearing\n"
        ),
    }
    adaptation = {
        "mode": "mating",
        "part_id": "custom-adapter-plate",
        "spec": {"kind": "adapter_plate"},
        "custom_geometry": {"app_size_mm": [90.0, 6.0, 70.0]},
        "interference": {"passed": True},
    }
    items = parse_upstream_bom(files, adaptation)
    upstream_items = [i for i in items if i["source"] == "upstream_repo"]
    custom_items = [i for i in items if i["source"] == "autolab_custom_adaptation"]

    assert len(upstream_items) == 4
    assert all(i["compatibility_status"] == "unverified_upstream" for i in upstream_items)
    assert len(custom_items) == 1
    assert custom_items[0]["id"] == "custom-adapter-plate"
    assert custom_items[0]["compatibility_status"] == "modeled_zero_interference_untested_hardware"


def test_parse_assembly_software_and_build_attributed_package_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AUTOLAB_UPSTREAM_CACHE_DIR", str(tmp_path / "cache"))
    step_path = tmp_path / "upstream_stage.step"
    export_step(Box(60, 40, 20), step_path, unit=Unit.MM)
    raw_step = step_path.read_bytes()

    commit = "1" * 40
    up = asyncio.run(
        ingest_upstream_cad(
            source_host="github",
            repo_full_name="openlab/z-stage",
            repo_url="https://github.com/openlab/z-stage",
            commit_sha=commit,
            cad_file_path="cad/z-stage.step",
            spdx_hint="CERN-OHL-S-2.0",
            local_bytes=raw_step,
        )
    )
    adapted = asyncio.run(
        adapt_upstream_mating(
            up, MatingAdaptationSpec(kind="adapter_plate", clearance_mm=0.4, placement="below")
        )
    )
    up["adaptation"] = adapted

    doc_files = {
        "bom/parts.csv": "Part,Qty,Notes\nT8 Leadscrew,1,=1+1\n",
        "docs/ASSEMBLY.md": (
            "# Assembly\n"
            "1. Mount the NEMA17 motor to the bottom flange.\n"
            "2. Insert the T8 leadscrew into the nut block.\n"
        ),
        "firmware/printer.cfg": "[stepper_x]\nstep_pin: PB13\n",
    }
    meta = up["provenance"]
    up["bom"] = parse_upstream_bom(doc_files, adapted)
    up["assembly"] = parse_upstream_assembly(doc_files, meta, adapted)
    up["software"] = extract_software_references(
        ["cad/z-stage.step", "bom/parts.csv", "docs/ASSEMBLY.md", "firmware/printer.cfg"],
        doc_files,
        meta,
    )

    assert len(up["assembly"]["upstream_steps"]) == 2
    assert len(up["assembly"]["custom_steps"]) == 3
    assert "Klipper" in up["software"]["detected_stacks"]
    assert up["software"]["execution_status"] == "reference_only_unverified"

    proj = apply_update(new_project(), {"workflow_kind": "general"})
    proj["upstream_project"] = up

    archive = build_upstream_package_zip(proj)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = set(zf.namelist())
        assert "upstream/source/z-stage.step" in names
        assert "upstream/normalized.step" in names
        assert "upstream/preview.glb" in names
        assert "custom/custom-adapter-plate.step" in names
        assert "custom/custom-adapter-plate.stl" in names
        assert "custom/custom-adapter-plate.glb" in names
        assert "assembly/adapted-assembly.step" in names
        assert "bom/bom.csv" in names
        assert "bom/bom.json" in names
        assert "docs/build-guide.md" in names
        assert "docs/software-references.json" in names
        assert "THIRD_PARTY_NOTICES.md" in names
        assert "manifest.json" in names

        assert sha256_bytes(zf.read("upstream/source/z-stage.step")) == meta["file_sha256"]
        notices = zf.read("THIRD_PARTY_NOTICES.md").decode("utf-8")
        assert "CERN-OHL-S-2.0" in notices and commit in notices

        # CSV formula escaping on "=1+1"
        rows = list(csv.reader(io.StringIO(zf.read("bom/bom.csv").decode("utf-8"))))
        assert any("'=1+1" in cell for r in rows for cell in r)

        manifest = json.loads(zf.read("manifest.json"))
        for entry in manifest["files"]:
            assert sha256_bytes(zf.read(entry["name"])) == entry["sha256"]
