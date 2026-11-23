"""Upstream BOM, assembly, software ingestion, and attributed build-package ZIP export."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from app.assembly import _csv_cell
from app.open_hardware_license import (
    render_third_party_notices,
    sha256_bytes,
    validate_repo_file_path,
)
from app.upstream_cad_ingest import resolve_raw_file_url

MAX_DOC_FETCH_BYTES = 256 * 1024
MAX_PACKAGE_ARCHIVE_BYTES = 128 * 1024 * 1024

_BULLET_BOM_RE = re.compile(
    r"^\s*[-*•]\s+(?:(\d+)\s*[x×]\s+)?(.+?)(?:\s+[x×]\s*(\d+))?\s*$", re.IGNORECASE
)
_NUMBERED_STEP_RE = re.compile(r"^\s*(?:step\s*)?(\d+)[.):\-\s]+(.+)$", re.IGNORECASE)


async def fetch_upstream_doc_files(
    source_host: str,
    repo_full_name: str,
    commit_sha: str,
    doc_paths: list[str],
    *,
    max_files: int = 12,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    """Fetch bounded UTF-8 text documents from commit-pinned allowlisted URLs."""
    cleaned: list[str] = []
    for path in doc_paths:
        try:
            safe = validate_repo_file_path(path, allow_cad=True, allow_docs=True)
            if safe not in cleaned:
                cleaned.append(safe)
        except ValueError:
            continue
    targets = cleaned[:max_files]
    if not targets:
        return {}

    headers = {"User-Agent": "AutoLab-OpenHardware-Discovery/0.1"}

    async def _one(active: httpx.AsyncClient, path: str) -> tuple[str, str | None]:
        try:
            url = resolve_raw_file_url(source_host, repo_full_name, commit_sha, path)
            async with asyncio.timeout(8.0):
                async with active.stream("GET", url, headers=headers, follow_redirects=False) as r:
                    if r.status_code != 200:
                        return path, None
                    buf = bytearray()
                    async for chunk in r.aiter_bytes(32768):
                        if len(buf) + len(chunk) > MAX_DOC_FETCH_BYTES:
                            break
                        buf.extend(chunk)
                    return path, buf.decode("utf-8", errors="replace")
        except (httpx.HTTPError, TimeoutError, ValueError):
            return path, None

    async def _gather_all(active: httpx.AsyncClient) -> dict[str, str]:
        pairs = await asyncio.gather(*(_one(active, p) for p in targets))
        return {p: txt for p, txt in pairs if isinstance(txt, str) and txt.strip()}

    if client is not None:
        return await _gather_all(client)
    async with httpx.AsyncClient(timeout=8.0, trust_env=False) as owned:
        return await _gather_all(owned)


def _norm_key(col: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", col.strip().lower()).strip("_")


def _pick_bom_name(rec: dict[str, str]) -> str | None:
    for key in (
        "part_name",
        "item_name",
        "component_name",
        "part",
        "name",
        "item",
        "component",
        "description",
        "product",
        "title",
    ):
        val = rec.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:200]
    return None


def _parse_qty(raw_val: str | None) -> int | str:
    raw = (raw_val or "1").strip()
    try:
        return max(1, int(float(raw)))
    except ValueError:
        return raw


def _extract_csv_bom(path: str, text: str) -> list[dict[str, Any]]:
    delim = "\t" if path.lower().endswith(".tsv") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    items: list[dict[str, Any]] = []
    for idx, row in enumerate(reader, 1):
        norm = {_norm_key(k): (v or "").strip() for k, v in row.items() if k}
        name = _pick_bom_name(norm)
        if not name:
            continue
        qty = _parse_qty(norm.get("qty") or norm.get("quantity") or norm.get("count"))
        items.append(
            {
                "id": f"upstream-bom-{idx}",
                "name": name,
                "quantity": qty,
                "sku": norm.get("sku") or norm.get("mpn") or norm.get("part_number") or None,
                "supplier": (
                    norm.get("supplier") or norm.get("vendor") or norm.get("source") or None
                ),
                "source_url": norm.get("url") or norm.get("link") or None,
                "notes": norm.get("notes") or norm.get("spec") or norm.get("material") or None,
                "source": "upstream_repo",
                "source_file": path,
                "compatibility_status": "unverified_upstream",
            }
        )
    return items


def _extract_yaml_subset_bom(path: str, text: str) -> list[dict[str, Any]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith("- "):
            if current:
                records.append(current)
            current = {}
            rest = stripped[2:].strip()
            if ":" in rest:
                k, v = rest.split(":", 1)
                current[_norm_key(k)] = v.strip().strip("\"'")
            elif rest:
                current["name"] = rest.strip("\"'")
        elif current is not None and ":" in stripped:
            k, v = stripped.split(":", 1)
            current[_norm_key(k)] = v.strip().strip("\"'")
    if current:
        records.append(current)

    items: list[dict[str, Any]] = []
    for idx, rec in enumerate(records, 1):
        name = _pick_bom_name(rec)
        if not name:
            continue
        qty = _parse_qty(rec.get("qty") or rec.get("quantity") or rec.get("count"))
        items.append(
            {
                "id": f"upstream-yaml-{idx}",
                "name": name,
                "quantity": qty,
                "sku": rec.get("sku") or rec.get("mpn") or None,
                "supplier": rec.get("supplier") or rec.get("vendor") or None,
                "source_url": rec.get("url") or rec.get("link") or None,
                "notes": rec.get("notes") or rec.get("spec") or None,
                "source": "upstream_repo",
                "source_file": path,
                "compatibility_status": "unverified_upstream",
            }
        )
    return items


def _extract_markdown_bom(path: str, text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    lines = text.splitlines()
    # 1. Markdown pipe tables
    i = 0
    while i + 1 < len(lines):
        row = lines[i].strip()
        sep = lines[i + 1].strip()
        if "|" in row and re.fullmatch(r"[\|\s:\-]+", sep) and "-" in sep:
            headers = [_norm_key(c) for c in row.strip("|").split("|")]
            i += 2
            while i < len(lines) and "|" in lines[i]:
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rec = {
                    headers[j]: cells[j] if j < len(cells) else ""
                    for j in range(len(headers))
                    if headers[j]
                }
                name = _pick_bom_name(rec)
                if name:
                    qty = _parse_qty(rec.get("qty") or rec.get("quantity") or rec.get("count"))
                    items.append(
                        {
                            "id": f"upstream-md-{len(items) + 1}",
                            "name": name,
                            "quantity": qty,
                            "sku": rec.get("sku") or rec.get("mpn") or None,
                            "supplier": rec.get("supplier") or rec.get("vendor") or None,
                            "source_url": rec.get("url") or None,
                            "notes": rec.get("notes") or rec.get("spec") or None,
                            "source": "upstream_repo",
                            "source_file": path,
                            "compatibility_status": "unverified_upstream",
                        }
                    )
                i += 1
            continue
        i += 1
    if items:
        return items

    # 2. Bulleted BOM lines (especially under a BOM heading or inside *bom* filenames)
    in_bom_section = "bom" in path.lower() or "parts" in path.lower()
    for raw_line in lines:
        if raw_line.lstrip().startswith("#"):
            hdr = raw_line.lower()
            in_bom_section = any(k in hdr for k in ("bom", "bill of materials", "parts list"))
            continue
        if not in_bom_section:
            continue
        m = _BULLET_BOM_RE.match(raw_line)
        if m:
            q1, name, q2 = m.group(1), m.group(2).strip(), m.group(3)
            qty = int(q1 or q2 or 1)
            items.append(
                {
                    "id": f"upstream-list-{len(items) + 1}",
                    "name": name[:200],
                    "quantity": max(1, qty),
                    "sku": None,
                    "supplier": None,
                    "source_url": None,
                    "notes": None,
                    "source": "upstream_repo",
                    "source_file": path,
                    "compatibility_status": "unverified_upstream",
                }
            )
    return items


def parse_upstream_bom(
    files: dict[str, str],
    adaptation: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Parse upstream BOM formats cleanly and append AutoLab custom adaptation parts separately."""
    items: list[dict[str, Any]] = []
    for path, text in sorted(files.items()):
        lower = path.lower()
        if not any(k in lower for k in ("bom", "parts", "hardware", "shopping")):
            if not (lower.endswith(".md") and "bill of materials" in text.lower()):
                continue
        if lower.endswith((".csv", ".tsv")):
            items.extend(_extract_csv_bom(path, text))
        elif lower.endswith((".yml", ".yaml")):
            items.extend(_extract_yaml_subset_bom(path, text))
        elif lower.endswith(".json"):
            try:
                loaded = json.loads(text)
            except ValueError:
                loaded = None
            rows = (
                loaded
                if isinstance(loaded, list)
                else (loaded.get("parts") or loaded.get("bom") or [])
                if isinstance(loaded, dict)
                else []
            )
            for idx, r in enumerate(rows, 1):
                if isinstance(r, dict) and (r.get("name") or r.get("part")):
                    items.append(
                        {
                            "id": f"upstream-json-{idx}",
                            "name": str(r.get("name") or r.get("part"))[:200],
                            "quantity": r.get("qty") or r.get("quantity") or 1,
                            "sku": r.get("sku") or r.get("mpn"),
                            "supplier": r.get("supplier") or r.get("vendor"),
                            "source_url": r.get("url"),
                            "notes": r.get("notes"),
                            "source": "upstream_repo",
                            "source_file": path,
                            "compatibility_status": "unverified_upstream",
                        }
                    )
        elif lower.endswith((".md", ".markdown")):
            items.extend(_extract_markdown_bom(path, text))

    if adaptation:
        part_id = adaptation.get("part_id") or "custom-part"
        inter = adaptation.get("interference") or {}
        compat = (
            "modeled_zero_interference_untested_hardware"
            if inter.get("passed", True)
            else "modeled_interference_detected"
        )
        mode = adaptation.get("mode", "mating")
        spec = adaptation.get("spec") or {}
        kind_label = spec.get("kind", mode).replace("_", " ")
        cgeom = adaptation.get("custom_geometry") or {}
        size_note = (
            f"Modeled {kind_label} ({' x '.join(str(v) for v in cgeom['app_size_mm'])} mm)."
            if isinstance(cgeom.get("app_size_mm"), list)
            else f"Modeled {kind_label} custom adaptation."
        )
        items.append(
            {
                "id": part_id,
                "name": f"AutoLab custom {kind_label}",
                "quantity": 1,
                "sku": None,
                "supplier": "Local fabrication (3D print / CNC)",
                "source_url": None,
                "notes": size_note,
                "source": "autolab_custom_adaptation",
                "source_file": f"custom/{part_id}.step",
                "compatibility_status": compat,
            }
        )
    return items[:120]


