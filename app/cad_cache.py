"""Content-addressed two-tier CAD artifact cache (bounded in-memory LRU + atomic disk cache)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

CACHE_SCHEMA_VERSION = "cad-cache-v1"
DEFAULT_MEMORY_MAX_ENTRIES = 64
DEFAULT_MEMORY_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_DISK_MAX_MB = 512

_LOCK = threading.Lock()
_MEMORY: OrderedDict[str, bytes] = OrderedDict()
_MEMORY_BYTES = 0
_HITS = 0
_MISSES = 0


def _cache_dir() -> Path:
    configured = os.environ.get("AUTOLAB_CAD_CACHE_DIR")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "autolab-cad-cache"


def _disk_max_bytes() -> int:
    raw = os.environ.get("AUTOLAB_CAD_CACHE_MAX_MB")
    try:
        mb = int(raw) if raw else DEFAULT_DISK_MAX_MB
    except ValueError:
        mb = DEFAULT_DISK_MAX_MB
    return max(16, mb) * 1024 * 1024


def cache_key(namespace: str, payload: Any, *, version: str = "1") -> str:
    """Compute a deterministic SHA-256 key namespaced by artifact type and CAD version."""
    safe_ns = "".join(c if c.isalnum() or c in "-_" else "_" for c in namespace)[:48] or "cad"
    canonical = json.dumps(
        {"schema": CACHE_SCHEMA_VERSION, "ns": safe_ns, "v": str(version), "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"{safe_ns}--{digest}"


def _entry_path(key: str) -> Path:
    return _cache_dir() / f"{key}.bin"


def _remember_in_memory(key: str, data: bytes) -> None:
    global _MEMORY_BYTES
    if len(data) > DEFAULT_MEMORY_MAX_BYTES // 2:
        return
    if key in _MEMORY:
        _MEMORY.move_to_end(key)
        return
    _MEMORY[key] = data
    _MEMORY_BYTES += len(data)
    while _MEMORY and (
        _MEMORY_BYTES > DEFAULT_MEMORY_MAX_BYTES or len(_MEMORY) > DEFAULT_MEMORY_MAX_ENTRIES
    ):
        _, evicted = _MEMORY.popitem(last=False)
        _MEMORY_BYTES -= len(evicted)


def _evict_disk_if_needed(folder: Path, limit_bytes: int) -> None:
    try:
        entries = [
            (p.stat().st_mtime, p.stat().st_size, p)
            for p in folder.glob("*.bin")
            if p.is_file()
        ]
    except OSError:
        return
    total = sum(size for _, size, _ in entries)
    if total <= limit_bytes:
        return
    entries.sort(key=lambda item: item[0])
    for _, size, path in entries:
        if total <= limit_bytes * 0.85:
            break
        try:
            path.unlink(missing_ok=True)
            total -= size
        except OSError:
            continue


def get_cached_bytes(key: str) -> bytes | None:
    """Look up bytes in the in-memory LRU first, then in the atomic disk cache."""
    global _HITS, _MISSES
    with _LOCK:
        mem = _MEMORY.get(key)
        if mem is not None:
            _MEMORY.move_to_end(key)
            _HITS += 1
            return mem
    path = _entry_path(key)
    try:
        if path.is_file():
            data = path.read_bytes()
            if data:
                with _LOCK:
                    _remember_in_memory(key, data)
                    _HITS += 1
                return data
    except OSError:
        pass
    with _LOCK:
        _MISSES += 1
    return None


def put_cached_bytes(key: str, data: bytes) -> None:
    """Store bytes in both memory LRU and atomically replaced disk entry."""
    if not data:
        return
    with _LOCK:
        _remember_in_memory(key, data)
    folder = _cache_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".tmp-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
        tmp.write_bytes(data)
        os.replace(tmp, _entry_path(key))
        _evict_disk_if_needed(folder, _disk_max_bytes())
    except OSError:
        pass


def clear_cache(namespace: str | None = None) -> None:
    """Clear memory and disk CAD cache entries, optionally scoped to one namespace."""
    global _MEMORY_BYTES, _HITS, _MISSES
    safe_ns = "".join(c if c.isalnum() or c in "-_" else "_" for c in (namespace or ""))[:48]
    prefix = f"{safe_ns}--" if namespace else None
    with _LOCK:
        if prefix is None:
            _MEMORY.clear()
            _MEMORY_BYTES = 0
            _HITS = 0
            _MISSES = 0
        else:
            for k in [key for key in _MEMORY if key.startswith(prefix)]:
                _MEMORY_BYTES -= len(_MEMORY.pop(k))
    folder = _cache_dir()
    if folder.exists():
        pattern = f"{prefix}*.bin" if prefix else "*.bin"
        for path in folder.glob(pattern):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue


def cache_stats() -> dict[str, Any]:
    """Return memory/disk footprint and hit/miss counters for telemetry."""
    folder = _cache_dir()
    disk_entries = 0
    disk_bytes = 0
    if folder.exists():
        for path in folder.glob("*.bin"):
            try:
                disk_bytes += path.stat().st_size
                disk_entries += 1
            except OSError:
                continue
    with _LOCK:
        return {
            "memory_entries": len(_MEMORY),
            "memory_bytes": _MEMORY_BYTES,
            "disk_entries": disk_entries,
            "disk_bytes": disk_bytes,
            "hits": _HITS,
            "misses": _MISSES,
            "cache_dir": str(folder),
        }
