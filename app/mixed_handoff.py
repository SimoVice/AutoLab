"""Issue and reopen a complete, revision-bound mixed assembly without the issuing cache."""

import io
import json
import re
import zipfile
from copy import deepcopy

from app.bom_engine import bom_to_csv, build_complete_bom
from app.cad_worker import execute_cad
from app.mixed_revision import change_impact, device_bindings, guide, host_software
from app.mixed_sources import collect_sources, digest, install_blobs, prepare_sources
from app.project_assembly import physical_assembly
from app.project_storage import Snapshot, decode_snapshot, restore_snapshot
from app.upstream_cad_ingest import _atomic_write, upstream_cache_dir

MAX_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 384 * 1024 * 1024
MAX_MEMBERS = 256
FORMAT = "autolab-mixed-assembly"
OUTPUTS = {
    "project.json",
    "assembly/physical-assembly.json",
    "assembly/changes.json",
    "cad/assembly.step",
    "cad/assembly.stl",
    "cad/geometry.json",
    "assembly/guide.md",
    "purchasing/bom.json",
    "purchasing/bom.csv",
    "software/device-bindings.json",
    "sources/NOTICES.md",
}


def encode(record):
    return json.dumps(
        record, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ).encode()


def _binding(assembly):
    return {key: assembly[key] for key in ("project_id", "revision", "geometry_sha256")}


def snapshot_key(snapshot, profile, procurement):
    content = {k: v for k, v in snapshot.items() if k != "saved_at"}
    return digest(encode([content, profile, procurement]))


def keep_package(raw, key):
    sha = digest(raw)
    path = upstream_cache_dir() / "handoffs" / (sha + ".zip")
    if not path.is_file() or digest(path.read_bytes()) != sha:
        _atomic_write(path, raw)
    return {"key": key, "sha256": sha}