def parse_upstream_assembly(
    files: dict[str, str],
    repo_meta: dict[str, Any],
    adaptation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract ordered upstream assembly steps alongside separate AutoLab integration steps."""
    upstream_steps: list[dict[str, Any]] = []
    source_docs = sorted(
        (p, txt)
        for p, txt in files.items()
        if any(k in p.lower() for k in ("assembly", "build", "guide", "manual", "readme"))
    )
    for path, text in source_docs:
        is_assembly_doc = any(k in path.lower() for k in ("assembly", "build", "guide", "manual"))
        in_section = is_assembly_doc
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#"):
                hdr = s.lstrip("#").strip()
                in_section = is_assembly_doc or any(
                    k in hdr.lower()
                    for k in ("assembly", "build", "setup", "fabrication", "install", "step")
                )
                continue
            if not in_section or not s:
                continue
            m = _NUMBERED_STEP_RE.match(s)
            if m:
                upstream_steps.append(
                    {
                        "order": len(upstream_steps) + 1,
                        "text": m.group(2).strip()[:320],
                        "source": "upstream_repo",
                        "source_file": path,
                    }
                )
            elif len(upstream_steps) < 25 and s.startswith(("- ", "* ")):
                upstream_steps.append(
                    {
                        "order": len(upstream_steps) + 1,
                        "text": s[2:].strip()[:320],
                        "source": "upstream_repo",
                        "source_file": path,
                    }
                )
        if len(upstream_steps) >= 30:
            break

    custom_steps: list[dict[str, Any]] = []
    if adaptation:
        part_id = adaptation.get("part_id") or "custom-part"
        inter = adaptation.get("interference") or {}
        if adaptation.get("mode") == "parametric":
            diffs = ", ".join(
                f"{d['name']}: {d['original']} -> {d['updated']}"
                for d in adaptation.get("parameter_diffs") or []
            )
            custom_steps.append(
                {
                    "order": 1,
                    "text": f"Fabricate parametric adapted part '{part_id}' ({diffs or 'diffs'}).",
                    "source": "autolab_custom_adaptation",
                    "source_file": f"custom/{part_id}",
                }
            )
        else:
            gap = inter.get("min_clearance_mm")
            custom_steps.extend(
                [
                    {
                        "order": 1,
                        "text": (
                            f"Print or machine AutoLab custom mating part '{part_id}' "
                            f"(custom/{part_id}.stl / .step)."
                        ),
                        "source": "autolab_custom_adaptation",
                        "source_file": f"custom/{part_id}.stl",
                    },
                    {
                        "order": 2,
                        "text": (
                            f"Dry-fit unmodified upstream hardware against '{part_id}' "
                            f"(modeled clearance: {gap if gap is not None else 'n/a'} mm; "
                            f"interference check: {inter.get('passed')})."
                        ),
                        "source": "autolab_custom_adaptation",
                        "source_file": "assembly/adapted-assembly.step",
                    },
                ]
            )
        custom_steps.append(
            {
                "order": len(custom_steps) + 1,
                "text": "Verify fasteners, alignment, and mechanical motion before lab use.",
                "source": "autolab_custom_adaptation",
                "source_file": "docs/build-guide.md",
            }
        )

    return {
        "upstream_repo": repo_meta.get("repo_full_name"),
        "commit_sha": repo_meta.get("commit_sha"),
        "upstream_steps": upstream_steps[:30],
        "custom_steps": custom_steps,
    }


def extract_software_references(
    tree_paths: list[str],
    files: dict[str, str],
    repo_meta: dict[str, Any],
) -> dict[str, Any]:
    """Identify firmware and control software stacks without claiming hardware verification."""
    blob_text = "\n".join(files.values()).lower()
    paths_lower = [p.lower() for p in tree_paths]
    stacks: list[str] = []

    def _has_any(tokens: tuple[str, ...]) -> bool:
        return any(t in p for p in paths_lower for t in tokens) or any(
            t in blob_text for t in tokens
        )

    if _has_any(("marlin", "configuration.h")):
        stacks.append("Marlin")
    if _has_any(("klipper", "printer.cfg", "[stepper_")):
        stacks.append("Klipper")
    if _has_any(("grbl", "$100=", "grbl_esp32")):
        stacks.append("GRBL")
    if _has_any(("platformio.ini",)):
        stacks.append("PlatformIO")
    if any(p.endswith(".ino") for p in paths_lower) or "arduino.h" in blob_text:
        stacks.append("Arduino")
    if any(
        p.endswith(".py")
        and any(k in p for k in ("control", "driver", "firmware", "pump", "cli", "app"))
        for p in paths_lower
    ) or "pyserial" in blob_text:
        stacks.append("Python control/driver")
    if _has_any(("micropython", "circuitpython", "code.py")):
        stacks.append("MicroPython / CircuitPython")
    if _has_any(("cmakelists.txt", "package.xml", "rospy", "rclpy")):
        stacks.append("ROS")

    host = str(repo_meta.get("source_host", "github")).lower()
    repo = str(repo_meta.get("repo_full_name", ""))
    commit = str(repo_meta.get("commit_sha", ""))
    entrypoints: list[dict[str, str]] = []
    for path in tree_paths:
        pl = path.lower()
        if any(pl.endswith(ext) for ext in (".ino", ".cfg", ".py", ".c", ".cpp")) and any(
            k in pl
            for k in (
                "firmware",
                "controller",
                "control",
                "driver",
                "main",
                "printer",
                "marlin",
                "grbl",
                "software",
            )
        ):
            url = (
                f"https://github.com/{repo}/blob/{commit}/{path}"
                if host == "github" and repo and commit
                else f"https://gitlab.com/{repo}/-/blob/{commit}/{path}"
                if host == "gitlab" and repo and commit
                else path
            )
            entrypoints.append({"path": path, "url": url})

    return {
        "detected_stacks": stacks,
        "entrypoints": entrypoints[:15],
        "execution_status": "reference_only_unverified",
        "disclaimer": (
            "Upstream firmware and control scripts are indexed for reference at the pinned "
            "commit; AutoLab has not flashed, executed, or validated electrical safety."
        ),
    }


def build_upstream_bom_csv(bom_items: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(
        ["id", "name", "quantity", "source", "compatibility_status", "sku", "supplier", "notes"]
    )
    for item in bom_items:
        writer.writerow(
            [
                _csv_cell(str(item.get("id") or "")),
                _csv_cell(str(item.get("name") or "")),
                _csv_cell(str(item.get("quantity") or 1)),
                _csv_cell(str(item.get("source") or "")),
                _csv_cell(str(item.get("compatibility_status") or "")),
                _csv_cell(str(item.get("sku") or "")),
                _csv_cell(str(item.get("supplier") or "")),
                _csv_cell(str(item.get("notes") or "")),
            ]
        )
    return buf.getvalue().encode("utf-8")


def build_upstream_build_guide_md(upstream_project: dict[str, Any]) -> str:
    prov = upstream_project.get("provenance") or {}
    assembly = upstream_project.get("assembly") or {}
    software = upstream_project.get("software") or {}
    lines = [
        f"# Open-Source Hardware Build Guide: {prov.get('repo_full_name', 'Unknown project')}",
        "",
        f"- **Repository:** {prov.get('repo_url', 'n/a')}",
        f"- **Pinned Commit SHA:** `{prov.get('commit_sha', 'unpinned')}`",
        f"- **SPDX License:** `{prov.get('spdx_id', 'UNKNOWN')}`",
        f"- **Original CAD File:** `{prov.get('file_path', 'n/a')}`",
        "",
        "## Upstream Assembly Reference Steps",
        "",
    ]
    up_steps = assembly.get("upstream_steps") or []
    if up_steps:
        for st in up_steps:
            lines.append(f"{st['order']}. {st['text']} *({st.get('source_file', 'repo')})*")
    else:
        lines.append("1. Consult the upstream repository documentation for primary assembly steps.")

    lines.extend(["", "## AutoLab Custom Adaptation Steps", ""])
    c_steps = assembly.get("custom_steps") or []
    if c_steps:
        for st in c_steps:
            lines.append(f"{st['order']}. {st['text']} *(`{st.get('source_file', 'custom')}`)*")
    else:
        lines.append("1. No custom mating or parametric adaptation generated in this revision.")

    lines.extend(["", "## Software & Firmware References (Unverified)", ""])
    stacks = software.get("detected_stacks") or []
    lines.append(f"- **Detected Stacks:** {', '.join(stacks) if stacks else 'None detected'}")
    lines.append(f"- **Status:** `{software.get('execution_status', 'reference_only_unverified')}`")
    for ep in software.get("entrypoints") or []:
        lines.append(f"- `{ep['path']}`: {ep['url']}")
    lines.append("")
    return "\n".join(lines)


def build_upstream_package_zip(project: dict[str, Any]) -> bytes:
    """Assemble an attributed, manifest-verified build package ZIP for an upstream project."""
    up = project.get("upstream_project")
    if not isinstance(up, dict):
        raise ValueError("Project has no ingested upstream open-source project.")

    prov = up.get("provenance") or {}
    artifacts = up.get("artifacts") or {}
    adaptation = up.get("adaptation") or {}
    adapt_art = adaptation.get("artifacts") or {}
    part_id = adaptation.get("part_id") or "custom-part"

    files: dict[str, bytes] = {}
    src_path = Path(str(artifacts.get("source_path") or ""))
    if not src_path.is_file():
        raise ValueError("Cached original upstream CAD source file is missing.")
    src_bytes = src_path.read_bytes()
    expected_sha = prov.get("file_sha256")
    if expected_sha and sha256_bytes(src_bytes) != expected_sha:
        raise ValueError("Cached upstream source SHA-256 mismatch.")
    orig_name = PurePosixPath(str(prov.get("file_path") or src_path.name)).name
    files[f"upstream/source/{orig_name}"] = src_bytes

    for label, key in (
        ("upstream/normalized.step", "normalized_step_path"),
        ("upstream/preview.glb", "preview_glb_path"),
    ):
        p = artifacts.get(key)
        if p and Path(p).is_file():
            files[label] = Path(p).read_bytes()

    if adaptation:
        for suffix, key in (
            (".step", "custom_step_path"),
            (".stl", "custom_stl_path"),
            (".glb", "custom_glb_path"),
        ):
            p = adapt_art.get(key)
            if p and Path(p).is_file():
                files[f"custom/{part_id}{suffix}"] = Path(p).read_bytes()
        src_adapt = adapt_art.get("adapted_source_path")
        if src_adapt and Path(src_adapt).is_file():
            files[f"custom/{Path(src_adapt).name}"] = Path(src_adapt).read_bytes()
        for label, key in (
            ("assembly/adapted-assembly.step", "assembly_step_path"),
            ("assembly/adapted-assembly.glb", "assembly_glb_path"),
        ):
            p = adapt_art.get(key)
            if p and Path(p).is_file():
                files[label] = Path(p).read_bytes()

    bom_items = up.get("bom") or []
    files["bom/bom.csv"] = build_upstream_bom_csv(bom_items)
    files["bom/bom.json"] = json.dumps(bom_items, indent=2).encode("utf-8")
    files["docs/build-guide.md"] = build_upstream_build_guide_md(up).encode("utf-8")
    files["docs/software-references.json"] = json.dumps(
        up.get("software") or {}, indent=2
    ).encode("utf-8")
    files["THIRD_PARTY_NOTICES.md"] = render_third_party_notices(up).encode("utf-8")

    manifest = {
        "schema_version": 1,
        "project_id": project.get("id"),
        "revision": project.get("revision"),
        "provenance": prov,
        "license_gate": up.get("license_gate"),
        "adaptation_summary": (
            {
                "mode": adaptation.get("mode"),
                "part_id": part_id,
                "interference": adaptation.get("interference"),
                "printer_fit": adaptation.get("printer_fit"),
                "parameter_diffs": adaptation.get("parameter_diffs"),
            }
            if adaptation
            else None
        ),
        "limitations": [
            "Upstream CAD/BOM compatibility with user lab hardware requires physical verification.",
            "AutoLab adaptations check modeled interference only; FEA/hardware remain unverified.",
        ],
        "files": [
            {"name": name, "size": len(files[name]), "sha256": sha256_bytes(files[name])}
            for name in sorted(files)
        ],
    }
    files["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, files[name])
    raw_zip = buf.getvalue()
    if len(raw_zip) > MAX_PACKAGE_ARCHIVE_BYTES:
        raise ValueError("Generated upstream package archive exceeds 128 MB limit.")
    return raw_zip
