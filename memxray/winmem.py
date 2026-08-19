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
    # Soft-fault family. These cost no disk I/O, so they never justify an
    # alarm, but they separate "the process is touching new memory" from "the
    # process is waiting on the disk" - which the total fault rate cannot.
    "page_faults_sec": r"\Memory\Page Faults/sec",
    "transition_faults_sec": r"\Memory\Transition Faults/sec",
    "demand_zero_faults_sec": r"\Memory\Demand Zero Faults/sec",
    "cache_faults_sec": r"\Memory\Cache Faults/sec",
    # Pages pulled back off the standby list are pages Windows had already
    # decided to give up on. A rising rate is the earliest sign of pressure.
    "transition_repurposed_sec": r"\Memory\Transition Pages RePurposed/sec",
    # Kernel pools and the resident file cache compete with the process for
    # the same physical RAM, so they are part of the headroom story.
    "pool_paged": r"\Memory\Pool Paged Bytes",
    "pool_nonpaged": r"\Memory\Pool Nonpaged Bytes",
    "pool_paged_resident": r"\Memory\Pool Paged Resident Bytes",
    "system_cache_resident": r"\Memory\System Cache Resident Bytes",
}

PAGEFILE_COUNTER = r"\Paging File(_Total)\% Usage"
# Per-file, not just the total: Windows grows each pagefile independently and
# a fixed-size file on a full disk fails while the total still looks healthy.
PAGEFILE_WILDCARD = r"\Paging File(*)\% Usage"
PAGEFILE_PEAK_WILDCARD = r"\Paging File(*)\% Usage Peak"

PROCESS_COUNTERS = {
    "private_working_set": "Working Set - Private",
    "working_set": "Working Set",
    "working_set_peak": "Working Set Peak",
    "private_bytes": "Private Bytes",
    "virtual_bytes": "Virtual Bytes",
    # Per-process commit charge and its high-water mark. This is the number
    # that attributes system pagefile usage to a process - without it,
    # evicted_max can only ever be an upper bound over the whole machine.
    "pagefile_bytes": "Page File Bytes",
    "pagefile_bytes_peak": "Page File Bytes Peak",
    "page_faults_sec": "Page Faults/sec",
    "io_read_bytes_sec": "IO Read Bytes/sec",
    "io_write_bytes_sec": "IO Write Bytes/sec",
    "pool_paged_bytes": "Pool Paged Bytes",
    "pool_nonpaged_bytes": "Pool Nonpaged Bytes",
    "handle_count": "Handle Count",
    "thread_count": "Thread Count",
}

# WDDM does not let NVML report per-process VRAM on Windows - every
# usedGpuMemory comes back "not available". Windows exposes the same facts
# through these counter sets instead, and adds the one number NVML has no
# concept of: Shared Usage, GPU memory that lives in system RAM and is
# therefore pagefile-eligible. That is the VRAM -> RAM -> pagefile path this
# whole pack exists to watch, so it is sampled from PDH, not from NVML.
GPU_PROCESS_COUNTERS = {
    "dedicated": r"\GPU Process Memory(*)\Dedicated Usage",
    "shared": r"\GPU Process Memory(*)\Shared Usage",
    "committed": r"\GPU Process Memory(*)\Total Committed",
    "local": r"\GPU Process Memory(*)\Local Usage",
    "non_local": r"\GPU Process Memory(*)\Non Local Usage",
}

GPU_ADAPTER_COUNTERS = {
    "dedicated": r"\GPU Adapter Memory(*)\Dedicated Usage",
    "shared": r"\GPU Adapter Memory(*)\Shared Usage",
    "committed": r"\GPU Adapter Memory(*)\Total Committed",
}

