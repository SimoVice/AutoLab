"""Bounded read-only online discovery for open-source laboratory hardware projects & CAD."""

from __future__ import annotations

import asyncio
import json
import os
import re
from time import monotonic
from typing import Any
from urllib.parse import quote

import httpx

from app.chat_activity import activity_scope
from app.open_hardware_license import (
    ALLOWED_CAD_EXTENSIONS,
    FORBIDDEN_EXTENSIONS,
    evaluate_license_gate,
    validate_repo_file_path,
    validate_upstream_url,
)

MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_LICENSE_FETCH_BYTES = 64 * 1024
DISCOVERY_TIMEOUT_SECONDS = 10.0
CACHE_TTL_SECONDS = 600.0
DEFAULT_SOURCES = ("github", "gitlab", "openalex", "europepmc", "nih3d")

_discovery_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_candidate_cache: dict[tuple[str, str, str], dict[str, Any]] = {}


def lookup_discovered_candidate(
    source_host: str, repo_full_name: str, commit_sha: str
) -> dict[str, Any] | None:
    """Return cached tree/license metadata for a previously discovered commit-pinned repo."""
    return _candidate_cache.get(
        (source_host.lower().strip(), repo_full_name.lower().strip(), commit_sha.strip())
    )
_REPO_URL_RE = re.compile(
    r"https://(?:github\.com|gitlab\.com)/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_CAD_SORT_PRIORITY = {".step": 0, ".stp": 0, ".scad": 1, ".py": 2, ".stl": 3}
_BOM_KEYWORDS = ("bom", "bill_of_materials", "bill-of-materials", "parts", "hardware", "shopping")
_ASSEMBLY_KEYWORDS = ("assembly", "build", "guide", "manual", "instruction", "readme")
_SOFTWARE_EXTS = {".py", ".ino", ".cfg", ".h", ".c", ".cpp", ".toml", ".json", ".yaml", ".yml"}
_SOFTWARE_DIRS = (
    "firmware",
    "software",
    "src",
    "klipper",
    "marlin",
    "grbl",
    "controller",
    "python",
)


def clear_discovery_cache() -> None:
    """Clear the bounded discovery caches."""
    _discovery_cache.clear()
    _candidate_cache.clear()


def _headers(accept: str = "application/json", *, is_github: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": "AutoLab-OpenHardware-Discovery/0.1",
        "Accept": accept,
    }
    if is_github:
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_text_bounded(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int = MAX_API_RESPONSE_BYTES,
    accept: str = "application/json",
    is_github: bool = False,
) -> tuple[int, str, dict[str, str]]:
    """Perform one bounded HTTPS GET, validating URL and every redirect hop."""
    validate_upstream_url(url, for_download=False)
    current = url
    for _ in range(4):
        async with asyncio.timeout(DISCOVERY_TIMEOUT_SECONDS):
            async with client.stream(
                "GET",
                current,
                headers=_headers(accept, is_github=is_github),
                follow_redirects=False,
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    loc = response.headers.get("location", "")
                    if not loc:
                        return response.status_code, "", dict(response.headers)
                    validate_upstream_url(loc, for_download=False)
                    current = loc
                    continue
                buf = bytearray()
                async for chunk in response.aiter_bytes(65536):
                    if len(buf) + len(chunk) > max_bytes:
                        raise ValueError("Upstream API response exceeded size ceiling.")
                    buf.extend(chunk)
                return (
                    response.status_code,
                    buf.decode("utf-8", errors="replace"),
                    {k.lower(): v for k, v in response.headers.items()},
                )
    raise ValueError("Too many redirects from upstream API host.")


async def _fetch_json_bounded(
    client: httpx.AsyncClient,
    url: str,
    *,
    accept: str = "application/json",
    is_github: bool = False,
) -> tuple[int, Any]:
    status, text, _ = await _fetch_text_bounded(client, url, accept=accept, is_github=is_github)
    if status != 200:
        return status, None
    try:
        return status, json.loads(text)
    except ValueError:
        return status, None


def _is_parametric_cad_py(lower_path: str) -> bool:
    name = lower_path.rsplit("/", 1)[-1]
    if name.startswith("test_") or "/tests/" in lower_path or name in {"setup.py", "__init__.py"}:
        return False
    return any(
        token in lower_path
        for token in (
            "cad",
            "build123d",
            "cadquery",
            "scad",
            "frame",
            "model",
            "geometry",
            "enclosure",
            "holder",
            "mount",
            "pump",
            "stage",
            "part",
        )
    )


def _classify_repo_paths(paths: list[str]) -> dict[str, list[str]]:
    cad_files: list[str] = []
    bom_files: list[str] = []
    assembly_files: list[str] = []
    software_files: list[str] = []
    license_files: list[str] = []

    for raw_path in paths:
        if not isinstance(raw_path, str):
            continue
        lower = raw_path.lower()
        name = lower.rsplit("/", 1)[-1]
        if any(lower.endswith(ext) for ext in FORBIDDEN_EXTENSIONS):
            continue
        try:
            safe = validate_repo_file_path(raw_path, allow_cad=True, allow_docs=True)
        except ValueError:
            continue
        ext = f".{name.rsplit('.', 1)[-1]}" if "." in name else ""

        if name.startswith(("license", "licence", "copying", "notice")):
            license_files.append(safe)
            continue

        if ext in ALLOWED_CAD_EXTENSIONS and (ext != ".py" or _is_parametric_cad_py(lower)):
            cad_files.append(safe)

        if ext in {".csv", ".tsv", ".yaml", ".yml", ".json", ".md"} and any(
            kw in name or f"/{kw}/" in lower for kw in _BOM_KEYWORDS
        ):
            bom_files.append(safe)

        if ext in {".md", ".markdown", ".rst", ".txt"} and any(
            kw in lower for kw in _ASSEMBLY_KEYWORDS
        ):
            assembly_files.append(safe)

        if ext in _SOFTWARE_EXTS and (
            any(dir_kw in lower for dir_kw in _SOFTWARE_DIRS)
            or ext in {".ino", ".c", ".cpp", ".h", ".py"}
        ):
            software_files.append(safe)

    cad_files.sort(key=lambda p: (_CAD_SORT_PRIORITY.get(f".{p.rsplit('.', 1)[-1].lower()}", 9), p))
    return {
        "cad_files": cad_files[:40],
        "bom_files": bom_files[:20],
        "assembly_files": assembly_files[:20],
        "software_files": software_files[:30],
        "license_files": license_files[:5],
    }


def _tokens(text: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[a-z0-9]{2,}", text.lower())
        if tok not in {"open", "source"}
    }


def _score_candidate(query: str, cand: dict[str, Any]) -> tuple[int, dict[str, int]]:
    q_tokens = _tokens(query)
    hay = " ".join(
        [
            cand.get("title", ""),
            cand.get("repo_full_name", ""),
            cand.get("description", ""),
            " ".join(cand.get("topics") or []),
        ]
    )
    h_tokens = _tokens(hay)
    overlap = len(q_tokens & h_tokens) / max(len(q_tokens), 1) if q_tokens else 0.5
    relevance = round(35 * overlap)

    cad = cand.get("cad_files") or []
    cad_exts = {f".{p.rsplit('.', 1)[-1].lower()}" for p in cad if "." in p}
    if ".step" in cad_exts or ".stp" in cad_exts:
        cad_score = 30
    elif ".scad" in cad_exts or ".py" in cad_exts:
        cad_score = 22
    elif cad:
        cad_score = 15
    else:
        cad_score = 0

    docs_score = 0
    if cand.get("bom_files"):
        docs_score += 8
    if cand.get("assembly_files"):
        docs_score += 7

    gate = cand.get("license_gate") or {}
    if gate.get("category") == "permissive":
        lic_score = 12
    elif gate.get("category") == "copyleft":
        lic_score = 9
    elif gate.get("allowed"):
        lic_score = 5
    else:
        lic_score = 0

    pub_bonus = 4 if cand.get("publication_references") else 0
    stars = int(cand.get("stars") or 0)
    community = 4 if stars >= 50 else 2 if stars >= 10 else 1 if stars >= 1 else 0
    maturity = min(8, pub_bonus + community)

    breakdown = {
        "relevance": relevance,
        "cad_completeness": cad_score,
        "documentation_bom": docs_score,
        "license_openness": lic_score,
        "maturity_fabrication": maturity,
    }
    return sum(breakdown.values()), breakdown


def rank_discovery_candidates(query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate cross-source hits and sort candidates deterministically (highest score first)."""
    by_key: dict[str, dict[str, Any]] = {}
    for item in candidates:
        repo = (item.get("repo_full_name") or "").lower().strip()
        key = f"{item.get('source_host', 'other')}:{repo}" if repo else item.get("id", "")
        if key in by_key:
            existing = by_key[key]
            pubs = list(existing.get("publication_references") or [])
            for p in item.get("publication_references") or []:
                if p not in pubs:
                    pubs.append(p)
            existing["publication_references"] = pubs
            if not existing.get("cad_files") and item.get("cad_files"):
                existing["cad_files"] = item["cad_files"]
            continue
        by_key[key] = item

    ranked: list[dict[str, Any]] = []
    for cand in by_key.values():
        score, breakdown = _score_candidate(query, cand)
        cand["score"] = score
        cand["score_breakdown"] = breakdown
        ranked.append(cand)

    ranked.sort(
        key=lambda c: (
            -int(c.get("score", 0)),
            -len(c.get("cad_files") or []),
            -int(c.get("stars") or 0),
            c.get("id", ""),
        )
    )
    return ranked


async def _inspect_github_repo(
    client: httpx.AsyncClient, repo: dict[str, Any]
) -> dict[str, Any] | None:
    full_name = repo.get("full_name")
    branch = repo.get("default_branch") or "main"
    if not isinstance(full_name, str) or "/" not in full_name:
        return None
    status, commit_body = await _fetch_json_bounded(
        client,
        f"https://api.github.com/repos/{full_name}/commits/{quote(branch, safe='')}",
        accept="application/vnd.github+json",
        is_github=True,
    )
    if status != 200 or not isinstance(commit_body, dict) or not commit_body.get("sha"):
        return None
    commit_sha = str(commit_body["sha"])

    t_status, tree_body = await _fetch_json_bounded(
        client,
        f"https://api.github.com/repos/{full_name}/git/trees/{commit_sha}?recursive=1",
        accept="application/vnd.github+json",
        is_github=True,
    )
    tree_paths = []
    if t_status == 200 and isinstance(tree_body, dict):
        tree_paths = [
            e["path"]
            for e in (tree_body.get("tree") or [])
            if isinstance(e, dict) and e.get("type") == "blob" and isinstance(e.get("path"), str)
        ]
    classified = _classify_repo_paths(tree_paths)

    lic_meta = repo.get("license") or {}
    spdx_hint = lic_meta.get("spdx_id") if isinstance(lic_meta, dict) else None
    license_text = ""
    if (not spdx_hint or str(spdx_hint).upper() == "NOASSERTION") and classified["license_files"]:
        lic_path = classified["license_files"][0]
        raw_lic_url = f"https://raw.githubusercontent.com/{full_name}/{commit_sha}/{lic_path}"
        l_status, l_text, _ = await _fetch_text_bounded(
            client, raw_lic_url, max_bytes=MAX_LICENSE_FETCH_BYTES, accept="text/plain"
        )
        if l_status == 200:
            license_text = l_text

    license_gate = evaluate_license_gate(spdx_hint, license_text)
    html_url = repo.get("html_url") or f"https://github.com/{full_name}"
    return {
        "id": f"github:{full_name}",
        "kind": "repository",
        "source_host": "github",
        "repo_full_name": full_name,
        "title": full_name,
        "description": str(repo.get("description") or "")[:400],
        "repo_url": html_url,
        "default_branch": branch,
        "commit_sha": commit_sha,
        "stars": int(repo.get("stargazers_count") or 0),
        "topics": list(repo.get("topics") or [])[:12],
        "spdx_id": license_gate["spdx_id"],
        "license_gate": license_gate,
        "raw_url_template": f"https://raw.githubusercontent.com/{full_name}/{commit_sha}/{{path}}",
        "publication_references": [],
        **classified,
    }


async def search_github_open_hardware(
    query: str,
    *,
    max_candidates: int = 4,
    client: httpx.AsyncClient,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Search public GitHub repositories and inspect commit-pinned trees for CAD & BOMs."""
    q = f"{query.strip()} open hardware OR step OR stl OR scad".strip()
    url = (
        f"https://api.github.com/search/repositories?q={quote(q)}"
        f"&sort=stars&order=desc&per_page={min(max(max_candidates * 2, 5), 10)}"
    )
    try:
        status, data = await _fetch_json_bounded(
            client, url, accept="application/vnd.github+json", is_github=True
        )
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        warnings.append(f"GitHub search unavailable: {exc}")
        return []
    if status == 403 or status == 429:
        warnings.append("GitHub search rate-limited; retry later or set GITHUB_TOKEN.")
        return []
    if status != 200 or not isinstance(data, dict):
        warnings.append(f"GitHub search returned HTTP {status}.")
        return []

    items = [r for r in (data.get("items") or []) if isinstance(r, dict)][:max_candidates]
    results = await asyncio.gather(
        *(_inspect_github_repo(client, repo) for repo in items), return_exceptions=True
    )
    out: list[dict[str, Any]] = []
    for res in results:
        if isinstance(res, dict):
            out.append(res)
    return out


async def _inspect_gitlab_project(
    client: httpx.AsyncClient, proj: dict[str, Any]
) -> dict[str, Any] | None:
    full_name = proj.get("path_with_namespace")
    proj_id = proj.get("id")
    branch = proj.get("default_branch") or "main"
    if not isinstance(full_name, str) or proj_id is None:
        return None
    enc_id = quote(str(proj_id), safe="")
    c_status, commit_body = await _fetch_json_bounded(
        client,
        f"https://gitlab.com/api/v4/projects/{enc_id}/repository/commits/{quote(branch, safe='')}",
    )
    if c_status != 200 or not isinstance(commit_body, dict) or not commit_body.get("id"):
        return None
    commit_sha = str(commit_body["id"])

    t_status, tree_body = await _fetch_json_bounded(
        client,
        f"https://gitlab.com/api/v4/projects/{enc_id}/repository/tree"
        f"?ref={commit_sha}&recursive=true&per_page=100",
    )
    tree_paths = []
    if t_status == 200 and isinstance(tree_body, list):
        tree_paths = [
            e["path"]
            for e in tree_body
            if isinstance(e, dict) and e.get("type") == "blob" and isinstance(e.get("path"), str)
        ]
    classified = _classify_repo_paths(tree_paths)
    license_obj = proj.get("license") or {}
    spdx_hint = license_obj.get("key") if isinstance(license_obj, dict) else None
    license_text = ""
    if (not spdx_hint or str(spdx_hint).lower() == "other") and classified["license_files"]:
        raw_lic = f"https://gitlab.com/{full_name}/-/raw/{commit_sha}/{classified['license_files'][0]}"
        l_status, l_text, _ = await _fetch_text_bounded(
            client, raw_lic, max_bytes=MAX_LICENSE_FETCH_BYTES, accept="text/plain"
        )
        if l_status == 200:
            license_text = l_text

    license_gate = evaluate_license_gate(spdx_hint, license_text)
    web_url = proj.get("web_url") or f"https://gitlab.com/{full_name}"
    return {
        "id": f"gitlab:{full_name}",
        "kind": "repository",
        "source_host": "gitlab",
        "repo_full_name": full_name,
        "title": proj.get("name_with_namespace") or full_name,
        "description": str(proj.get("description") or "")[:400],
        "repo_url": web_url,
        "default_branch": branch,
        "commit_sha": commit_sha,
        "stars": int(proj.get("star_count") or 0),
        "topics": list(proj.get("topics") or [])[:12],
        "spdx_id": license_gate["spdx_id"],
        "license_gate": license_gate,
        "raw_url_template": f"https://gitlab.com/{full_name}/-/raw/{commit_sha}/{{path}}",
        "publication_references": [],
        **classified,
    }


async def search_gitlab_open_hardware(
    query: str,
    *,
    max_candidates: int = 3,
    client: httpx.AsyncClient,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Search public GitLab projects and inspect repository trees."""
    url = (
        f"https://gitlab.com/api/v4/projects?search={quote(query.strip())}"
        f"&visibility=public&order_by=star_count&per_page={min(max_candidates * 2, 10)}"
    )
    try:
        status, data = await _fetch_json_bounded(client, url)
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        warnings.append(f"GitLab search unavailable: {exc}")
        return []
    if status != 200 or not isinstance(data, list):
        warnings.append(f"GitLab search returned HTTP {status}.")
        return []
    projects = [p for p in data if isinstance(p, dict)][:max_candidates]
    results = await asyncio.gather(
        *(_inspect_gitlab_project(client, p) for p in projects), return_exceptions=True
    )
    return [r for r in results if isinstance(r, dict)]


async def search_openalex_hardware_literature(
    query: str,
    *,
    max_results: int = 5,
    client: httpx.AsyncClient,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Query OpenAlex for open-hardware scientific articles and linked source repositories."""
    q = f"{query.strip()} open hardware laboratory".strip()
    url = (
        f"https://api.openalex.org/works?search={quote(q)}&filter=is_oa:true"
        f"&per-page={min(max_results, 8)}"
    )
    try:
        status, data = await _fetch_json_bounded(client, url)
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        warnings.append(f"OpenAlex search unavailable: {exc}")
        return []
    if status != 200 or not isinstance(data, dict):
        warnings.append(f"OpenAlex search returned HTTP {status}.")
        return []
    refs: list[dict[str, Any]] = []
    for work in (data.get("results") or [])[:max_results]:
        if not isinstance(work, dict):
            continue
        title = str(work.get("display_name") or work.get("title") or "")[:300]
        doi = work.get("doi") or ""
        primary = work.get("primary_location") or {}
        landing = primary.get("landing_page_url") if isinstance(primary, dict) else ""
        lic = primary.get("license") if isinstance(primary, dict) else None
        text_blob = f"{title} {doi} {landing or ''}"
        repos = _REPO_URL_RE.findall(text_blob)
        refs.append(
            {
                "id": f"openalex:{work.get('id', doi or title)}",
                "kind": "literature",
                "source_host": "openalex",
                "title": title,
                "year": work.get("publication_year"),
                "doi": doi,
                "url": doi or landing or work.get("id"),
                "license": lic,
                "linked_repos": list(dict.fromkeys(repos)),
            }
        )
    return refs


async def search_europepmc_open_hardware(
    query: str,
    *,
    max_results: int = 5,
    client: httpx.AsyncClient,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Query Europe PMC open-access literature for open-hardware designs."""
    q = f"({query.strip()}) AND (open hardware OR HardwareX OR 3D printed) AND OPEN_ACCESS:y"
    url = (
        f"https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        f"?query={quote(q)}&format=json&resultType=core&pageSize={min(max_results, 8)}"
    )
    try:
        status, data = await _fetch_json_bounded(client, url)
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        warnings.append(f"Europe PMC search unavailable: {exc}")
        return []
    if status != 200 or not isinstance(data, dict):
        warnings.append(f"Europe PMC search returned HTTP {status}.")
        return []
    entries = ((data.get("resultList") or {}).get("result") or [])[:max_results]
    refs: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "")[:300]
        abstract = str(entry.get("abstractText") or "")[:600]
        doi = entry.get("doi")
        pmcid = entry.get("pmcid")
        repos = _REPO_URL_RE.findall(f"{title} {abstract}")
        refs.append(
            {
                "id": f"europepmc:{pmcid or entry.get('id') or title}",
                "kind": "literature",
                "source_host": "europepmc",
                "title": title,
                "year": entry.get("pubYear"),
                "doi": f"https://doi.org/{doi}" if doi else None,
                "pmcid": pmcid,
                "url": f"https://europepmc.org/article/PMC/{pmcid}" if pmcid else (
                    f"https://doi.org/{doi}" if doi else None
                ),
                "linked_repos": list(dict.fromkeys(repos)),
            }
        )
    return refs


async def search_nih3d_models(
    query: str,
    *,
    max_results: int = 4,
    client: httpx.AsyncClient,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Bounded read-only lookup against NIH 3D public catalog when available."""
    url = (
        f"https://3d.nih.gov/api/entries?search={quote(query.strip())}"
        f"&limit={min(max_results, 6)}"
    )
    try:
        status, data = await _fetch_json_bounded(client, url)
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        warnings.append(f"NIH 3D search unavailable: {exc}")
        return []
    if status != 200 or data is None:
        warnings.append(f"NIH 3D search returned HTTP {status}.")
        return []
    rows = data if isinstance(data, list) else (data.get("results") or data.get("entries") or [])
    out: list[dict[str, Any]] = []
    for row in rows[:max_results]:
        if not isinstance(row, dict):
            continue
        entry_id = str(row.get("id") or row.get("accession") or "").strip()
        title = str(row.get("title") or row.get("name") or "").strip()
        if not entry_id or not title:
            continue
        lic_gate = evaluate_license_gate(row.get("license"), None)
        download_url = row.get("download_url") or row.get("stl_url")
        cad_files = []
        if isinstance(download_url, str) and download_url.lower().endswith(
            (".stl", ".step", ".stp")
        ):
            cad_files = [download_url.rsplit("/", 1)[-1]]
        out.append(
            {
                "id": f"nih3d:{entry_id}",
                "kind": "model",
                "source_host": "nih3d",
                "repo_full_name": f"nih3d/{entry_id}",
                "title": title[:300],
                "description": str(row.get("description") or "")[:400],
                "repo_url": f"https://3d.nih.gov/entries/{quote(entry_id, safe='')}",
                "commit_sha": str(row.get("version") or "v1"),
                "stars": 0,
                "topics": ["nih3d"],
                "spdx_id": lic_gate["spdx_id"],
                "license_gate": lic_gate,
                "raw_url_template": download_url or "",
                "cad_files": cad_files,
                "bom_files": [],
                "assembly_files": [],
                "software_files": [],
                "license_files": [],
                "publication_references": [],
            }
        )
    return out


async def discover_open_hardware(
    query: str,
    *,
    sources: list[str] | None = None,
    max_results: int = 8,
    require_cad: bool = True,
    commercial_use_ok: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Search approved open-hardware indices, rank candidates, and record provenance hints."""
    cleaned_query = (query or "").strip()
    if len(cleaned_query) < 2:
        raise ValueError("Discovery query must contain at least 2 characters.")
    selected = tuple(
        s for s in (sources or DEFAULT_SOURCES) if s in DEFAULT_SOURCES
    ) or DEFAULT_SOURCES
    limit = max(1, min(int(max_results), 20))
    cache_key = (cleaned_query.lower(), selected, limit, bool(require_cad), bool(commercial_use_ok))
    if client is None:
        cached = _discovery_cache.get(cache_key)
        if cached and cached[0] > monotonic():
            return cached[1]

    warnings: list[str] = []

    async def _run(active_client: httpx.AsyncClient) -> dict[str, Any]:
        tasks = []
        if "github" in selected:
            tasks.append(
                search_github_open_hardware(
                    cleaned_query,
                    max_candidates=min(limit, 4),
                    client=active_client,
                    warnings=warnings,
                )
            )
        if "gitlab" in selected:
            tasks.append(
                search_gitlab_open_hardware(
                    cleaned_query,
                    max_candidates=min(limit, 3),
                    client=active_client,
                    warnings=warnings,
                )
            )
        if "openalex" in selected:
            tasks.append(
                search_openalex_hardware_literature(
                    cleaned_query,
                    max_results=min(limit, 5),
                    client=active_client,
                    warnings=warnings,
                )
            )
        if "europepmc" in selected:
            tasks.append(
                search_europepmc_open_hardware(
                    cleaned_query,
                    max_results=min(limit, 5),
                    client=active_client,
                    warnings=warnings,
                )
            )
        if "nih3d" in selected:
            tasks.append(
                search_nih3d_models(
                    cleaned_query,
                    max_results=min(limit, 4),
                    client=active_client,
                    warnings=warnings,
                )
            )
        batches = await asyncio.gather(*tasks)
        repo_candidates: list[dict[str, Any]] = []
        literature_refs: list[dict[str, Any]] = []
        for batch in batches:
            for item in batch:
                if item.get("kind") == "literature":
                    literature_refs.append(item)
                else:
                    repo_candidates.append(item)

        # Attach matching literature references to discovered repositories by owner/repo hint.
        for cand in repo_candidates:
            rname = (cand.get("repo_full_name") or "").lower()
            for ref in literature_refs:
                linked_repos = ref.get("linked_repos") or []
                if rname and any(rname == linked.lower() for linked in linked_repos):
                    cand.setdefault("publication_references", []).append(
                        {"title": ref["title"], "doi": ref.get("doi"), "url": ref.get("url")}
                    )

        filtered: list[dict[str, Any]] = []
        for cand in repo_candidates:
            if require_cad and not cand.get("cad_files"):
                continue
            if commercial_use_ok and not (cand.get("license_gate") or {}).get("commercial_ok"):
                continue
            filtered.append(cand)

        ranked = rank_discovery_candidates(cleaned_query, filtered)[:limit]
        for cand in ranked:
            sha = str(cand.get("commit_sha") or "").strip()
            rname = str(cand.get("repo_full_name") or "").lower().strip()
            shost = str(cand.get("source_host") or "").lower().strip()
            if sha and rname and shost:
                _candidate_cache[(shost, rname, sha)] = cand
        return {
            "query": cleaned_query,
            "sources_queried": list(selected),
            "source_warnings": warnings,
            "candidate_count": len(ranked),
            "candidates": ranked,
            "literature_references": literature_refs[:limit],
        }

    with activity_scope("researching"):
        if client is not None:
            result = await _run(client)
        else:
            async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT_SECONDS, trust_env=False) as c:
                result = await _run(c)
                _discovery_cache[cache_key] = (monotonic() + CACHE_TTL_SECONDS, result)
    return result
