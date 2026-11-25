"""CAD execution isolated from the API interpreter and its global interpreter lock.

The parent starts a fixed local script with copied project JSON. No callable,
module path or executable supplied by a project can select what the worker runs.
"""

from __future__ import annotations

import asyncio
import atexit
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 512 * 1024 * 1024
MAX_RESULT_FILES = 256
MAX_DIAGNOSTIC_BYTES = 2048
CAD_TIMEOUT_SECONDS = float(os.environ.get("AUTOLAB_CAD_TIMEOUT_SECONDS", "180"))
CAD_QUEUE_TIMEOUT_SECONDS = float(os.environ.get("AUTOLAB_CAD_QUEUE_TIMEOUT_SECONDS", "180"))
MAX_WORKER_RSS_BYTES = int(os.environ.get("AUTOLAB_CAD_MAX_RSS_MB", "2048")) * 1024 * 1024
POLL_SECONDS = 0.05
_slots = threading.BoundedSemaphore(1)
_children: dict[int, subprocess.Popen] = {}
_children_lock = threading.Lock()


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _ProcessEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def _win32_descendant_pids(root_pid: int) -> list[int]:
    """Return direct and transitive child PIDs on Windows (including venv stubs)."""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return []
        try:
            parent_map: dict[int, list[int]] = {}
            entry = _ProcessEntry32()
            entry.dwSize = ctypes.sizeof(_ProcessEntry32)
            if kernel32.Process32First(snap, ctypes.byref(entry)):
                while True:
                    parent_map.setdefault(int(entry.th32ParentProcessID), []).append(
                        int(entry.th32ProcessID)
                    )
                    if not kernel32.Process32Next(snap, ctypes.byref(entry)):
                        break
            descendants: list[int] = []
            queue = list(parent_map.get(int(root_pid), []))
            seen = {int(root_pid)}
            while queue:
                cid = queue.pop(0)
                if cid in seen:
                    continue
                seen.add(cid)
                descendants.append(cid)
                queue.extend(parent_map.get(cid, []))
            return descendants
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return []


def _process_rss_bytes(pid: int) -> int | None:
    """Measure resident set size (Working Set on Windows, VmRSS on POSIX) for one PID."""
    if os.name == "nt":
        handle = None
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            getter = getattr(kernel32, "K32GetProcessMemoryInfo", None)
            if getter is None:
                getter = ctypes.windll.psapi.GetProcessMemoryInfo
            if getter(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return None
        except Exception:
            return None
        finally:
            if handle:
                try:
                    ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    pass
    proc_status = Path(f"/proc/{int(pid)}/status")
    try:
        for line in proc_status.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1]) * 1024
    except OSError:
        return None
    return None


def _posix_descendant_pids(root_pid: int) -> list[int]:
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return []
    parent_map: dict[int, list[int]] = {}
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8", errors="ignore")
            rparen = stat_text.rfind(")")
            if rparen <= 0:
                continue
            rest = stat_text[rparen + 2 :].split()
            if len(rest) >= 2 and rest[1].lstrip("-").isdigit():
                parent_map.setdefault(int(rest[1]), []).append(int(entry.name))
        except OSError:
            continue
    descendants: list[int] = []
    queue = list(parent_map.get(int(root_pid), []))
    seen = {int(root_pid)}
    while queue:
        cid = queue.pop(0)
        if cid in seen:
            continue
        seen.add(cid)
        descendants.append(cid)
        queue.extend(parent_map.get(cid, []))
    return descendants


def _process_tree_rss_bytes(root_pid: int) -> int | None:
    """Sum resident set size across root_pid and all active descendants (handles venv stubs)."""
    pids = [int(root_pid)]
    if os.name == "nt":
        pids.extend(_win32_descendant_pids(root_pid))
    else:
        pids.extend(_posix_descendant_pids(root_pid))
    total = 0
    seen_any = False
    for pid in pids:
        rss = _process_rss_bytes(pid)
        if rss is not None:
            total += rss
            seen_any = True
    return total if seen_any else None


