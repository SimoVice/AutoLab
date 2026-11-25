"""Verified upstream CAD download, disk caching, and sandboxed geometry normalization."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.cad_worker import (
    MAX_WORKER_RSS_BYTES,
    POLL_SECONDS,
    _children,
    _children_lock,
    _process_tree_rss_bytes,
    _stop_process,
)
from app.chat_activity import activity_scope
from app.open_hardware_license import (
    build_provenance_record,
    evaluate_license_gate,
    sha256_bytes,
    validate_repo_file_path,
    validate_upstream_url,
)

MAX_CAD_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_TEXT_DOWNLOAD_BYTES = 2 * 1024 * 1024
DEFAULT_UPSTREAM_WORKER_TIMEOUT = float(
    os.environ.get("AUTOLAB_UPSTREAM_TIMEOUT_SECONDS", "60")
)


class UpstreamCadError(RuntimeError):
    """Raised when downloading, inspecting, or normalizing upstream CAD fails."""


def upstream_cache_dir() -> Path:
    """Return the writable content-addressed disk cache for verified upstream artifacts."""
    env = os.environ.get("AUTOLAB_UPSTREAM_CACHE_DIR", "").strip()
    base = Path(env) if env else (Path(__file__).resolve().parents[1] / ".local" / "upstream-cache")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _safe_filename(name: str) -> str:
    clean = Path(PurePosixPath(name).name).name
    return clean if clean and clean not in (".", "..") else "source.bin"


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = tempfile.NamedTemporaryFile(dir=target.parent, delete=False)
    tmp = Path(staged.name)
    try:
        with staged:
            staged.write(data)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def cache_source_bytes(filename: str, raw: bytes) -> dict[str, Any]:
    """Store validated upstream bytes content-addressed by SHA-256."""
    digest = sha256_bytes(raw)
    safe_name = _safe_filename(filename)
    path = upstream_cache_dir() / "sources" / digest / safe_name
    if not path.is_file() or path.stat().st_size != len(raw):
        _atomic_write(path, raw)
    return {
        "file_sha256": digest,
        "file_bytes": len(raw),
        "local_path": str(path),
    }


async def download_verified_asset(
    url: str,
    *,
    expected_sha256: str | None = None,
    max_bytes: int = MAX_CAD_DOWNLOAD_BYTES,
    timeout_seconds: float = 20.0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Download one allowlisted HTTPS asset with redirect validation, cap, and SHA-256 pin."""
    validate_upstream_url(url, for_download=True)
    headers = {"User-Agent": "AutoLab-OpenHardware-Discovery/0.1"}

    async def _fetch(active: httpx.AsyncClient) -> tuple[str, bytes]:
        current = url
        for _ in range(5):
            async with asyncio.timeout(timeout_seconds):
                async with active.stream(
                    "GET", current, headers=headers, follow_redirects=False
                ) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location", "")
                        if not loc:
                            raise UpstreamCadError(f"Empty redirect location from {current}.")
                        nxt = urljoin(current, loc)
                        validate_upstream_url(nxt, for_download=True)
                        current = nxt
                        continue
                    if resp.status_code != 200:
                        raise UpstreamCadError(
                            f"Download failed with HTTP {resp.status_code} from upstream host."
                        )
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes(65536):
                        if len(buf) + len(chunk) > max_bytes:
                            limit_mb = max_bytes // (1024 * 1024)
                            raise UpstreamCadError(
                                f"Upstream asset exceeded the {limit_mb} MB limit."
                            )
                        buf.extend(chunk)
                    return current, bytes(buf)
        raise UpstreamCadError("Too many redirects while downloading upstream asset.")

    with activity_scope("researching"):
        if client is not None:
            final_url, raw = await _fetch(client)
        else:
            async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as active:
                final_url, raw = await _fetch(active)

    if not raw:
        raise UpstreamCadError("Downloaded upstream asset is empty.")
    digest = sha256_bytes(raw)
    if expected_sha256 and digest.lower() != expected_sha256.lower().strip():
        raise UpstreamCadError("Downloaded upstream asset SHA-256 does not match pinned digest.")

    filename = urlparse(final_url).path.rsplit("/", 1)[-1] or "upstream.bin"
    stored = cache_source_bytes(filename, raw)
    return {
        "raw_url": final_url,
        **stored,
    }


def _upstream_worker_script() -> Path:
    return Path(__file__).resolve().with_name("upstream_cad_worker.py")