# SSD endurance is spent by writes, not reads, and the page-out counters only
# say how many pages Windows evicted - not which drive took them or what else
# was writing at the time. These do, per physical disk.
DISK_COUNTERS = {
    "read_bytes_sec": r"\PhysicalDisk(*)\Disk Read Bytes/sec",
    "write_bytes_sec": r"\PhysicalDisk(*)\Disk Write Bytes/sec",
    "reads_sec": r"\PhysicalDisk(*)\Disk Reads/sec",
    "writes_sec": r"\PhysicalDisk(*)\Disk Writes/sec",
    "queue": r"\PhysicalDisk(*)\Current Disk Queue Length",
    "avg_read_s": r"\PhysicalDisk(*)\Avg. Disk sec/Read",
    "avg_write_s": r"\PhysicalDisk(*)\Avg. Disk sec/Write",
    "idle_pct": r"\PhysicalDisk(*)\% Idle Time",
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


# --------------------------------------------------------------------------
# Wildcard PDH: counter sets whose instances come and go
# --------------------------------------------------------------------------

PDH_MORE_DATA = 0x800007D2


def _pdh_dll():
    """Lazily load pdh.dll with argtypes set. None off Windows."""
    global _PDH
    if _PDH is not None or not IS_WINDOWS:
        return _PDH
    try:
        dll = ctypes.WinDLL("pdh")
    except OSError:
        return None
    dll.PdhOpenQueryW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    ]
    dll.PdhAddEnglishCounterW.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    dll.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
    dll.PdhCloseQuery.argtypes = [ctypes.c_void_p]
    dll.PdhExpandWildCardPathW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
    ]
    _PDH = dll
    return _PDH


_PDH = None


def expand_wildcard(path: str) -> list[str]:
    """Expand one wildcard counter path into its live instance paths."""
    dll = _pdh_dll()
    if dll is None:
        return []
    size = wintypes.DWORD(0)
    # PDH returns a DWORD status; ctypes hands it back signed, so PDH_MORE_DATA
    # arrives as a negative number and a naive == comparison always fails.
    rc = dll.PdhExpandWildCardPathW(None, path, None, ctypes.byref(size), 0) & 0xFFFFFFFF
    if rc != PDH_MORE_DATA or size.value == 0:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    rc = dll.PdhExpandWildCardPathW(None, path, buf, ctypes.byref(size), 0)
    if rc != 0:
        return []
    # MULTI_SZ: NUL-separated, double-NUL terminated.
    out, start = [], 0
    raw = buf[: size.value]
    for i, ch in enumerate(raw):
        if ch == "\x00":
            if i == start:
                break
            out.append("".join(raw[start:i]))
            start = i + 1
    return out


def _instance_of(path: str) -> str:
    a = path.find("(")
    b = path.rfind(")")
    return path[a + 1 : b] if 0 <= a < b else ""


class WildcardQuery:
    """A PDH query over counter sets with volatile instances.

    Instances appear and vanish as processes start and exit, so the query is
    rebuilt rather than kept forever: a handle to a dead instance keeps
    reporting its last value, which would show a closed ComfyUI still holding
    VRAM for the rest of the trace.
    """

    def __init__(self, counters: dict[str, str]):
        self.counters = counters
        self.error: Optional[str] = None
        self._query = None
        self._handles: list[tuple[str, str, ctypes.c_void_p]] = []
        self._dll = _pdh_dll()
        if self._dll is None:
            self.error = "pdh.dll unavailable"
            return
        self.rebuild()

    @property
    def ok(self) -> bool:
        return bool(self._handles)

    def rebuild(self) -> None:
        self.close()
        dll = self._dll
        if dll is None:
            return
        q = ctypes.c_void_p()
        if dll.PdhOpenQueryW(None, 0, ctypes.byref(q)) != 0:
            self.error = "PdhOpenQuery failed"
            return
        self._query = q
        for key, wildcard in self.counters.items():
            for full in expand_wildcard(wildcard):
                h = ctypes.c_void_p()
                if dll.PdhAddEnglishCounterW(q, full, 0, ctypes.byref(h)) != 0:
                    continue
                self._handles.append((key, _instance_of(full), h))
        # These are gauges, not rates, so one collect is enough to make them
        # readable - but collecting twice costs nothing and keeps any rate
        # counter added later honest.
        dll.PdhCollectQueryData(q)

    def read(self) -> dict[str, dict[str, float]]:
        """{instance: {key: value}} for every instance that reported."""
        out: dict[str, dict[str, float]] = {}
        dll = self._dll
        if dll is None or self._query is None:
            return out
        if dll.PdhCollectQueryData(self._query) != 0:
            return out
        for key, inst, handle in self._handles:
            val = PDH_FMT_COUNTERVALUE()
            rc = dll.PdhGetFormattedCounterValue(
                handle, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, None, ctypes.byref(val)
            )
            if rc == 0:
                out.setdefault(inst, {})[key] = val.value.doubleValue
        return out

    def close(self) -> None:
        if self._query is not None and self._dll is not None:
            try:
                self._dll.PdhCloseQuery(self._query)
            except Exception:
                pass
        self._query = None
        self._handles = []


