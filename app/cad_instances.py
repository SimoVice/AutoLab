"""Named, pinned CAD sources attached to a project's existing physical assembly."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from app.upstream_cad_adapt import MatingAdaptationSpec, adapt_upstream_mating
from app.upstream_cad_ingest import _atomic_write, resolve_raw_file_url, upstream_cache_dir

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Coordinate = Annotated[float, Field(ge=-100_000, le=100_000, allow_inf_nan=False)]
Vector = Annotated[list[Coordinate], Field(min_length=3, max_length=3)]


class AccessSpace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    face: Literal["+x", "-x", "+y", "-y", "+z", "-z"]
    distance_mm: Annotated[float, Field(gt=0, le=10_000, allow_inf_nan=False)]


class CadPlacement(BaseModel):
    """Measurements in the host frame's Y-up axes, with Euler angles in degrees."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    parent: Annotated[str, Field(min_length=1, max_length=400)]
    translation_mm: Vector
    rotation_deg_xyz: Vector = Field(default_factory=lambda: [0, 0, 0])
    datum: Annotated[str, Field(min_length=1, max_length=400)]
    access: AccessSpace | None = None


def _validate_geometry(geom):
    if not isinstance(geom, dict) or not geom.get("is_valid"):
        raise ValueError("Expected inspected CAD geometry.")
    vector = TypeAdapter(Vector)
    low, high, size = [
        vector.validate_python(geom.get(key)) for key in ("cad_min_mm", "cad_max_mm", "app_size_mm")
    ]
    if any(b < a for a, b in zip(low, high)) or any(v < 0 for v in size):
        raise ValueError("Invalid CAD bounds.")
    expected = [high[i] - low[i] for i in (0, 2, 1)]
    if any(abs(a - b) > 0.003 for a, b in zip(expected, size)):
        raise ValueError("Source bounds and dimensions must agree.")


class MatingChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance: Name
    revision: Annotated[int, Field(ge=1, strict=True)]
    previous_spec: MatingAdaptationSpec | None
    current_spec: MatingAdaptationSpec
    previous_step_sha256: Digest | None
    current_step_sha256: Digest


class CadInstance(BaseModel):
    """Portable project data. Only digests from the named artifact store can be resolved."""

    model_config = ConfigDict(extra="forbid")
    name: Name
    placement: CadPlacement
    source_units: Literal["auto", "mm", "in"]
    source_up_axis: Literal["y", "z"]
    title: str
    provenance: dict[str, JsonValue]
    license_gate: dict[str, JsonValue]
    geometry: dict[str, JsonValue]
    bom: list[dict[str, JsonValue]]
    artifact_hashes: dict[
        Literal[
            "original", "normalized", "preview", "adapter-step", "adapter-stl", "adapter-preview",
            "license-text"
        ],
        Digest,
    ]
    adaptation: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def validate_source(self):
        prov = self.provenance
        validate_pin(
            prov.get("source_host"),
            prov.get("repo_full_name"),
            prov.get("commit_sha"),
            prov.get("file_path"),
            prov.get("raw_url"),
            prov.get("repo_url"),
        )
        if prov.get("file_sha256") != self.artifact_hashes.get("original") or not prov.get(
            "spdx_id"
        ):
            raise ValueError("The source hash and license must accompany the original.")
        if not self.license_gate.get("allowed") or not self.geometry.get("is_valid"):
            raise ValueError("The CAD source needs an accepted license and inspected geometry.")
        is_stl = prov["file_path"].lower().endswith(".stl")
        kind = self.geometry.get("cad_kind")
        if (
            is_stl
            and (kind not in ("ascii_stl", "binary_stl") or self.source_units == "auto")
            or not is_stl
            and (kind != "step_brep" or "normalized" not in self.artifact_hashes)
            or self.license_gate.get("spdx_id") != prov["spdx_id"]
        ):
            raise ValueError("The CAD source format, units and license must match its inspection.")
        _validate_geometry(self.geometry)
        transform = self.geometry.get("normalization_transform")
        if (
            not isinstance(transform, dict)
            or transform.get("source_up_axis") != self.source_up_axis
            or transform.get("source_units_applied") != self.source_units
            or not self.geometry.get("detected_units")
        ):
            raise ValueError("Missing or inconsistent CAD normalization metadata.")
        if self.geometry.get("cad_kind") not in ("step_brep", "ascii_stl", "binary_stl"):
            raise ValueError("Unsupported CAD geometry for this instance.")
        if self.adaptation is not None:
            _validate_geometry(self.adaptation.get("custom_geometry"))
            MatingAdaptationSpec.model_validate(self.adaptation.get("spec"))
            intersection = self.adaptation.get("interference")
            if (
                self.adaptation.get("mode") != "mating"
                or not isinstance(intersection, dict)
                or type(intersection.get("passed")) is not bool
                or intersection.get("method") != "brep_boolean_intersection"
                or not {"adapter-preview", "adapter-step", "adapter-stl"}
                <= self.artifact_hashes.keys()
            ):
                raise ValueError(
                    "The saved mating part is missing geometry or interference evidence."
                )
        if "preview" not in self.artifact_hashes:
            raise ValueError("The CAD source has no inspected preview.")
        return self


