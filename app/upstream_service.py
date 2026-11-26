"""High-level orchestration for attaching and adapting upstream open-hardware projects."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx

from app.domain import apply_update
from app.open_hardware_discovery import lookup_discovered_candidate
from app.open_hardware_license import scoped_license_path
from app.spatial_fit import refresh_spatial_fit
from app.upstream_cad_adapt import (
    MatingAdaptationSpec,
    ParamOverride,
    adapt_upstream_mating,
    adapt_upstream_parametric,
)
from app.upstream_cad_ingest import ingest_upstream_cad
from app.upstream_docs import (
    extract_software_references,
    fetch_upstream_doc_files,
    parse_upstream_assembly,
    parse_upstream_bom,
)
from app.workflows import describe_workflow


def _ensure_upstream_allowed(project: dict[str, Any]) -> dict[str, Any]:
    wf_kind = (project.get("workflow") or {}).get("kind")
    design = project.get("design") or {}
    if (
        project.get("cad_instances")
        or project.get("engineering_design")
        or design.get("foundation")
        or design.get("platform")
        or project.get("concept_design")
        or project.get("standalone_fixture")
        or project.get("bench_station")
    ):
        raise ValueError(
            "Start a new project for an open-source hardware import to keep "
            "the current workstation or design intact."
        )
    if wf_kind not in ("general", "liquid_preparation"):
        return apply_update(project, {"workflow_kind": "general"})
    return deepcopy(project)


async def read_upstream_source(
    *,
    source_host: str,
    repo_full_name: str,
    commit_sha: str,
    cad_file_path: str,
    repo_url: str | None = None,
    raw_url: str | None = None,
    spdx_hint: str | None = None,
    license_text: str | None = None,
    allow_non_commercial: bool = False,
    allow_copyleft: bool = True,
    allow_unknown_license: bool = False,
    source_units: str = "auto",
    source_up_axis: str = "z",
    expected_sha256: str | None = None,
    local_bytes: bytes | None = None,
    bom_files: list[str] | None = None,
    assembly_files: list[str] | None = None,
    software_files: list[str] | None = None,
    license_files: list[str] | None = None,
    doc_texts_override: dict[str, str] | None = None,
    authors: str | None = None,
    artifact_dir: Path | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Ingest verified commit-pinned upstream CAD, docs, BOM, and software refs into project."""
    cached = lookup_discovered_candidate(source_host, repo_full_name, commit_sha) or {}

    eff_repo_url = repo_url or cached.get("repo_url") or (
        f"https://gitlab.com/{repo_full_name}"
        if source_host.lower().strip() == "gitlab"
        else f"https://github.com/{repo_full_name}"
    )
    eff_spdx = spdx_hint or cached.get("spdx_id")
    eff_bom = list(bom_files if bom_files is not None else (cached.get("bom_files") or []))
    eff_asm = list(
        assembly_files if assembly_files is not None else (cached.get("assembly_files") or [])
    )
    eff_sw = list(
        software_files if software_files is not None else (cached.get("software_files") or [])
    )
    eff_lic = list(
        license_files if license_files is not None else (cached.get("license_files") or [])
    )
    license_path = scoped_license_path(eff_lic, cad_file_path)

    doc_texts: dict[str, str] = dict(doc_texts_override or {})
    if not doc_texts_override and source_host.lower().strip() in ("github", "gitlab"):
        paths_to_fetch = (
            ([license_path] if license_path and not license_text else [])
            + eff_bom[:3]
            + eff_asm[:3]
            + eff_sw[:5]
        )
        if not eff_asm and not eff_bom and "README.md" not in paths_to_fetch:
            paths_to_fetch.append("README.md")
        fetched = await fetch_upstream_doc_files(
            source_host,
            repo_full_name,
            commit_sha,
            paths_to_fetch,
            client=client,
        )
        doc_texts.update(fetched)

    eff_lic_text = license_text or doc_texts.get(license_path)
    if license_path and "/" in license_path:
        # The selected directory has its own terms. Repository metadata describes the root.
        eff_spdx = None

    ingested = await ingest_upstream_cad(
        artifact_dir=artifact_dir,
        source_host=source_host,
        repo_full_name=repo_full_name,
        repo_url=eff_repo_url,
        commit_sha=commit_sha,
        cad_file_path=cad_file_path,
        spdx_hint=eff_spdx,
        license_text=eff_lic_text,
        raw_url=raw_url,
        source_units=source_units,
        source_up_axis=source_up_axis,
        allow_non_commercial=allow_non_commercial,
        allow_copyleft=allow_copyleft,
        allow_unknown_license=allow_unknown_license,
        expected_sha256=expected_sha256,
        local_bytes=local_bytes,
        authors=authors,
        client=client,
    )

    prov = ingested["provenance"]
    tree_paths = sorted(set([cad_file_path, *eff_bom, *eff_asm, *eff_sw, *doc_texts.keys()]))
    bom_items = parse_upstream_bom(doc_texts, None)
    assembly = parse_upstream_assembly(doc_texts, prov, None)
    software = extract_software_references(tree_paths, doc_texts, prov)

    up_state: dict[str, Any] = {
        "schema_version": "1",
        "title": cached.get("title") or repo_full_name,
        "description": cached.get("description") or "",
        "publication_references": list(cached.get("publication_references") or []),
        "provenance": prov,
        "license_gate": ingested["license_gate"],
        "geometry": ingested["geometry"],
        "artifacts": ingested["artifacts"],
        "tree_index": {
            "cad_files": list(cached.get("cad_files") or [cad_file_path])[:30],
            "bom_files": eff_bom[:15],
            "assembly_files": eff_asm[:15],
            "software_files": eff_sw[:20],
        },
        "bom": bom_items,
        "assembly": assembly,
        "software": software,
        "adaptation": None,
    }

    if artifact_dir is not None and eff_lic_text:
        notice = artifact_dir / "original-license.txt"
        notice.write_text(eff_lic_text, encoding="utf-8")
        up_state["artifacts"]["license_text_path"] = str(notice)
    return up_state


