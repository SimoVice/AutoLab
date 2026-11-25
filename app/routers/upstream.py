"""FastAPI routes for online open-hardware discovery, sandboxed CAD ingest/adapt & downloads."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.cad_instances import CadPlacement, Digest, Name, instance_file, selected_source
from app.lab_profile import has_lab_profile_data, load_lab_profile
from app.open_hardware_discovery import DEFAULT_SOURCES, discover_open_hardware
from app.open_hardware_license import render_third_party_notices
from app.services.runtime import api_hook
from app.services.session_store import accept_project, check_revision, session_for
from app.upstream_cad_adapt import MatingAdaptationSpec, ParamOverride
from app.upstream_cad_ingest import UpstreamCadError
from app.upstream_docs import (
    build_upstream_bom_csv,
    build_upstream_build_guide_md,
    build_upstream_package_zip,
)
from app.upstream_service import apply_upstream_adaptation, attach_upstream_project

router = APIRouter()


class DiscoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, Field(min_length=2, max_length=300)]
    sources: list[Literal["github", "gitlab", "openalex", "europepmc", "nih3d"]] | None = None
    max_results: Annotated[int, Field(ge=1, le=20)] = 8
    require_cad: bool = True
    commercial_use_ok: bool = False


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_revision: Annotated[int, Field(strict=True, ge=0)]
    source_host: Literal["github", "gitlab", "nih3d"]
    repo_full_name: Annotated[str, Field(min_length=3, max_length=200)]
    commit_sha: Annotated[str, Field(min_length=2, max_length=64)]
    cad_file_path: Annotated[str, Field(min_length=3, max_length=400)]
    instance_id: Name | None = None
    placement: CadPlacement | None = None
    expected_sha256: Digest | None = None
    repo_url: Annotated[str | None, Field(max_length=500)] = None
    raw_url: Annotated[str | None, Field(max_length=500)] = None
    spdx_hint: Annotated[str | None, Field(max_length=64)] = None
    allow_non_commercial: bool = False
    allow_copyleft: bool = True
    allow_unknown_license: bool = False
    source_units: Literal["auto", "mm", "in"] = "auto"
    source_up_axis: Literal["z", "y"] = "z"


class AdaptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_revision: Annotated[int, Field(strict=True, ge=0)]
    mode: Literal["mating", "parametric"]
    instance_id: Name | None = None
    mating_spec: MatingAdaptationSpec | None = None
    param_overrides: list[ParamOverride] | None = None


@router.post("/api/upstream/discover")
async def discover_upstream_projects_route(request: DiscoverRequest) -> dict[str, Any]:
    try:
        return await discover_open_hardware(
            request.query,
            sources=request.sources or list(DEFAULT_SOURCES),
            max_results=request.max_results,
            require_cad=request.require_cad,
            commercial_use_ok=request.commercial_use_ok,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/api/projects/{project_id}/upstream/ingest")
async def ingest_upstream_project_route(project_id: str, request: IngestRequest) -> dict[str, Any]:
    session = api_hook("session_for", session_for)(project_id)
    accept_fn = api_hook("_accept_project", accept_project)
    profile = load_lab_profile()
    lab_profile = profile if has_lab_profile_data(profile) else None

    async with session.model_lock():
        check_revision(session.project, request.project_revision)
        try:
            updated = await attach_upstream_project(
                session.project,
                instance_id=request.instance_id, placement=request.placement,
                expected_sha256=request.expected_sha256, source_host=request.source_host,
                repo_full_name=request.repo_full_name,
                repo_url=request.repo_url,
                raw_url=request.raw_url,
                commit_sha=request.commit_sha,
                cad_file_path=request.cad_file_path,
                spdx_hint=request.spdx_hint,
                allow_non_commercial=request.allow_non_commercial,
                allow_copyleft=request.allow_copyleft,
                allow_unknown_license=request.allow_unknown_license,
                source_units=request.source_units,
                source_up_axis=request.source_up_axis,
                lab_profile=lab_profile,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        except (ValidationError, ValueError, UpstreamCadError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if updated is not session.project:
            accept_fn(session, updated)
        source = selected_source(updated, request.instance_id)
        prov = source["provenance"]
        return {
            "project": deepcopy(updated),
            "assistant_message": (
                f"Ingested {prov['repo_full_name']} ({prov['file_path']}, "
                f"{prov['spdx_id']}) at revision {updated['revision']}."
            ),
        }


@router.post("/api/projects/{project_id}/upstream/adapt")
async def adapt_upstream_project_route(project_id: str, request: AdaptRequest) -> dict[str, Any]:
    session = api_hook("session_for", session_for)(project_id)
    accept_fn = api_hook("_accept_project", accept_project)
    profile = load_lab_profile()
    lab_profile = profile if has_lab_profile_data(profile) else None

    async with session.model_lock():
        check_revision(session.project, request.project_revision)
        try:
            updated = await apply_upstream_adaptation(
                session.project,
                mode=request.mode, instance_id=request.instance_id,
                mating_spec=request.mating_spec,
                param_overrides=request.param_overrides,
                lab_profile=lab_profile,
            )
        except (ValidationError, ValueError, UpstreamCadError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if updated is not session.project:
            accept_fn(session, updated)
        source = selected_source(updated, request.instance_id)
        adapt = source["adaptation"]
        return {
            "project": deepcopy(updated),
            "assistant_message": (
                f"Generated {adapt['mode']} adaptation '{adapt['part_id']}' "
                f"(interference_passed={adapt['interference']['passed']}) at revision "
                f"{updated['revision']}."
            ),
        }


@router.get("/api/projects/{project_id}/upstream/instances/{name}/{role}")
async def get_instance_asset(project_id: str, name: str, role: str, revision: int) -> Response:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        instance = (session.project.get("cad_instances") or {}).get(name)
        path = instance_file(instance, role) if instance else None
        if path is None:
            raise HTTPException(status_code=404, detail="CAD artifact unavailable in this cache.")
        mime = {".glb": "model/gltf-binary", ".stl": "model/stl", ".step": "model/step",
                ".txt": "text/plain"}
        return Response(content=path.read_bytes(), media_type=mime[path.suffix],
                        headers={"Cache-Control": "no-store", "Content-Disposition":
                                 f'inline; filename="{name}-{role}{path.suffix}"'})


@router.get("/api/projects/{project_id}/upstream/preview/{kind}.glb")
async def preview_upstream_glb(project_id: str, kind: str, revision: int) -> Response:
    if kind not in ("upstream", "custom", "assembly"):
        raise HTTPException(status_code=422, detail="Choose 'upstream', 'custom', or 'assembly'.")
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        up = deepcopy(session.project.get("upstream_project"))
    if not isinstance(up, dict):
        raise HTTPException(status_code=404, detail="No upstream project is active.")
    adapt = up.get("adaptation") or {}
    path_str = None
    if kind == "upstream":
        path_str = (up.get("artifacts") or {}).get("preview_glb_path")
    elif kind == "custom":
        path_str = (adapt.get("artifacts") or {}).get("custom_glb_path")
    else:
        path_str = (adapt.get("artifacts") or {}).get("assembly_glb_path")
    if not path_str or not Path(path_str).is_file():
        raise HTTPException(status_code=404, detail=f"Preview GLB '{kind}' is unavailable.")
    return Response(
        content=Path(path_str).read_bytes(),
        media_type="model/gltf-binary",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/projects/{project_id}/upstream/download/{asset:path}")
async def download_upstream_asset(project_id: str, asset: str, revision: int) -> Response:
    session = api_hook("session_for", session_for)(project_id)
    async with session.model_lock():
        check_revision(session.project, revision)
        current = deepcopy(session.project)
    up = current.get("upstream_project")
    if not isinstance(up, dict):
        raise HTTPException(status_code=404, detail="No upstream project is active.")

    norm = asset.lstrip("/")
    if norm == "package.zip":
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(None, build_upstream_package_zip, current)
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="autolab-upstream-r{revision}.zip"',
                "Cache-Control": "no-store",
            },
        )
    if norm == "THIRD_PARTY_NOTICES.md":
        return Response(
            content=render_third_party_notices(up).encode("utf-8"),
            media_type="text/markdown",
            headers={
                "Content-Disposition": 'attachment; filename="THIRD_PARTY_NOTICES.md"',
                "Cache-Control": "no-store",
            },
        )
    if norm == "bom.csv":
        return Response(
            content=build_upstream_bom_csv(up.get("bom") or []),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="upstream-bom-r{revision}.csv"',
                "Cache-Control": "no-store",
            },
        )
    if norm == "build-guide.md":
        return Response(
            content=build_upstream_build_guide_md(up).encode("utf-8"),
            media_type="text/markdown",
            headers={
                "Content-Disposition": f'attachment; filename="build-guide-r{revision}.md"',
                "Cache-Control": "no-store",
            },
        )

    adapt = up.get("adaptation") or {}
    adapt_art = adapt.get("artifacts") or {}
    part_id = adapt.get("part_id") or "custom-part"
    if norm in (f"custom/{part_id}.step", "custom.step") and adapt_art.get("custom_step_path"):
        p = Path(adapt_art["custom_step_path"])
        if p.is_file():
            return Response(content=p.read_bytes(), media_type="model/step")
    if norm in (f"custom/{part_id}.stl", "custom.stl") and adapt_art.get("custom_stl_path"):
        p = Path(adapt_art["custom_stl_path"])
        if p.is_file():
            return Response(content=p.read_bytes(), media_type="model/stl")
    if norm == "assembly.step" and adapt_art.get("assembly_step_path"):
        p = Path(adapt_art["assembly_step_path"])
        if p.is_file():
            return Response(content=p.read_bytes(), media_type="model/step")

    raise HTTPException(status_code=404, detail="Requested upstream project asset was not found.")