class CadWorkerError(Exception):
    """Bounded worker execution, protocol or lifecycle failure."""


def _safe_name(name: object) -> None:
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 256
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", name)
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        raise CadWorkerError("The CAD worker returned an unsafe artifact name.")


def _validate_files(files: object) -> dict[str, bytes]:
    if not isinstance(files, dict) or not files or len(files) > MAX_RESULT_FILES:
        raise CadWorkerError("The CAD worker returned an invalid artifact mapping.")
    total = 0
    for name, payload in files.items():
        _safe_name(name)
        if not isinstance(payload, (bytes, bytearray)):
            raise CadWorkerError("The CAD worker returned a nonbinary artifact.")
        total += len(payload)
        if total > MAX_RESULT_BYTES:
            raise CadWorkerError("CAD artifacts exceed the 512 MB uncompressed limit.")
    return files


def _write_result(path: Path, files: dict[str, bytes]) -> None:
    _validate_files(files)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(files):
            archive.writestr(name, files[name])


def _read_result(path: Path) -> dict[str, bytes]:
    # ZIP overhead is bounded separately from its advertised uncompressed files.
    if not path.is_file() or path.stat().st_size > MAX_RESULT_BYTES + 256 * 1024:
        raise CadWorkerError("The CAD worker result is missing or exceeds its transfer limit.")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_RESULT_FILES:
                raise CadWorkerError("The CAD worker returned an invalid artifact count.")
            names = [entry.filename for entry in entries]
            if len(set(names)) != len(names):
                raise CadWorkerError("The CAD worker returned duplicate artifact names.")
            total = 0
            for entry in entries:
                _safe_name(entry.filename)
                total += entry.file_size
                if (
                    entry.is_dir()
                    or entry.compress_type != zipfile.ZIP_STORED
                    or entry.flag_bits & 1
                    or total > MAX_RESULT_BYTES
                ):
                    raise CadWorkerError("The CAD worker returned an invalid artifact archive.")
            files = {entry.filename: archive.read(entry) for entry in entries}
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise CadWorkerError("The CAD worker result could not be read.") from exc
    return _validate_files(files)


def _stop_process(process: subprocess.Popen) -> None:
    if os.name == "nt":
        descendants = _win32_descendant_pids(process.pid)
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        for cid in reversed(descendants):
            h = kernel32.OpenProcess(0x0001, False, cid)
            if h:
                try:
                    kernel32.TerminateProcess(h, 1)
                finally:
                    kernel32.CloseHandle(h)
    if process.poll() is None:
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=2)
    else:
        process.wait()


def _stop_all_children() -> None:
    with _children_lock:
        processes = list(_children.values())
    for process in processes:
        try:
            _stop_process(process)
        except (OSError, subprocess.SubprocessError):
            pass


atexit.register(_stop_all_children)


@asynccontextmanager
async def _cad_slot():
    deadline = time.monotonic() + CAD_QUEUE_TIMEOUT_SECONDS
    while not _slots.acquire(blocking=False):
        if time.monotonic() >= deadline:
            raise CadWorkerError("The CAD worker queue timed out.")
        await asyncio.sleep(POLL_SECONDS)
    try:
        yield
    finally:
        _slots.release()


def _worker_command() -> tuple[str, ...]:
    return sys.executable, "-I", str(Path(__file__).resolve())