async def attach_upstream_project(
    project: dict[str, Any], *, instance_id: str | None = None, placement=None,
    lab_profile: dict[str, Any] | None = None, **source,
) -> dict[str, Any]:
    """Open a standalone source or place a named file inside the current assembly."""
    if instance_id is not None:
        from app.cad_instances import attach_instance
        return await attach_instance(project, instance_id, placement, source, lab_profile)
    if placement is not None:
        raise ValueError("Supply an instance_id with the measured placement.")
    updated = _ensure_upstream_allowed(project)
    up_state = await read_upstream_source(**source)
    app_size = up_state["geometry"]["app_size_mm"]
    updated["upstream_project"] = up_state
    updated["revision"] = int(updated.get("revision") or 0) + 1
    updated["design"]["footprint"] = {
        "width_mm": round(float(app_size[0]), 2),
        "depth_mm": round(float(app_size[2]), 2),
        "height_mm": round(float(app_size[1]), 2),
    }
    updated["workflow"] = describe_workflow(updated)
    from app.design_workflow import refresh_workflow

    refresh_workflow(updated)
    refresh_spatial_fit(updated, lab_profile=lab_profile)
    return updated


async def apply_upstream_adaptation(
    project: dict[str, Any],
    *,
    mode: str,
    instance_id: str | None = None,
    mating_spec: MatingAdaptationSpec | dict[str, Any] | None = None,
    param_overrides: list[ParamOverride] | list[dict[str, Any]] | dict[str, Any] | None = None,
    lab_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a mating part or parametric revision for project['upstream_project']."""
    if instance_id is not None:
        from app.cad_instances import adapt_instance
        if mode != "mating" or mating_spec is None:
            raise ValueError("A named STEP or STL instance needs a mating specification.")
        return await adapt_instance(project, instance_id, mating_spec, lab_profile)
    up = project.get("upstream_project")
    if not isinstance(up, dict):
        raise ValueError("No upstream open-source project is ingested in this project.")

    updated = deepcopy(project)
    up_copy = updated["upstream_project"]

    if mode == "mating":
        if mating_spec is None:
            raise ValueError("mating_spec is required when mode='mating'.")
        adaptation = await adapt_upstream_mating(
            up_copy, mating_spec, lab_profile=lab_profile
        )
    elif mode == "parametric":
        if param_overrides is None:
            raise ValueError("param_overrides is required when mode='parametric'.")
        if isinstance(param_overrides, list) and param_overrides and isinstance(
            param_overrides[0], dict
        ):
            parsed_overrides = [ParamOverride.model_validate(item) for item in param_overrides]
        else:
            parsed_overrides = param_overrides  # type: ignore[assignment]
        adaptation = await adapt_upstream_parametric(
            up_copy, parsed_overrides, lab_profile=lab_profile
        )
    else:
        raise ValueError(f"Unsupported adaptation mode: {mode!r}")

    up_copy["adaptation"] = adaptation

    # Preserve upstream BOM items and update the single AutoLab adaptation row cleanly.
    upstream_bom = [
        deepcopy(item)
        for item in (up_copy.get("bom") or [])
        if item.get("source") == "upstream_repo"
    ]
    custom_only = parse_upstream_bom({}, adaptation)
    up_copy["bom"] = (upstream_bom + custom_only)[:120]

    # Preserve parsed upstream assembly steps while refreshing AutoLab custom steps.
    existing_asm = up_copy.get("assembly") or {}
    refreshed_asm = parse_upstream_assembly({}, up_copy["provenance"], adaptation)
    up_copy["assembly"] = {
        "upstream_repo": up_copy["provenance"].get("repo_full_name"),
        "commit_sha": up_copy["provenance"].get("commit_sha"),
        "upstream_steps": list(existing_asm.get("upstream_steps") or []),
        "custom_steps": refreshed_asm["custom_steps"],
    }

    comb = adaptation.get("combined_geometry") or up_copy["geometry"]
    app_size = comb["app_size_mm"]
    updated["revision"] = int(updated.get("revision") or 0) + 1
    updated["design"]["footprint"] = {
        "width_mm": round(float(app_size[0]), 2),
        "depth_mm": round(float(app_size[2]), 2),
        "height_mm": round(float(app_size[1]), 2),
    }
    updated["workflow"] = describe_workflow(updated)
    from app.design_workflow import refresh_workflow

    refresh_workflow(updated)
    refresh_spatial_fit(updated, lab_profile=lab_profile)
    return updated
