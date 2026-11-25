"""Enumerate portable CAD assets using role names and immutable content digests."""

import hashlib
from pathlib import Path

from app.cad_instances import _ARTIFACTS, _blob_path, instance_file, validate_pin
from app.open_hardware_license import evaluate_license_gate, render_third_party_notices
from app.upstream_cad_adapt import MatingAdaptationSpec, _spec_digest
from app.upstream_cad_ingest import _atomic_write, upstream_cache_dir

HOST_ROLES = {
    **_ARTIFACTS,
    "assembly-step": ("assembly_step_path", ".step"),
    "assembly-preview": ("assembly_glb_path", ".glb"),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _license(source):
    p = source["provenance"]
    validate_pin(
        p["source_host"],
        p["repo_full_name"],
        p["commit_sha"],
        p["file_path"],
        p.get("raw_url"),
        p.get("repo_url"),
    )
    gate = evaluate_license_gate(p["spdx_id"])
    if not gate["allowed"] or not gate["derivatives_ok"]:
        raise ValueError(
            f"Review redistribution rights for {p['repo_full_name']} before packaging."
        )


def _extension(role, prov):
    return HOST_ROLES[role][1] or (
        ".stl" if prov["file_path"].lower().endswith(".stl") else ".step"
    )


def _host_roles(source):
    geom = source["geometry"]
    if geom["cad_kind"] not in ("step_brep", "binary_stl", "ascii_stl"):
        raise ValueError("Mixed packages support inspected STEP and STL hosts.")
    roles = ["original", "preview"]
    if geom["cad_kind"] == "step_brep":
        roles.append("normalized")
    if adapter := source.get("adaptation"):
        if adapter["mode"] != "mating":
            raise ValueError("A mixed assembly requires a static STEP or STL host and mating part.")
        roles.extend(["adapter-step", "adapter-stl", "adapter-preview", "assembly-preview"])
        if "normalized" in roles:
            roles.append("assembly-step")
    return roles


def _host_blob(role, value, prov):
    import re

    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid host CAD digest.")
    return upstream_cache_dir() / "handoff-host" / (value + _extension(role, prov))


def legacy_files(source):
    """Reconstruct only paths owned by the importer, including pre-handoff projects."""
    _license(source)
    p = source["provenance"]
    roles = _host_roles(source)
    if refs := source.get("handoff_artifacts"):
        if set(refs) != set(roles) or refs["original"] != p["file_sha256"]:
            raise ValueError("Incomplete host CAD references.")
        return {r: _host_blob(r, refs[r], p) for r in roles}
    root = upstream_cache_dir()
    sha = p["file_sha256"]
    _host_blob("original", sha, p)
    stem = Path(p["file_path"]).name
    paths = {
        "original": root / "sources" / sha / stem,
        "preview": root / "inspected" / sha / "preview.glb",
        "normalized": root / "inspected" / sha / "normalized.step",
    }
    if a := source.get("adaptation"):
        spec = MatingAdaptationSpec.model_validate(a["spec"]).model_dump()
        key = _spec_digest({"sha": sha, "spec": spec, "overlap": 0.0})
        folder = root / "adapted" / f"{sha[:12]}-{key}"
        for role in roles:
            if role.startswith(("adapter-", "assembly-")):
                base = (
                    "custom-" + spec["kind"].replace("_", "-")
                    if role.startswith("adapter-")
                    else "adapted-assembly"
                )
                paths[role] = folder / (base + _extension(role, p))
    return {role: paths[role] for role in roles}


def _member(group, role, prov):
    return f"sources/{group}/{role}{_extension(role, prov)}"


def _assets(snapshot):
    for name, item in sorted(snapshot["cad_instances"].items()):
        _license(item)
        for role, sha in item["artifact_hashes"].items():
            yield (
                _member("instances/" + name, role, item["provenance"]),
                sha,
                _blob_path(role, sha, item["provenance"]),
            )
    if host := snapshot.get("upstream_project"):
        for role, path in legacy_files(host).items():
            yield _member("host", role, host["provenance"]), host["handoff_artifacts"][role], path


def _read(path):
    if not path or not path.is_file():
        raise ValueError(
            "An original or generated CAD asset is missing. Reopen its complete assembly package."
        )
    return path.read_bytes()


def collect_sources(snapshot):
    """Remove host-local paths from the portable snapshot and include attributed originals."""
    files = {}
    sources = [
        (f"instances/{name}", item) for name, item in sorted(snapshot["cad_instances"].items())
    ]
    if host := snapshot.get("upstream_project"):
        refs = {}
        for role, path in legacy_files(host).items():
            content = _read(path)
            refs[role] = digest(content)
            files[_member("host", role, host["provenance"])] = content
        if refs["original"] != host["provenance"]["file_sha256"]:
            raise ValueError("The original host CAD failed its pinned digest.")
        host["handoff_artifacts"] = refs
        host.pop("artifacts", None)
        if host.get("adaptation"):
            host["adaptation"].pop("artifacts", None)
        sources.append(("host", host))
    for name, item in snapshot["cad_instances"].items():
        _license(item)
        for role, sha in item["artifact_hashes"].items():
            data = _read(instance_file(item, role))
            if digest(data) != sha:
                raise ValueError(f"The cached {name} {role} failed its content check.")
            files[_member("instances/" + name, role, item["provenance"])] = data
    notices = [
        render_third_party_notices(
            source,
            original_path=_member(group, "original", source["provenance"]),
            adaptation_path="sources/" + group + "/adapter-*",
        )
        for group, source in sources
    ]
    if snapshot.get("foundation_id"):
        from app.upstream_cad import STEP_PATH, foundation_metadata, original_step_bytes

        meta = foundation_metadata(snapshot["foundation_bed_drop_mm"])
        files["sources/foundation/original.step"] = original_step_bytes()
        files["sources/foundation/LICENSE.txt"] = (
            STEP_PATH.with_suffix("").with_suffix(".LICENSE.txt").read_bytes()
        )
        notices.append(f"Jubilee by Machine Agency · {meta['license']} · {meta['source_url']}")
    files["sources/NOTICES.md"] = "\n\n".join(notices).encode()
    return files


def prepare_sources(snapshot, files):
    host = snapshot.get("upstream_project") or {}
    if host and (
        "artifacts" in host
        or "artifacts" in (host.get("adaptation") or {})
        or not host.get("handoff_artifacts")
    ):
        raise ValueError("Host CAD assets must use portable digest references.")
    blobs = []
    members = set()
    for member, sha, path in _assets(snapshot):
        if member not in files or digest(files[member]) != sha:
            raise ValueError(f"Missing or mismatched CAD artifact {member}.")
        members.add(member)
        blobs.append((path, files[member]))
    if snapshot.get("foundation_id"):
        from app.upstream_cad import STEP_PATH, STEP_SHA256

        original = "sources/foundation/original.step"
        license_file = "sources/foundation/LICENSE.txt"
        if (
            digest(files[original]) != STEP_SHA256
            or files[license_file]
            != STEP_PATH.with_suffix("").with_suffix(".LICENSE.txt").read_bytes()
        ):
            raise ValueError("The packaged Jubilee foundation differs from its pinned source.")
        members.update((original, license_file))
    if host:
        for role, path in legacy_files(host).items():
            owner = host["adaptation"] if role.startswith(("adapter-", "assembly-")) else host
            owner.setdefault("artifacts", {})[HOST_ROLES[role][0]] = str(path)
    return members, blobs


def install_blobs(blobs):
    for path, data in blobs:
        if not path.is_file() or digest(path.read_bytes()) != digest(data):
            _atomic_write(path, data)