def _diagnostic(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_DIAGNOSTIC_BYTES + 1)
        if len(raw) > MAX_DIAGNOSTIC_BYTES:
            return "Worker diagnostic exceeded its limit."
        value = json.loads(raw)
        kind = value.get("error_type")
        return (
            kind
            if isinstance(kind, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", kind)
            else "Worker failed."
        )
    except (OSError, ValueError, TypeError):
        return "Worker failed."


async def execute_cad(project: dict) -> dict[str, bytes]:
    """Run one isolated CAD job, yielding continuously to the API event loop.

    Tests may replace the trusted zero-argument _worker_command hook. Project
    data never controls that command. All such workers use this same process,
    queue, deadline, bounded-result and cleanup path; there is no thread fallback.
    """
    raw = json.dumps(project, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_INPUT_BYTES:
        raise CadWorkerError("The CAD project exceeds the 8 MB worker input limit.")
    async with _cad_slot():
        with TemporaryDirectory(prefix="autolab-cad-") as directory:
            root = Path(directory)
            source, result, diagnostic = (
                root / "project.json",
                root / "cad.zip",
                root / "error.json",
            )
            await asyncio.to_thread(source.write_bytes, raw)
            process = None
            try:
                options = (
                    {"creationflags": subprocess.CREATE_NO_WINDOW}
                    if os.name == "nt"
                    else {"start_new_session": True}
                )
                process = subprocess.Popen(
                    [*_worker_command(), str(source), str(result), str(diagnostic)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    cwd=Path(__file__).resolve().parents[1],
                    **options,
                )
                with _children_lock:
                    _children[process.pid] = process
                deadline = time.monotonic() + CAD_TIMEOUT_SECONDS
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise CadWorkerError("The CAD worker timed out.")
                    rss = _process_tree_rss_bytes(process.pid)
                    if rss is not None and rss > MAX_WORKER_RSS_BYTES:
                        raise CadWorkerError(
                            f"The CAD worker exceeded its {MAX_WORKER_RSS_BYTES // (1024 * 1024)} "
                            f"MB memory limit ({rss / (1024 * 1024):.1f} MB)."
                        )
                    await asyncio.sleep(POLL_SECONDS)
                if process.returncode != 0:
                    raise CadWorkerError("The CAD worker failed: " + _diagnostic(diagnostic))
                return await asyncio.to_thread(_read_result, result)
            finally:
                if process is not None:
                    # Reap on success, failure, timeout and task cancellation. Terminating
                    # OCC happens outside this interpreter, keeping heartbeat reads usable.
                    await asyncio.to_thread(_stop_process, process)
                    with _children_lock:
                        _children.pop(process.pid, None)


async def execute_cad_part(
    project: dict, component_id: str, fmt: str = "step"
) -> tuple[bytes, dict]:
    """Export one component CAD file in the isolated, memory- and time-bounded worker."""
    files = await execute_cad(
        {"_part_request": {"project": project, "component_id": component_id, "format": fmt}}
    )
    key = f"cad/part.{fmt}"
    if key not in files:
        raise CadWorkerError("The CAD worker did not return the requested part artifact.")
    meta = json.loads(files.get("cad/part-report.json", b"{}"))
    return files[key], meta


def _apply_child_rlimit() -> None:
    if os.name == "nt":
        return
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        cap = MAX_WORKER_RSS_BYTES
        if hard == resource.RLIM_INFINITY or cap < hard:
            resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    except Exception:
        pass


def main(arguments: list[str]) -> int:
    if len(arguments) != 3:
        return 2
    source, result, diagnostic = map(Path, arguments)
    try:
        _apply_child_rlimit()
        if source.stat().st_size > MAX_INPUT_BYTES:
            raise CadWorkerError("Input limit exceeded.")
        payload = json.loads(source.read_bytes())
        if not isinstance(payload, dict):
            raise CadWorkerError("A project object is required.")
        if "_mixed_assembly" in payload:
            from app.mixed_geometry import build_mixed_cad

            files = build_mixed_cad(payload["_mixed_assembly"])
        elif "_part_request" in payload:
            from app.cad_exports import export_single_part

            req = payload["_part_request"]
            data, meta = export_single_part(
                req["project"], req["component_id"], fmt=req.get("format", "step")
            )
            files = {
                f"cad/part.{meta['format']}": data,
                "cad/part-report.json": json.dumps(meta).encode("utf-8"),
            }
        else:
            # This import is deliberately confined to the child process.
            from app.cad_exports import build_cad_files

            files = build_cad_files(payload)
        _write_result(result, files)
        return 0
    except BaseException as exc:
        try:
            diagnostic.write_text(
                json.dumps({"error_type": type(exc).__name__[:80]}), encoding="utf-8"
            )
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main(sys.argv[1:]))
