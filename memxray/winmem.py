"""Low-level Windows memory probes.

Everything here answers one question: is a byte of ComfyUI's memory sitting in
physical RAM right now, or has Windows pushed it out to the pagefile?

The two numbers that matter:

  Private Bytes         - private memory the process has committed. Includes
                          anything the kernel has since evicted.
  Working Set - Private - the subset of Private Bytes currently resident in
                          physical RAM.

  Private Bytes - Working Set Private = private memory NOT in RAM.

Task Manager shows neither pair side by side, which is why "is it cached in RAM
or in the pagefile?" is normally unanswerable by eye.

Both counters come from PDH (Performance Data Helper). The Process V2 counter
set is used because its instance names are "<procname>:<pid>", so there is no
ambiguity when a dozen python.exe are running. Everything degrades gracefully:
if a counter is missing the value comes back None instead of raising.
"""

from __future__ import annotations

import ctypes
import logging
import os
from ctypes import wintypes
from typing import Optional

IS_WINDOWS = os.name == "nt"

log = logging.getLogger("MemXray")

PAGE_SIZE = 4096  # refreshed from GetPerformanceInfo at import time


# --------------------------------------------------------------------------
# kernel32 / psapi structures
# --------------------------------------------------------------------------

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wintypes.DWORD),
        ("ProcessCount", wintypes.DWORD),
        ("ThreadCount", wintypes.DWORD),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


if IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    # Explicit prototypes are not optional here: ctypes defaults every return
    # type to C int, which truncates the 64-bit HANDLE from OpenProcess and
    # makes every subsequent call fail in a way that looks like a permission
    # problem.
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    _psapi.GetPerformanceInfo.restype = wintypes.BOOL
    _psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    _psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD
    ]
else:  # pragma: no cover - the pack is a no-op off Windows
    _kernel32 = None
    _psapi = None


def global_memory_status() -> Optional[dict]:
    """System-wide physical + commit totals. Cheap, always available."""
    if not IS_WINDOWS:
        return None
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(st)
    if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return None
    return {
        "phys_total": st.ullTotalPhys,
        "phys_avail": st.ullAvailPhys,
        "commit_limit": st.ullTotalPageFile,
        "commit_avail": st.ullAvailPageFile,
        "memory_load_pct": st.dwMemoryLoad,
    }


def performance_info() -> Optional[dict]:
    """Commit charge and kernel pools. CommitTotal is the number Task Manager
    calls "Committed" - it counts RAM *and* pagefile backing together."""
    if not IS_WINDOWS:
        return None
    pi = PERFORMANCE_INFORMATION()
    pi.cb = ctypes.sizeof(pi)
    if not _psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        return None
    ps = pi.PageSize or PAGE_SIZE
    return {
        "page_size": ps,
        "commit_total": pi.CommitTotal * ps,
        "commit_limit": pi.CommitLimit * ps,
        "commit_peak": pi.CommitPeak * ps,
        "phys_total": pi.PhysicalTotal * ps,
        "phys_avail": pi.PhysicalAvailable * ps,
        "system_cache": pi.SystemCache * ps,
        "kernel_paged": pi.KernelPaged * ps,
        "kernel_nonpaged": pi.KernelNonpaged * ps,
    }


def process_memory_counters(pid: Optional[int] = None) -> Optional[dict]:
    """PSAPI counters for one process. PrivateUsage == "Private Bytes"."""
    if not IS_WINDOWS:
        return None
    opened = False
    if pid is None:
        handle = _kernel32.GetCurrentProcess()
    else:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        PROCESS_VM_READ = 0x0010
        handle = _kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid
        )
        if not handle:
            return None
        opened = True
    try:
        pmc = PROCESS_MEMORY_COUNTERS_EX()
        pmc.cb = ctypes.sizeof(pmc)
        ok = _psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb)
        if not ok:
            return None
        return {
            "working_set": pmc.WorkingSetSize,
            "working_set_peak": pmc.PeakWorkingSetSize,
            "private_bytes": pmc.PrivateUsage,
            "private_peak": pmc.PeakPagefileUsage,
            "page_faults_total": pmc.PageFaultCount,
        }
    finally:
        if opened:
            _kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------
# PDH
# --------------------------------------------------------------------------

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000


