"""License detection, security allowlists, commit/hash provenance, and attribution notices."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

ALLOWED_API_HOSTS = frozenset(
    {
        "api.github.com",
        "gitlab.com",
        "api.openalex.org",
        "www.ebi.ac.uk",
        "3d.nih.gov",
    }
)
ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "raw.githubusercontent.com",
        "github.com",
        "codeload.github.com",
        "api.github.com",
        "gitlab.com",
        "3d.nih.gov",
    }
)

ALLOWED_CAD_EXTENSIONS = frozenset({".step", ".stp", ".stl", ".scad", ".py"})
ALLOWED_DOC_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".rst",
        ".txt",
        ".csv",
        ".tsv",
        ".yml",
        ".yaml",
        ".json",
        ".toml",
        ".ino",
        ".cfg",
        ".conf",
        ".ini",
        ".h",
        ".hpp",
        ".c",
        ".cpp",
        ".py",
    }
)
FORBIDDEN_EXTENSIONS = frozenset(
    {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bat",
        ".cmd",
        ".ps1",
        ".sh",
        ".bash",
        ".zsh",
        ".msi",
        ".dmg",
        ".pkg",
        ".deb",
        ".rpm",
        ".apk",
        ".jar",
        ".war",
        ".wasm",
        ".bin",
        ".com",
        ".scr",
        ".vbs",
        ".vbe",
        ".app",
    }
)

_SPDX_TABLE: dict[str, dict[str, Any]] = {
    "MIT": {
        "category": "permissive",
        "url": "https://opensource.org/licenses/MIT",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": False,
        "derivatives_ok": True,
        "summary": "Permissive license; retain copyright and permission notice.",
    },
    "Apache-2.0": {
        "category": "permissive",
        "url": "https://www.apache.org/licenses/LICENSE-2.0",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": False,
        "derivatives_ok": True,
        "summary": "Permissive license with explicit patent grant; retain attribution/NOTICE.",
    },
    "BSD-2-Clause": {
        "category": "permissive",
        "url": "https://opensource.org/licenses/BSD-2-Clause",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": False,
        "derivatives_ok": True,
        "summary": "Permissive 2-clause BSD license; retain copyright notice.",
    },
    "BSD-3-Clause": {
        "category": "permissive",
        "url": "https://opensource.org/licenses/BSD-3-Clause",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": False,
        "derivatives_ok": True,
        "summary": "Permissive 3-clause BSD license; retain notice and non-endorsement clause.",
    },
    "ISC": {
        "category": "permissive",
        "url": "https://opensource.org/licenses/ISC",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": False,
        "derivatives_ok": True,
        "summary": "Permissive ISC license; retain copyright and permission notice.",
    },
    "CC0-1.0": {
        "category": "permissive",
        "url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "requires_attribution": False,
        "commercial_ok": True,
        "copyleft": False,
        "derivatives_ok": True,
        "summary": "Public domain dedication (CC0 1.0).",
    },
    "Unlicense": {
        "category": "permissive",
        "url": "https://unlicense.org/",
        "requires_attribution": False,
        "commercial_ok": True,
        "copyleft": False,
        "derivatives_ok": True,
        "summary": "Public domain dedication (Unlicense).",
    },
    "CC-BY-4.0": {
        "category": "permissive",
        "url": "https://creativecommons.org/licenses/by/4.0/",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": False,
        "derivatives_ok": True,
        "summary": "Creative Commons Attribution 4.0; commercial/derivative use allowed.",
    },
    "CC-BY-3.0": {
        "category": "permissive",
        "url": "https://creativecommons.org/licenses/by/3.0/",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": False,
        "derivatives_ok": True,
        "summary": "Creative Commons Attribution 3.0; commercial/derivative use allowed.",
    },
    "CC-BY-SA-4.0": {
        "category": "copyleft",
        "url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": True,
        "derivatives_ok": True,
        "summary": "CC BY-SA 4.0; adaptations must keep compatible ShareAlike terms.",
    },
    "CC-BY-SA-3.0": {
        "category": "copyleft",
        "url": "https://creativecommons.org/licenses/by-sa/3.0/",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": True,
        "derivatives_ok": True,
        "summary": "CC BY-SA 3.0; adaptations must keep compatible ShareAlike terms.",
    },
    "CC-BY-NC-4.0": {
        "category": "non_commercial",
        "url": "https://creativecommons.org/licenses/by-nc/4.0/",
        "requires_attribution": True,
        "commercial_ok": False,
        "copyleft": False,
        "derivatives_ok": True,
        "summary": "Non-commercial only (CC BY-NC 4.0); commercial deployment restricted.",
    },
    "CC-BY-NC-SA-4.0": {
        "category": "non_commercial",
        "url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        "requires_attribution": True,
        "commercial_ok": False,
        "copyleft": True,
        "derivatives_ok": True,
        "summary": "Non-commercial share-alike (CC BY-NC-SA 4.0); commercial restricted.",
    },
    "CC-BY-ND-4.0": {
        "category": "no_derivatives",
        "url": "https://creativecommons.org/licenses/by-nd/4.0/",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": False,
        "derivatives_ok": False,
        "summary": "No derivatives (CC BY-ND 4.0); distributing modified CAD files is disallowed.",
    },
    "CERN-OHL-P-2.0": {
        "category": "permissive",
        "url": "https://spdx.org/licenses/CERN-OHL-P-2.0.html",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": False,
        "derivatives_ok": True,
        "summary": "CERN Open Hardware Licence v2 Permissive.",
    },
    "CERN-OHL-S-2.0": {
        "category": "copyleft",
        "url": "https://spdx.org/licenses/CERN-OHL-S-2.0.html",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": True,
        "derivatives_ok": True,
        "summary": "CERN OHL v2 Strongly Reciprocal; modified designs require source disclosure.",
    },
    "CERN-OHL-W-2.0": {
        "category": "copyleft",
        "url": "https://spdx.org/licenses/CERN-OHL-W-2.0.html",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": True,
        "derivatives_ok": True,
        "summary": "CERN OHL v2 Weakly Reciprocal; modified hardware files require source notice.",
    },
    "CERN-OHL-1.2": {
        "category": "copyleft",
        "url": "https://spdx.org/licenses/CERN-OHL-1.2.html",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": True,
        "derivatives_ok": True,
        "summary": "CERN OHL 1.2 reciprocal open hardware license.",
    },
    "GPL-3.0-only": {
        "category": "copyleft",
        "url": "https://www.gnu.org/licenses/gpl-3.0.html",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": True,
        "derivatives_ok": True,
        "summary": "GPL v3 copyleft; distributed derivatives must preserve GPL v3 terms.",
    },
    "GPL-3.0-or-later": {
        "category": "copyleft",
        "url": "https://www.gnu.org/licenses/gpl-3.0.html",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": True,
        "derivatives_ok": True,
        "summary": "GPL v3+ copyleft; distributed derivatives must preserve GPL terms.",
    },
    "GPL-2.0-only": {
        "category": "copyleft",
        "url": "https://www.gnu.org/licenses/old-licenses/gpl-2.0.html",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": True,
        "derivatives_ok": True,
        "summary": "GPL v2 copyleft; distributed derivatives must preserve GPL v2 terms.",
    },
    "LGPL-3.0-only": {
        "category": "copyleft",
        "url": "https://www.gnu.org/licenses/lgpl-3.0.html",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": True,
        "derivatives_ok": True,
        "summary": "LGPL v3 weak copyleft license.",
    },
    "MPL-2.0": {
        "category": "copyleft",
        "url": "https://www.mozilla.org/en-US/MPL/2.0/",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": True,
        "derivatives_ok": True,
        "summary": "Mozilla Public License 2.0 (file-level copyleft).",
    },
    "TAPR-OHL-1.0": {
        "category": "copyleft",
        "url": "https://spdx.org/licenses/TAPR-OHL-1.0.html",
        "requires_attribution": True,
        "commercial_ok": True,
        "copyleft": True,
        "derivatives_ok": True,
        "summary": "TAPR Open Hardware License 1.0.",
    },
    "UNKNOWN": {
        "category": "unknown",
        "url": None,
        "requires_attribution": True,
        "commercial_ok": False,
        "copyleft": False,
        "derivatives_ok": False,
        "summary": "License missing or unrecognized; verify redistribution rights manually.",
    },
}

_HINT_ALIASES: dict[str, str] = {
    "mit": "MIT",
    "apache-2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "isc": "ISC",
    "cc0-1.0": "CC0-1.0",
    "cc0": "CC0-1.0",
    "unlicense": "Unlicense",
    "cc-by-4.0": "CC-BY-4.0",
    "cc by 4.0": "CC-BY-4.0",
    "cc-by-3.0": "CC-BY-3.0",
    "cc-by-sa-4.0": "CC-BY-SA-4.0",
    "cc-by-sa-3.0": "CC-BY-SA-3.0",
    "cc-by-nc-4.0": "CC-BY-NC-4.0",
    "cc-by-nc-sa-4.0": "CC-BY-NC-SA-4.0",
    "cc-by-nd-4.0": "CC-BY-ND-4.0",
    "cern-ohl-p-2.0": "CERN-OHL-P-2.0",
    "cern-ohl-s-2.0": "CERN-OHL-S-2.0",
    "cern-ohl-w-2.0": "CERN-OHL-W-2.0",
    "cern-ohl-1.2": "CERN-OHL-1.2",
    "gpl-3.0": "GPL-3.0-only",
    "gpl-3.0-only": "GPL-3.0-only",
    "gpl-3.0-or-later": "GPL-3.0-or-later",
    "gpl-2.0": "GPL-2.0-only",
    "gpl-2.0-only": "GPL-2.0-only",
    "lgpl-3.0": "LGPL-3.0-only",
    "lgpl-3.0-only": "LGPL-3.0-only",
    "mpl-2.0": "MPL-2.0",
    "tapr-ohl-1.0": "TAPR-OHL-1.0",
}

_TEXT_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"cern-ohl-s-2\.0|cern open hardware licence version 2\s*-\s*strongly reciprocal",
        "CERN-OHL-S-2.0",
    ),
    (
        r"cern-ohl-w-2\.0|cern open hardware licence version 2\s*-\s*weakly reciprocal",
        "CERN-OHL-W-2.0",
    ),
    (
        r"cern-ohl-p-2\.0|cern open hardware licence version 2\s*-\s*permissive",
        "CERN-OHL-P-2.0",
    ),
    (r"cern open hardware licence v1\.2|cern-ohl-1\.2", "CERN-OHL-1.2"),
    (r"tapr open hardware license|tapr-ohl", "TAPR-OHL-1.0"),
    (r"attribution-noncommercial-sharealike|cc[- ]by-nc-sa|by-nc-sa/4\.0", "CC-BY-NC-SA-4.0"),
    (r"attribution-noncommercial|cc[- ]by-nc|creativecommons\.org/licenses/by-nc/", "CC-BY-NC-4.0"),
    (r"attribution-noderivatives|cc[- ]by-nd|creativecommons\.org/licenses/by-nd/", "CC-BY-ND-4.0"),
    (r"attribution-sharealike\s*3\.0|cc[- ]by-sa[- ]3\.0|by-sa/3\.0", "CC-BY-SA-3.0"),
    (r"attribution-sharealike|cc[- ]by-sa|by-sa/4\.0", "CC-BY-SA-4.0"),
    (r"attribution\s*3\.0|cc[- ]by[- ]3\.0|licenses/by/3\.0", "CC-BY-3.0"),
    (r"creative commons attribution|cc[- ]by[- ]4\.0|licenses/by/4\.0", "CC-BY-4.0"),
    (r"cc0\s*1\.0|publicdomain/zero/1\.0", "CC0-1.0"),
    (
        r"this is free and unencumbered software released into the public domain|unlicense",
        "Unlicense",
    ),
    (r"apache license,\s*version 2\.0", "Apache-2.0"),
    (r"gnu lesser general public license.*version 3", "LGPL-3.0-only"),
    (r"gnu general public license.*version 3", "GPL-3.0-only"),
    (r"gnu general public license.*version 2", "GPL-2.0-only"),
    (r"mozilla public license version 2\.0", "MPL-2.0"),
    (r"permission is hereby granted, free of charge, to any person obtaining a copy", "MIT"),
    (
        r"neither the name of the copyright holder nor the names of its contributors",
        "BSD-3-Clause",
    ),
    (
        r"redistribution and use in source and binary forms, with or without modification",
        "BSD-2-Clause",
    ),
    (r"permission to use, copy, modify, and/or distribute this software for any purpose", "ISC"),
)


def detect_spdx_license(spdx_hint: str | None = None, license_text: str | None = None) -> str:
    """Resolve an SPDX license identifier from repository metadata and license file text."""
    if isinstance(spdx_hint, str):
        cleaned = spdx_hint.strip()
        if cleaned and cleaned.upper() not in ("NOASSERTION", "NONE", "OTHER"):
            if cleaned in _SPDX_TABLE:
                return cleaned
            alias = _HINT_ALIASES.get(cleaned.lower())
            if alias:
                return alias
    if isinstance(license_text, str) and license_text.strip():
        compact = " ".join(license_text.lower().split())
        for pattern, spdx_id in _TEXT_PATTERNS:
            if re.search(pattern, compact):
                return spdx_id
    return "UNKNOWN"


def evaluate_license_gate(
    spdx_id: str | None = None,
    license_text: str | None = None,
    *,
    allow_non_commercial: bool = False,
    allow_copyleft: bool = True,
    allow_unknown: bool = False,
) -> dict[str, Any]:
    """Classify license compliance and determine whether automated ingestion is permitted."""
    resolved = detect_spdx_license(spdx_id, license_text)
    meta = _SPDX_TABLE.get(resolved, _SPDX_TABLE["UNKNOWN"])
    category = meta["category"]
    flags: list[str] = []
    blocking: list[str] = []

    if meta["requires_attribution"]:
        flags.append("attribution_required")
    if meta["copyleft"]:
        flags.append("copyleft_share_alike")
        if not allow_copyleft:
            blocking.append(
                f"Copyleft license {resolved} is disabled by policy (allow_copyleft=False)."
            )
    if not meta["commercial_ok"] and category == "non_commercial":
        flags.append("non_commercial_restricted")
        if not allow_non_commercial:
            blocking.append(
                f"License {resolved} restricts commercial use; explicit user confirmation required."
            )
    if not meta["derivatives_ok"] and category == "no_derivatives":
        flags.append("no_derivatives_restricted")
        blocking.append(
            f"License {resolved} forbids distributing derivatives; cannot redistribute adaptations."
        )
    if resolved == "UNKNOWN":
        flags.append("missing_or_unrecognized_license")
        if not allow_unknown:
            blocking.append(
                "Repository license missing/unrecognized; override required before ingest."
            )

    return {
        "spdx_id": resolved,
        "category": category,
        "allowed": not blocking,
        "requires_attribution": bool(meta["requires_attribution"]),
        "commercial_ok": bool(meta["commercial_ok"]),
        "copyleft": bool(meta["copyleft"]),
        "derivatives_ok": bool(meta["derivatives_ok"]),
        "flags": flags,
        "blocking_reasons": blocking,
        "license_url": meta["url"],
        "summary": meta["summary"],
    }


def validate_upstream_url(url: str, *, for_download: bool = True) -> str:
    """Enforce HTTPS and a strict host allowlist to block SSRF and local network access."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Upstream URL must be a non-empty string.")
    raw = url.strip()
    if len(raw) > 2048 or any(ch.isspace() for ch in raw):
        raise ValueError("Invalid whitespace or length in upstream URL.")
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Only HTTPS upstream URLs are permitted (got {parsed.scheme!r}).")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in upstream URLs are forbidden.")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("Upstream URL is missing a host.")
    if parsed.port not in (None, 443):
        raise ValueError(f"Non-standard port {parsed.port} is forbidden.")
    try:
        ipaddress.ip_address(host)
        raise ValueError("Direct IP address URLs are forbidden.")
    except ValueError as exc:
        if "Direct IP address" in str(exc):
            raise
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError(f"Local or internal host {host!r} is forbidden.")
    allowed = (
        ALLOWED_DOWNLOAD_HOSTS if for_download else (ALLOWED_API_HOSTS | ALLOWED_DOWNLOAD_HOSTS)
    )
    if host not in allowed:
        raise ValueError(f"Upstream host {host!r} is outside the reviewed allowlist.")
    return raw


