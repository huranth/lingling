"""Windows Job Object: WARP children die with the gateway, no matter how it dies.

Lingling spawns long-lived `wireproxy.exe` (and short-lived `wgcf.exe`)
children. Closing the backend window kills the Python process but on Windows
nothing kills the children -- the SOCKS5 proxies survive as orphans and keep
their ports busy forever (see `start.bat`'s old "then run taskkill /F /IM
wireproxy.exe" stop instructions, which existed precisely because of this).

A Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` fixes it at the OS
level: the moment the last handle to the job closes -- i.e. our process exits,
by any path: window close, Ctrl+C, a crash, Task Manager -- Windows terminates
every process in the job. Children spawned after our process joined the job
inherit membership automatically, so no per-child bookkeeping is needed in the
common case.

The module is a no-op on POSIX, where each child is already its own session
leader and dies with its controlling terminal's death.

``ensure_kill_job()`` is safe to call repeatedly (idempotent) and lazily --
the job is only created when the WARP manager actually spawns something.

Why the ctypes ceremony: ``kernel32`` calls return 64-bit HANDLEs, and the
``ctypes.windll`` default of a 32-bit ``c_int`` return type truncates them.
The first version of this module did exactly that and every
``AssignProcessToJobObject`` silently failed -- children were never in the
job, so closing the gateway still orphaned the proxies. ``_kernel()`` sets
explicit restypes/argtypes so handles round-trip intact, and it uses
``use_last_error=True`` so ``GetLastError`` is available for diagnostics.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
# AssignProcessToJobObject requires PROCESS_SET_QUOTA | PROCESS_TERMINATE.
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

_job: int = 0  # kernel handle; module-global so it outlives every call site

_kernel32 = None


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _on_windows() -> bool:
    return os.name == "nt"


def _kernel():
    """Kernel32 with 64-bit HANDLEs wired up (the silent-failure killer)."""
    global _kernel32
    if _kernel32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32 = k
    return _kernel32

def _assign(pid: int) -> bool:
    """Put an already-running process into the kill job. False if it cannot."""
    if not _job:
        return False
    k = _kernel()
    handle = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(k.AssignProcessToJobObject(_job, handle))
    finally:
        k.CloseHandle(handle)


def ensure_kill_job() -> bool:
    """Create the kill-on-close job and join it with the current process.

    Idempotent: only the first call creates anything; later calls return True
    once armed. Returns True if a kill-on-close job is active (children spawned
    from here now die with this process), False otherwise.

    Children spawned after our process joined the job inherit membership,
    which is what makes the common path zero-touch; ``assign()`` exists as the
    fallback when joining here fails (e.g. our process was already inside a
    foreign job).
    """
    global _job
    if _job:
        return True
    if not _on_windows():
        return False
    k = _kernel()
    job = k.CreateJobObjectW(None, None)
    if not job:
        return False
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info),
    )
    if not ok:
        k.CloseHandle(job)
        return False
    _job = job
    _assign(os.getpid())
    return True


def assign(pid: int) -> bool:
    """Put an already-spawned child into the kill job. True on success."""
    if not _on_windows():
        return False
    return _assign(pid)



def job_active() -> bool:
    """True once a kill-on-close job is armed (used by tests)."""
    return bool(_job)