def parse_gpu_process_instance(inst: str) -> dict:
    """`pid_17156_luid_0x00000000_0x0000C1B0_phys_0` -> its parts.

    The LUID identifies the adapter to Windows; it is NOT the NVML or CUDA
    index, and `phys_N` numbers cards within one LUID. Both are kept raw so a
    later join can be made honestly rather than guessed at here.
    """
    parts = inst.split("_")
    out = {"instance": inst, "pid": None, "luid": None, "phys": None}
    try:
        for i, p in enumerate(parts):
            if p == "pid" and i + 1 < len(parts):
                out["pid"] = int(parts[i + 1])
            elif p == "luid" and i + 2 < len(parts):
                out["luid"] = parts[i + 1] + "_" + parts[i + 2]
            elif p == "phys" and i + 1 < len(parts):
                out["phys"] = int(parts[i + 1])
    except (ValueError, IndexError):
        pass
    return out


def pagefile_instances() -> dict[str, dict]:
    """Per-pagefile-file bytes, read raw so the numbers are bytes not percent.

    FirstValue = pages in use, SecondValue = size in pages, same as the
    _Total instance the sampler already reads.
    """
    dll = _pdh_dll()
    if dll is None:
        return {}
    q = ctypes.c_void_p()
    if dll.PdhOpenQueryW(None, 0, ctypes.byref(q)) != 0:
        return {}
    handles: list[tuple[str, str, ctypes.c_void_p]] = []
    try:
        for key, wildcard in (
            ("used", PAGEFILE_WILDCARD), ("peak", PAGEFILE_PEAK_WILDCARD)
        ):
            for full in expand_wildcard(wildcard):
                h = ctypes.c_void_p()
                if dll.PdhAddEnglishCounterW(q, full, 0, ctypes.byref(h)) == 0:
                    handles.append((key, _instance_of(full), h))
        dll.PdhCollectQueryData(q)
        out: dict[str, dict] = {}
        for key, inst, h in handles:
            raw = PDH_RAW_COUNTER()
            if dll.PdhGetRawCounterValue(h, None, ctypes.byref(raw)) != 0:
                continue
            if raw.SecondValue <= 0:
                continue
            rec = out.setdefault(inst, {"name": inst})
            rec["size"] = raw.SecondValue * PAGE_SIZE
            rec["%s_bytes" % key] = raw.FirstValue * PAGE_SIZE
        return out
    finally:
        try:
            dll.PdhCloseQuery(q)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Per-page residency: where one named byte range actually lives
# --------------------------------------------------------------------------
# Everything above is process-wide. Those counters can say 6 GB of this process
# is not in RAM; they cannot say which allocation the 6 GB belonged to, and
# there is no per-process pagefile counter at all - \Process\Page File Bytes is
# commit charge, not disk. Two calls close both gaps for a range we can name:
#
#   VirtualQuery      - is this region private (its only backing store is the
#                       pagefile) or a mapped file (it evicts back to the
#                       .safetensors it came from)? Exact, not inferred.
#   QueryWorkingSetEx - per page, is it in this process's working set?
#
# What this still cannot tell you: a page that has left the working set may be
# physically in RAM on the standby/modified list rather than on disk. No
# user-mode API exposes that per page, so callers must say "out of RAM, backed
# by <store>" and not "on disk".
#
# QueryWorkingSetEx takes an array of addresses the CALLER fills in, so a large
# range can be probed at a stride instead of page by page: 6 GB at 4 KB
# granularity needs a 25 MB buffer, at 256 KB granularity 393 KB. Weights are
# contiguous and eviction happens in long runs, so the stride reads the same
# answer far cheaper.

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
MEM_IMAGE = 0x1000000

# Default probe granularity and the ceiling on points per call.
RESIDENCY_GRANULARITY = 256 * 1024
RESIDENCY_MAX_POINTS = 8192
_WS_CHUNK = 4096  # entries per QueryWorkingSetEx call