def validate_repo_file_path(
    path: str,
    *,
    allow_cad: bool = True,
    allow_docs: bool = True,
) -> str:
    """Validate a repository-relative POSIX path and block traversal or executables."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Repository file path must be non-empty.")
    cleaned = path.strip()
    if "\x00" in cleaned or "\\" in cleaned or cleaned.startswith("/"):
        raise ValueError(f"Unsafe repository path: {path!r}")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    posix = PurePosixPath(cleaned)
    parts = posix.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"Path traversal or empty segment in path: {path!r}")
    ext = posix.suffix.lower()
    if ext in FORBIDDEN_EXTENSIONS:
        raise ValueError(f"Forbidden binary or executable extension {ext!r} in {path!r}.")
    allowed_exts: set[str] = set()
    if allow_cad:
        allowed_exts.update(ALLOWED_CAD_EXTENSIONS)
    if allow_docs:
        allowed_exts.update(ALLOWED_DOC_EXTENSIONS)
    base_upper = posix.name.upper()
    if not ext and base_upper in {"LICENSE", "LICENCE", "NOTICE", "COPYING", "README"}:
        if allow_docs:
            return posix.as_posix()
    if ext not in allowed_exts:
        raise ValueError(f"Unsupported or disallowed extension {ext!r} for {path!r}.")
    return posix.as_posix()


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest for raw bytes."""
    return hashlib.sha256(data).hexdigest()