async def run_upstream_worker(
    mode: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
    max_rss_bytes: int | None = None,
) -> dict[str, Any]:
    """Run app.upstream_cad_worker in an isolated child process with RSS & timeout caps."""
    limit_seconds = float(
        DEFAULT_UPSTREAM_WORKER_TIMEOUT if timeout_seconds is None else timeout_seconds
    )
    rss_limit = int(MAX_WORKER_RSS_BYTES if max_rss_bytes is None else max_rss_bytes)
    worker_script = _upstream_worker_script()

    with TemporaryDirectory(prefix="autolab-upstream-worker-") as tmp:
        root = Path(tmp)
        in_path = root / "input.json"
        out_path = root / "output.json"
        await asyncio.to_thread(in_path.write_text, json.dumps(payload), "utf-8")

        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(worker_script),
                "--mode",
                mode,
                "--input",
                str(in_path),
                "--output",
                str(out_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            cwd=Path(__file__).resolve().parents[1],
            **options,
        )
        with _children_lock:
            _children[process.pid] = process

        try:
            deadline = time.monotonic() + limit_seconds
            while True:
                code = process.poll()
                if code is not None:
                    break
                rss = _process_tree_rss_bytes(process.pid)
                if rss is not None and rss > rss_limit:
                    await asyncio.to_thread(_stop_process, process)
                    rss_mb = rss_limit // (1024 * 1024)
                    raise UpstreamCadError(
                        f"Upstream CAD worker exceeded RSS ceiling ({rss_mb} MB)."
                    )
                if time.monotonic() >= deadline:
                    await asyncio.to_thread(_stop_process, process)
                    raise UpstreamCadError("Upstream CAD worker timed out.")
                await asyncio.sleep(POLL_SECONDS)
        except BaseException:
            if process.poll() is None:
                await asyncio.to_thread(_stop_process, process)
            raise
        finally:
            with _children_lock:
                _children.pop(process.pid, None)

        if not out_path.is_file():
            raise UpstreamCadError("Upstream CAD worker finished without writing an output report.")
        data = json.loads(out_path.read_text(encoding="utf-8"))
        if code != 0 or not data.get("ok"):
            detail = data.get("detail") or data.get("error_type") or "Worker error"
            raise UpstreamCadError(f"Upstream CAD {mode} failed: {detail}")
        return data


def resolve_raw_file_url(
    source_host: str,
    repo_full_name: str,
    commit_sha: str,
    file_path: str,
) -> str:
    """Construct a canonical immutable commit-pinned HTTPS raw URL for an allowed host."""
    safe_path = validate_repo_file_path(file_path, allow_cad=True, allow_docs=True)
    host = source_host.lower().strip()
    if host == "github":
        url = f"https://raw.githubusercontent.com/{repo_full_name}/{commit_sha}/{safe_path}"
    elif host == "gitlab":
        url = f"https://gitlab.com/{repo_full_name}/-/raw/{commit_sha}/{safe_path}"
    else:
        raise ValueError(f"Unsupported source host for raw file URL construction: {source_host!r}")
    return validate_upstream_url(url, for_download=True)


async def ingest_upstream_cad(
    *,
    artifact_dir: Path | None = None,
    source_host: str,
    repo_full_name: str,
    repo_url: str,
    commit_sha: str,
    cad_file_path: str,
    spdx_hint: str | None = None,
    license_text: str | None = None,
    raw_url: str | None = None,
    source_units: str = "auto",
    source_up_axis: str = "z",
    allow_non_commercial: bool = False,
    allow_copyleft: bool = True,
    allow_unknown_license: bool = False,
    expected_sha256: str | None = None,
    local_bytes: bytes | None = None,
    authors: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Gate license, download commit-pinned CAD, normalize/inspect in worker, return state."""
    safe_path = validate_repo_file_path(cad_file_path, allow_cad=True, allow_docs=False)
    gate = evaluate_license_gate(
        spdx_hint,
        license_text,
        allow_non_commercial=allow_non_commercial,
        allow_copyleft=allow_copyleft,
        allow_unknown=allow_unknown_license,
    )
    if not gate["allowed"]:
        raise PermissionError("; ".join(gate["blocking_reasons"]))

    pinned_raw_url = (
        validate_upstream_url(raw_url, for_download=True)
        if raw_url
        else resolve_raw_file_url(source_host, repo_full_name, commit_sha, safe_path)
    )
    if local_bytes is not None:
        if len(local_bytes) == 0:
            raise UpstreamCadError("Supplied local CAD bytes are empty.")
        digest = sha256_bytes(local_bytes)
        if expected_sha256 and digest.lower() != expected_sha256.lower().strip():
            raise UpstreamCadError("Supplied CAD bytes do not match pinned SHA-256.")
        dl = {
            "raw_url": pinned_raw_url,
            **cache_source_bytes(PurePosixPath(safe_path).name, local_bytes),
        }
    else:
        dl = await download_verified_asset(
            pinned_raw_url,
            expected_sha256=expected_sha256,
            client=client,
        )

    artifact_dir = artifact_dir or upstream_cache_dir() / "inspected" / dl["file_sha256"]
    worker_out = await run_upstream_worker(
        "inspect",
        {
            "source_path": dl["local_path"],
            "artifact_dir": str(artifact_dir),
            "source_units": source_units,
            "source_up_axis": source_up_axis,
        },
    )
    prov = build_provenance_record(
        source_host=source_host,
        repo_full_name=repo_full_name,
        repo_url=repo_url,
        commit_sha=commit_sha,
        file_path=safe_path,
        raw_url=dl["raw_url"],
        file_sha256=dl["file_sha256"],
        file_bytes=dl["file_bytes"],
        license_gate=gate,
        authors=authors,
    )
    return {
        "provenance": prov,
        "license_gate": gate,
        "geometry": worker_out["geometry"],
        "artifacts": {
            "source_path": dl["local_path"],
            **worker_out["artifacts"],
        },
    }