_WS_VALID_BIT = 0x1


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    # PartitionId is Windows 10 2004+; it sits in padding that already existed
    # on 64-bit, so naming it does not change the struct size.
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("_pad", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class PSAPI_WORKING_SET_EX_INFORMATION(ctypes.Structure):
    # VirtualAttributes is a bitfield union; only bit 0 (Valid) is used here,
    # so it is carried as a plain word rather than modelled field by field.
    _fields_ = [
        ("VirtualAddress", ctypes.c_void_p),
        ("VirtualAttributes", ctypes.c_ulonglong),
    ]


_query_working_set_ex = None


def _init_residency_protos() -> None:
    """Prototypes for the two residency calls. Same rule as OpenProcess above:
    without an explicit restype ctypes truncates SIZE_T to int."""
    global _query_working_set_ex
    try:
        _kernel32.VirtualQuery.restype = ctypes.c_size_t
        _kernel32.VirtualQuery.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t
        ]
    except Exception as exc:  # pragma: no cover - VirtualQuery is always there
        log.debug("[MemXray] VirtualQuery unavailable: %s", exc)

    # psapi exports QueryWorkingSetEx; kernel32 exports the same thing as
    # K32QueryWorkingSetEx on builds where the psapi stub is missing.
    for dll, name in ((_psapi, "QueryWorkingSetEx"),
                      (_kernel32, "K32QueryWorkingSetEx")):
        fn = getattr(dll, name, None)
        if fn is None:
            continue
        fn.restype = wintypes.BOOL
        fn.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        _query_working_set_ex = fn
        return
    log.debug("[MemXray] QueryWorkingSetEx unavailable on this build")


def residency_available() -> bool:
    return bool(IS_WINDOWS and _query_working_set_ex is not None)


def region_at(addr: int) -> Optional[dict]:
    """The virtual-memory region containing addr: what backs it, and how far it
    runs. `kind` is what decides pagefile-vs-model-file for a byte range."""
    if not IS_WINDOWS or _kernel32 is None or not addr:
        return None
    mbi = MEMORY_BASIC_INFORMATION()
    got = _kernel32.VirtualQuery(
        ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
    )
    if got < ctypes.sizeof(mbi):
        return None
    kind = {
        MEM_PRIVATE: "private",
        MEM_MAPPED: "mapped",
        MEM_IMAGE: "image",
    }.get(int(mbi.Type), "unknown")
    base = int(mbi.BaseAddress or 0)
    return {
        "base": base,
        "end": base + int(mbi.RegionSize),
        "kind": kind,
        "committed": bool(int(mbi.State) & MEM_COMMIT),
    }


def working_set_residency(
    addr: int,
    length: int,
    granularity: int = RESIDENCY_GRANULARITY,
    max_points: int = RESIDENCY_MAX_POINTS,
) -> Optional[dict]:
    """How much of [addr, addr+length) is in this process's working set.

    Probes at a stride, so `resident` is exact only when `exact` is True;
    otherwise it is the sampled fraction scaled to the range. Reads no page
    contents - probing must never be the thing that faults a model back in.
    """
    if not residency_available() or length <= 0:
        return None
    ps = PAGE_SIZE or 4096
    pages = (length + ps - 1) // ps
    stride_pages = max(1, granularity // ps)
    if -(-pages // stride_pages) > max_points:
        stride_pages = -(-pages // max_points)
    points = -(-pages // stride_pages)
    step = stride_pages * ps

    handle = _kernel32.GetCurrentProcess()
    entry_size = ctypes.sizeof(PSAPI_WORKING_SET_EX_INFORMATION)
    resident_points = 0
    done = 0
    while done < points:
        n = min(_WS_CHUNK, points - done)
        buf = (PSAPI_WORKING_SET_EX_INFORMATION * n)()
        for i in range(n):
            buf[i].VirtualAddress = ctypes.c_void_p(addr + (done + i) * step)
        if not _query_working_set_ex(handle, ctypes.byref(buf), entry_size * n):
            return None
        for i in range(n):
            if buf[i].VirtualAttributes & _WS_VALID_BIT:
                resident_points += 1
        done += n

    frac = resident_points / float(points)
    return {
        "points": points,
        "resident_points": resident_points,
        "granularity": step,
        "exact": stride_pages == 1,
        "resident": int(round(length * frac)),
        "out_of_ram": length - int(round(length * frac)),
    }


def _init_page_size() -> None:
    global PAGE_SIZE
    pi = performance_info()
    if pi and pi.get("page_size"):
        PAGE_SIZE = pi["page_size"]


if IS_WINDOWS:
    _init_page_size()
    _init_residency_protos()