def build_provenance_record(
    *,
    source_host: str,
    repo_full_name: str,
    repo_url: str,
    commit_sha: str,
    file_path: str,
    raw_url: str,
    file_sha256: str,
    file_bytes: int,
    license_gate: dict[str, Any],
    authors: str | None = None,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    """Build an immutable commit- and hash-pinned provenance record."""
    return {
        "source_host": source_host,
        "repo_full_name": repo_full_name,
        "repo_url": validate_upstream_url(repo_url, for_download=False),
        "commit_sha": str(commit_sha),
        "file_path": validate_repo_file_path(file_path, allow_cad=True, allow_docs=True),
        "raw_url": validate_upstream_url(raw_url, for_download=True),
        "file_sha256": str(file_sha256),
        "file_bytes": int(file_bytes),
        "retrieved_at": retrieved_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "spdx_id": license_gate["spdx_id"],
        "license_category": license_gate["category"],
        "license_url": license_gate.get("license_url"),
        "license_flags": list(license_gate.get("flags") or []),
        "requires_attribution": bool(license_gate.get("requires_attribution", True)),
        "commercial_ok": bool(license_gate.get("commercial_ok", False)),
        "copyleft": bool(license_gate.get("copyleft", False)),
        "authors": authors or repo_full_name.split("/")[0],
    }


def render_third_party_notices(
    upstream_project: dict[str, Any], *, original_path: str = "upstream/source/",
    adaptation_path: str = "custom/",
) -> str:
    """Render THIRD_PARTY_NOTICES.md separating upstream source from AutoLab adaptations."""
    prov = upstream_project.get("provenance") or {}
    gate = upstream_project.get("license_gate") or {}
    adaptation = upstream_project.get("adaptation")
    lines = [
        "# Third-Party Open-Source Hardware Notices",
        "",
        "This build package incorporates geometry and/or reference documentation from an external",
        "open-source project. Upstream files remain governed by their original license and are",
        "preserved separately from any AutoLab custom adaptations.",
        "",
        "## Upstream Project Provenance",
        "",
        f"- **Repository:** `{prov.get('repo_full_name', 'unknown')}` "
        f"({prov.get('repo_url', 'n/a')})",
        f"- **Authors / Maintainer:** {prov.get('authors', 'Not listed')}",
        f"- **Pinned Commit SHA:** `{prov.get('commit_sha', 'unpinned')}`",
        f"- **Original CAD File:** `{prov.get('file_path', 'n/a')}`",
        f"- **Raw Source URL:** {prov.get('raw_url', 'n/a')}",
        f"- **Original File SHA-256:** `{prov.get('file_sha256', 'n/a')}`",
        f"- **File Size (bytes):** `{prov.get('file_bytes', 0)}`",
        f"- **Retrieved At (UTC):** `{prov.get('retrieved_at', 'n/a')}`",
        "",
        "## License & Redistribution Terms",
        "",
        f"- **Detected SPDX License:** `{prov.get('spdx_id', gate.get('spdx_id', 'UNKNOWN'))}`",
        f"- **Category:** `{prov.get('license_category', gate.get('category', 'unknown'))}`",
        f"- **License Text URL:** {prov.get('license_url') or 'Not specified'}",
        f"- **Summary:** {gate.get('summary', 'Verify license obligations.')}",
    ]
    flags = prov.get("license_flags") or gate.get("flags") or []
    if flags:
        lines.append(f"- **Policy Flags:** {', '.join(f'`{f}`' for f in flags)}")
    if not prov.get("commercial_ok", False):
        lines.append(
            "- **WARNING — Commercial Restriction:** Upstream license is NC or unverified."
        )
    if prov.get("copyleft", False):
        lines.append(
            "- **Obligation — Reciprocal/ShareAlike:** Redistributions with derivative parts must "
            "comply with the upstream copyleft / open-hardware source-sharing obligations."
        )

    lines.extend(
        [
            "",
            "## Boundary Between Unmodified Source and AutoLab Adaptations",
            "",
            f"- **Unmodified upstream source:** Saved in `{original_path}` with matching SHA-256.",
        ]
    )
    if adaptation:
        mode = adaptation.get("mode", "mating")
        lines.extend(
            [
                f"- **AutoLab adaptation mode:** `{mode}`",
                f"- **Custom artifacts:** Stored separately under `{adaptation_path}` "
                "(non-destructive",
                "  mating parts or explicit parameter overrides from the pinned revision).",
                "- **Verification scope:** Modeled digital clearance/interference check only;",
                "  physical fit, load capacity, wiring, and hardware operation remain unverified.",
            ]
        )
    else:
        lines.append("- **Custom modifications:** None applied in this project revision.")
    lines.append("")
    return "\n".join(lines)