class _PdhValueUnion(ctypes.Union):
    _fields_ = [
        ("longValue", ctypes.c_long),
        ("doubleValue", ctypes.c_double),
        ("largeValue", ctypes.c_longlong),
        ("AnsiStringValue", ctypes.c_char_p),
        ("WideStringValue", ctypes.c_wchar_p),
    ]


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("value", _PdhValueUnion)]


class PDH_RAW_COUNTER(ctypes.Structure):
    _fields_ = [
        ("CStatus", wintypes.DWORD),
        ("TimeStamp", wintypes.FILETIME),
        ("FirstValue", ctypes.c_longlong),
        ("SecondValue", ctypes.c_longlong),
        ("MultiCount", wintypes.DWORD),
    ]


# Everything the panel graphs. Keys are what the rest of the pack asks for.
SYSTEM_COUNTERS = {
    "avail_bytes": r"\Memory\Available Bytes",
    "standby_normal": r"\Memory\Standby Cache Normal Priority Bytes",
    "standby_reserve": r"\Memory\Standby Cache Reserve Bytes",
    "standby_core": r"\Memory\Standby Cache Core Bytes",
    "modified_list": r"\Memory\Modified Page List Bytes",
    "free_zero_list": r"\Memory\Free & Zero Page List Bytes",
    "cache_bytes": r"\Memory\Cache Bytes",
    # The hard-fault family. Non-zero here means the disk is being read to
    # satisfy memory accesses - the actual symptom of pagefile thrash.
    "pages_input_sec": r"\Memory\Pages Input/sec",
    "page_reads_sec": r"\Memory\Page Reads/sec",
    "pages_output_sec": r"\Memory\Pages Output/sec",
    "page_writes_sec": r"\Memory\Page Writes/sec",
}

PAGEFILE_COUNTER = r"\Paging File(_Total)\% Usage"

PROCESS_COUNTERS = {
    "private_working_set": "Working Set - Private",
    "working_set": "Working Set",
    "private_bytes": "Private Bytes",
    "page_faults_sec": "Page Faults/sec",
    "io_read_bytes_sec": "IO Read Bytes/sec",
}