def selected_source(project, name):
    if name is None:
        return project.get("upstream_project")
    return (project.get("cad_instances") or {}).get(name)


def validate_pin(host, repo, commit, path, raw=None, repo_url=None):
    if (
        host not in ("github", "gitlab")
        or not isinstance(repo, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", repo)
        or any(item in (".", "..") for item in repo.split("/"))
    ):
        raise ValueError("Choose a GitHub or GitLab repository for this CAD instance.")
    if not isinstance(commit, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?", commit
    ):
        raise ValueError("A named CAD instance requires a full immutable Git commit hash.")
    if not isinstance(path, str) or Path(path).suffix.lower() not in (".step", ".stp", ".stl"):
        raise ValueError("Choose a STEP or STL file for a placed CAD instance.")
    expected = resolve_raw_file_url(host, repo, commit, path)
    if (
        raw
        and raw != expected
        or repo_url
        and (not isinstance(repo_url, str) or repo_url.rstrip("/") != f"https://{host}.com/{repo}")
    ):
        raise ValueError("The source URL must identify the same repository, file and commit.")


# All file extensions come from this map. Snapshots never supply a local path.
_ARTIFACTS = {
    "original": ("source_path", None),
    "license-text": ("license_text_path", ".txt"),
    "normalized": ("normalized_step_path", ".step"),
    "preview": ("preview_glb_path", ".glb"),
    "adapter-step": ("custom_step_path", ".step"),
    "adapter-stl": ("custom_stl_path", ".stl"),
    "adapter-preview": ("custom_glb_path", ".glb"),
}


def _blob_path(role, digest, provenance):
    if (
        role not in _ARTIFACTS
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise ValueError("Invalid CAD artifact reference.")
    extension = _ARTIFACTS[role][1] or (
        ".stl" if provenance["file_path"].lower().endswith(".stl") else ".step"
    )
    return upstream_cache_dir() / "instances" / (digest + extension)


def instance_file(instance, role):
    digest = instance["artifact_hashes"].get(role)
    if digest is None:
        return None
    path = _blob_path(role, digest, instance["provenance"])
    return path if path.is_file() else None


def _store_files(paths, provenance):
    hashes = {}
    for role, (field, _) in _ARTIFACTS.items():
        if not paths.get(field):
            continue
        data = Path(paths[field]).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if role == "original" and digest != provenance["file_sha256"]:
            raise ValueError("The original CAD no longer matches its verified digest.")
        dest = _blob_path(role, digest, provenance)
        if not dest.exists():
            _atomic_write(dest, data)
        hashes[role] = digest
    return hashes


def _finish(project, lab_profile):
    from app.design_workflow import refresh_workflow
    from app.project_storage import make_snapshot
    from app.spatial_fit import refresh_spatial_fit

    project["revision"] += 1
    project["status"] = "draft"
    project.pop("machine_approval", None)
    refresh_spatial_fit(project, lab_profile=lab_profile)
    refresh_workflow(project)
    make_snapshot(project, [])
    return project


async def attach_instance(project, name, placement, options, lab_profile):
    from app.project_assembly import _geometry
    from app.upstream_service import read_upstream_source

    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", name):
        raise ValueError("Give the instance a lowercase name using letters, digits or hyphens.")
    pose = CadPlacement.model_validate(placement).model_dump()
    graph = _geometry(project)
    if not graph.instances:
        raise ValueError("Compile a host design before attaching named CAD.")
    if pose["parent"] != "project":
        parent = next(
            (
                part
                for key, part in graph.instances.items()
                if pose["parent"] in (key, part["id"]) and part["placement"]
            ),
            None,
        )
        if parent is None:
            raise ValueError(f"Unknown or unplaced parent frame: {pose['parent']}.")
        pose["parent"] = parent["id"]
    pin = [options.get(k) for k in ("source_host", "repo_full_name", "commit_sha", "cad_file_path")]
    validate_pin(*pin, options.get("raw_url"), options.get("repo_url"))
    units, axis = options.get("source_units", "auto"), options.get("source_up_axis", "z")
    if str(pin[3]).lower().endswith(".stl") and units == "auto":
        raise ValueError(
            "STL has no declared units. Supply measured source_units before placement."
        )
    records = project.get("cad_instances") or {}
    if name in records:
        saved = records[name]
        prov = saved["provenance"]
        same = [
            prov[k] for k in ("source_host", "repo_full_name", "commit_sha", "file_path")
        ] == pin
        requested_hash = options.get("expected_sha256")
        if (
            not same
            or saved["source_units"] != units
            or saved["source_up_axis"] != axis
            or requested_hash
            and requested_hash.lower() != prov["file_sha256"]
            or options.get("local_bytes") is not None
            and hashlib.sha256(options["local_bytes"]).hexdigest() != prov["file_sha256"]
        ):
            raise ValueError(f"Instance '{name}' already identifies another pinned source.")
        if saved["placement"] != pose:
            raise ValueError(f"Instance '{name}' already exists with different measurements.")
        return project
    with TemporaryDirectory(dir=upstream_cache_dir()) as staging:
        source = await read_upstream_source(**options, artifact_dir=Path(staging))
        if source["geometry"].get("cad_kind") in ("ascii_stl", "binary_stl"):
            source["geometry"]["detected_units"] = "unitless"
        record = CadInstance(
            name=name,
            placement=pose,
            source_units=units,
            source_up_axis=axis,
            title=source["title"],
            provenance=source["provenance"],
            license_gate=source["license_gate"],
            geometry=source["geometry"],
            bom=source["bom"],
            artifact_hashes=_store_files(source["artifacts"], source["provenance"]),
        )
    updated = deepcopy(project)
    updated.setdefault("cad_instances", {})[name] = record.model_dump()
    return _finish(updated, lab_profile)


async def adapt_instance(project, name, spec, lab_profile):
    instance = (project.get("cad_instances") or {}).get(name)
    if not instance:
        raise ValueError(f"Unknown CAD instance '{name}'.")
    mating = MatingAdaptationSpec.model_validate(spec)
    if (instance.get("adaptation") or {}).get("spec") == mating.model_dump():
        return project
    normalized = instance_file(instance, "normalized")
    original = instance_file(instance, "original")
    if normalized is None or original is None:
        raise ValueError("A cached, solid STEP source is required to generate the mating part.")
    with TemporaryDirectory(dir=upstream_cache_dir()) as staging:
        adaptation = await adapt_upstream_mating(
            {
                **instance,
                "artifacts": {
                    "source_path": str(original),
                    "normalized_step_path": str(normalized),
                },
            },
            mating,
            lab_profile=lab_profile,
            artifact_dir=Path(staging),
        )
        files = _store_files(adaptation.pop("artifacts"), instance["provenance"])
    updated = deepcopy(project)
    target = updated["cad_instances"][name]
    target["artifact_hashes"].update(files)
    target["adaptation"] = adaptation
    updated["mating_change"] = MatingChange(
        instance=name, revision=project["revision"] + 1,
        previous_spec=(instance.get("adaptation") or {}).get("spec"),
        current_spec=mating,
        previous_step_sha256=instance["artifact_hashes"].get("adapter-step"),
        current_step_sha256=files["adapter-step"],
    ).model_dump()
    return _finish(updated, lab_profile)