def read_issued(reference, key):
    if not reference or reference.get("key") != key:
        return None
    sha = reference["sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return None
    path = upstream_cache_dir() / "handoffs" / (sha + ".zip")
    if path.is_file():
        data = path.read_bytes()
        if digest(data) == sha:
            return data
    return None


def _pack(files, binding):
    if len(files) + 1 > MAX_MEMBERS:
        raise ValueError("The complete assembly exceeds the portable package limit.")
    entries = {
        name: {"sha256": digest(data), "bytes": len(data)} for name, data in sorted(files.items())
    }
    manifest = encode({"format": FORMAT, "version": 1, **binding, "files": entries})
    if len(manifest) + sum(map(len, files.values())) > MAX_EXPANDED_BYTES:
        raise ValueError("The complete assembly exceeds the portable package limit.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        archive.writestr("manifest.json", manifest)
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    if buf.tell() > MAX_PACKAGE_BYTES:
        raise ValueError("The complete assembly exceeds the 128 MiB package limit.")
    return buf.getvalue()


async def build_package(project, snapshot, profile=None, procurement=None):
    if not project.get("cad_instances"):
        raise ValueError("Attach a named CAD instance before issuing a mixed package.")
    from starlette.concurrency import run_in_threadpool

    portable = deepcopy(snapshot)
    files = await run_in_threadpool(collect_sources, portable)
    portable_bytes = encode(portable)
    decode_snapshot(portable_bytes)
    assembly = physical_assembly(project)
    binding = _binding(assembly)
    bom = build_complete_bom(project, lab_profile=profile, procurement_report=procurement)
    bom["project_id"] = binding["project_id"]
    bom["geometry_sha256"] = binding["geometry_sha256"]
    issued_bom = encode(bom)
    impact = change_impact(project, assembly)
    files.update(
        {
            "project.json": portable_bytes,
            "assembly/physical-assembly.json": encode(assembly),
            "assembly/changes.json": encode({**binding, "last_mating_change": impact}),
            "assembly/guide.md": guide(project, assembly, impact),
            "purchasing/bom.json": issued_bom,
            "purchasing/bom.csv": bom_to_csv(json.loads(issued_bom)),
            "software/device-bindings.json": encode(device_bindings(project, assembly, impact)),
        }
    )
    files.update(host_software(project, assembly))
    files.update(await execute_cad({"_mixed_assembly": project}))
    geometry = json.loads(files["cad/geometry.json"])
    files["cad/geometry.json"] = encode({**binding, **geometry})
    return await run_in_threadpool(_pack, files, binding)


def _unpack(raw):
    if len(raw) > MAX_PACKAGE_BYTES:
        raise ValueError("The assembly package exceeds 128 MiB.")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        names = {i.filename for i in infos}
        if (
            len(infos) != len(names)
            or len(infos) > MAX_MEMBERS
            or "manifest.json" not in names
            or sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES
            or archive.getinfo("manifest.json").file_size > 128 * 1024
        ):
            raise ValueError("Invalid assembly package directory.")
        if any(
            i.flag_bits & 1
            or i.compress_type not in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED)
            or not re.fullmatch(r"(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.[a-z]+", i.filename)
            for i in infos
        ):
            raise ValueError("Unexpected assembly package member.")
        manifest = json.loads(archive.read("manifest.json"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != FORMAT
            or manifest.get("version") != 1
            or not isinstance(manifest.get("files"), dict)
            or set(manifest["files"]) != names - {"manifest.json"}
            or not OUTPUTS <= names
        ):
            raise ValueError("Incomplete or unsupported AutoLab assembly manifest.")
        files = {}
        for name, record in manifest["files"].items():
            info = archive.getinfo(name)
            if not isinstance(record, dict) or record.get("bytes") != info.file_size:
                raise ValueError(f"Incorrect size for {name}.")
            data = archive.read(info)
            if digest(data) != record.get("sha256"):
                raise ValueError(f"Digest mismatch for {name}.")
            files[name] = data
    return manifest, files


def open_package(raw, profile=None):
    try:
        manifest, files = _unpack(raw)
        snapshot = decode_snapshot(files["project.json"])
        Snapshot.model_validate(snapshot)
        if not snapshot.get("cad_instances"):
            raise ValueError("The mixed package is missing its CAD instances.")
        members, blobs = prepare_sources(snapshot, files)
        project, history = restore_snapshot(snapshot, lab_profile=profile)
        assembly = physical_assembly(project)
        software = host_software(project, assembly)
        if set(files) != OUTPUTS | members | software.keys():
            raise ValueError("Unexpected assets in the portable assembly.")
        for name, expected in software.items():
            issued = files[name]
            if name.endswith(".json"):
                matches = json.loads(issued) == json.loads(expected)
            else:
                matches = issued.replace(b"\r\n", b"\n") == expected.replace(b"\r\n", b"\n")
            if not matches:
                raise ValueError("The issued host software differs from the saved design.")
        binding = _binding(assembly)
        if (
            _binding(manifest) != binding
            or json.loads(files["assembly/physical-assembly.json"]) != assembly
        ):
            raise ValueError("The issued assembly does not match the rebuilt project.")
        for name in ("cad/geometry.json", "assembly/changes.json", "software/device-bindings.json"):
            if _binding(json.loads(files[name])) != binding:
                raise ValueError(f"The issued {name} belongs to another revision.")
        bom = json.loads(files["purchasing/bom.json"])
        if (
            bom["project_revision"] != binding["revision"]
            or bom["project_id"] != binding["project_id"]
            or bom["geometry_sha256"] != binding["geometry_sha256"]
        ):
            raise ValueError("The issued purchasing list belongs to another revision.")
        if files["purchasing/bom.csv"] != bom_to_csv(bom):
            raise ValueError("The issued purchasing lists disagree.")
        impact = change_impact(project, assembly)
        if (
            json.loads(files["assembly/changes.json"])["last_mating_change"] != impact
            or json.loads(files["software/device-bindings.json"])
            != device_bindings(project, assembly, impact)
            or files["assembly/guide.md"] != guide(project, assembly, impact)
        ):
            raise ValueError("The issued installation records disagree with the saved design.")
        placed = {p["id"] for p in assembly["instances"] if p["placement"]}
        exported = [p["instance_id"] for p in json.loads(files["cad/geometry.json"])["parts"]]
        if len(placed) != len(exported) or placed != set(exported):
            raise ValueError("The CAD index disagrees with the scene.")
        install_blobs(blobs)
        return project, history
    except (
        zipfile.BadZipFile,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        RuntimeError,
        RecursionError,
        UnicodeError,
        OSError,
    ) as exc:
        raise ValueError("The assembly package is invalid or unreadable.") from exc