class PdhSession:
    """One PDH query holding every counter we sample.

    PDH rate counters need two collections before they report a number, so the
    first sample after construction is discarded by the caller.
    """

    def __init__(self, pid: int, process_name: str):
        self.ok = False
        self.pid = pid
        self.process_name = process_name
        self.process_counter_set = None
        self.error: Optional[str] = None
        self._query = None
        self._counters: dict[str, ctypes.c_void_p] = {}
        self._pagefile = None
        if not IS_WINDOWS:
            self.error = "not windows"
            return
        try:
            self._pdh = ctypes.WinDLL("pdh")
            self._pdh.PdhOpenQueryW.argtypes = [
                wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
            ]
            self._pdh.PdhAddEnglishCounterW.argtypes = [
                ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            self._pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            self._pdh.PdhRemoveCounter.argtypes = [ctypes.c_void_p]
        except OSError as exc:  # pragma: no cover
            self.error = f"pdh.dll unavailable: {exc}"
            return
        self._open()

    # -- setup ------------------------------------------------------------

    def _open(self) -> None:
        q = ctypes.c_void_p()
        if self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) != 0:
            self.error = "PdhOpenQuery failed"
            return
        self._query = q

        for key, path in SYSTEM_COUNTERS.items():
            self._add(key, path)

        self._pagefile = self._add("_pagefile", PAGEFILE_COUNTER)

        # Process V2 instance names are "<procname>:<pid>", so a box running a
        # dozen python.exe is unambiguous. PdhAddEnglishCounter happily accepts
        # an instance that does not exist and only fails later with
        # PDH_INVALID_DATA, so the instance is validated by actually reading it
        # before it is trusted - otherwise a stale/wrong pid looks like a
        # working monitor that always reports zero.
        v2 = f"{self.process_name}:{self.pid}"
        if self._try_process_instance("Process V2", r"\Process V2(%s)\{counter}" % v2):
            self.process_counter_set = "Process V2"
        else:
            # Older Windows: classic \Process(name#N). The index is positional
            # and NOT the pid, so it must be resolved by reading "ID Process"
            # off each candidate instance. Guessing here silently monitors some
            # unrelated python.exe.
            inst = self._resolve_classic_instance()
            if inst is not None:
                path = r"\Process(%s)\{counter}" % inst
                if self._try_process_instance("Process", path):
                    self.process_counter_set = f"Process({inst})"

        self.ok = bool(self._counters)
        if not self.ok:
            self.error = "no PDH counters could be added"
        elif self.process_counter_set is None:
            self.error = "per-process counters unavailable; system counters only"

    def _try_process_instance(self, label: str, path_template: str) -> bool:
        """Add the per-process counters for one instance and prove they read.

        Returns False and removes the counters again if the instance does not
        produce data, so the caller can fall through to the next strategy.
        """
        added: dict[str, ctypes.c_void_p] = {}
        for key, counter in PROCESS_COUNTERS.items():
            h = self._add(f"proc_{key}", path_template.format(counter=counter))
            if h is not None:
                added[f"proc_{key}"] = h
        if not added:
            return False

        # Two collections: rate counters need a delta, and an absent instance
        # only reveals itself on the read, not on the add.
        self._pdh.PdhCollectQueryData(self._query)
        probe_key = "proc_private_working_set"
        handle = added.get(probe_key) or next(iter(added.values()))
        val = PDH_FMT_COUNTERVALUE()
        rc = self._pdh.PdhGetFormattedCounterValue(
            handle, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, None, ctypes.byref(val)
        )
        if rc == 0:
            return True

        for key, h in added.items():
            try:
                self._pdh.PdhRemoveCounter(h)
            except Exception:
                pass
            self._counters.pop(key, None)
        return False

    def _resolve_classic_instance(self, max_index: int = 64) -> Optional[str]:
        """Find which \\Process(name#N) instance is this pid, via ID Process."""
        for i in range(max_index):
            inst = self.process_name if i == 0 else f"{self.process_name}#{i}"
            h = ctypes.c_void_p()
            path = r"\Process(%s)\ID Process" % inst
            if self._pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(h)) != 0:
                break  # ran off the end of the instance list
            self._pdh.PdhCollectQueryData(self._query)
            val = PDH_FMT_COUNTERVALUE()
            rc = self._pdh.PdhGetFormattedCounterValue(
                h, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, None, ctypes.byref(val)
            )
            try:
                self._pdh.PdhRemoveCounter(h)
            except Exception:
                pass
            if rc == 0 and int(val.value.doubleValue) == self.pid:
                return inst
        return None

    def _add(self, key: str, path: str):
        h = ctypes.c_void_p()
        # AddEnglishCounter so this works on non-English Windows installs.
        rc = self._pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(h))
        if rc != 0:
            return None
        self._counters[key] = h
        return h

    # -- sampling ---------------------------------------------------------

    def collect(self) -> dict:
        """Collect every counter once. Returns {key: float|int}; missing or
        not-yet-valid counters are simply absent from the dict."""
        out: dict = {}
        if not self.ok:
            return out
        if self._pdh.PdhCollectQueryData(self._query) != 0:
            return out

        for key, handle in self._counters.items():
            if key == "_pagefile":
                continue
            val = PDH_FMT_COUNTERVALUE()
            rc = self._pdh.PdhGetFormattedCounterValue(
                handle, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, None, ctypes.byref(val)
            )
            if rc == 0:
                out[key] = val.value.doubleValue

        # Pagefile is read raw: FirstValue = pages in use, SecondValue = size in
        # pages. That yields true pagefile bytes, which no formatted counter and
        # no commit-charge arithmetic gives you.
        if self._pagefile is not None:
            raw = PDH_RAW_COUNTER()
            rc = self._pdh.PdhGetRawCounterValue(self._pagefile, None, ctypes.byref(raw))
            if rc == 0 and raw.SecondValue > 0:
                out["pagefile_used"] = raw.FirstValue * PAGE_SIZE
                out["pagefile_size"] = raw.SecondValue * PAGE_SIZE

        return out

    def close(self) -> None:
        if self._query is not None:
            try:
                self._pdh.PdhCloseQuery(self._query)
            except Exception:
                pass
            self._query = None
            self.ok = False


def _init_page_size() -> None:
    global PAGE_SIZE
    pi = performance_info()
    if pi and pi.get("page_size"):
        PAGE_SIZE = pi["page_size"]


if IS_WINDOWS:
    _init_page_size()
