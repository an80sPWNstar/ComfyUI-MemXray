"""Background sampler: one snapshot per second into a ring buffer.

Sampling runs whether or not the UI is open, so opening the panel mid-run still
shows the history that led up to now - which is the whole point when the
question is "did loading that checkpoint push me into the pagefile?".
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Optional

from . import comfy_probe, winmem

log = logging.getLogger("MemXray")

SAMPLE_INTERVAL = 1.0
HISTORY_SECONDS = 900  # 15 minutes at 1 Hz

# Thresholds for the verdict line. Deliberately conservative: a monitor that
# cries pagefile at 50 MB of eviction is noise.
PAGED_OUT_WARN = 512 * 1024 * 1024
PAGED_OUT_ALARM = 2 * 1024 * 1024 * 1024
HARD_FAULT_WARN = 50.0    # page reads/sec, system wide
HARD_FAULT_ALARM = 300.0


class Sampler:
    def __init__(self):
        self.history: deque = deque(maxlen=HISTORY_SECONDS)
        self.markers: deque = deque(maxlen=200)
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.pid = os.getpid()
        self.process_name = "python"
        self.static: dict = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pdh: Optional[winmem.PdhSession] = None
        self._latest: dict = {}
        self._peak_paged_out = 0
        self._model_cache: dict = {}
        self._model_cache_at = 0.0
        self._prev_pagefile: Optional[float] = None
        self._prev_t: Optional[float] = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            import psutil

            self.process_name = os.path.splitext(
                psutil.Process(self.pid).name()
            )[0]
        except Exception:
            self.process_name = os.path.splitext(
                os.path.basename(getattr(__import__("sys"), "executable", "python"))
            )[0]

        self._pdh = winmem.PdhSession(self.pid, self.process_name)
        if not self._pdh.ok:
            log.warning("[MemXray] PDH unavailable (%s); falling back to PSAPI only",
                        self._pdh.error)
        else:
            self._pdh.collect()  # prime rate counters; first read is garbage

        pi = winmem.performance_info() or {}
        self.static = {
            "pid": self.pid,
            "process_name": self.process_name,
            "phys_total": pi.get("phys_total", 0),
            "commit_limit": pi.get("commit_limit", 0),
            "page_size": pi.get("page_size", winmem.PAGE_SIZE),
            "pdh": bool(self._pdh and self._pdh.ok),
            "pdh_process_set": self._pdh.process_counter_set if self._pdh else None,
            "pdh_error": self._pdh.error if self._pdh else None,
            "sample_interval": SAMPLE_INTERVAL,
            "history_seconds": HISTORY_SECONDS,
        }
        self.static.update(comfy_probe.vram_state())

        self._thread = threading.Thread(
            target=self._run, name="MemXray-sampler", daemon=True
        )
        self._thread.start()
        log.info(
            "[MemXray] sampling pid %s (%s) via %s",
            self.pid,
            self.process_name,
            self.static.get("pdh_process_set") or "PSAPI",
        )

    def stop(self) -> None:
        self._stop.set()
        if self._pdh:
            self._pdh.close()

    def _run(self) -> None:
        next_at = time.time()
        while not self._stop.is_set():
            try:
                snap = self.sample()
                with self.lock:
                    self.history.append(snap)
                    self._latest = snap
            except Exception:
                log.exception("[MemXray] sampler iteration failed")
            next_at += SAMPLE_INTERVAL
            delay = next_at - time.time()
            if delay < 0:
                next_at = time.time()  # we fell behind; resync rather than spin
                delay = 0
            self._stop.wait(delay)

    # -- one snapshot -----------------------------------------------------

    def sample(self) -> dict:
        now = time.time()
        pdh = self._pdh.collect() if (self._pdh and self._pdh.ok) else {}
        gms = winmem.global_memory_status() or {}
        pi = winmem.performance_info() or {}
        pmc = winmem.process_memory_counters() or {}

        phys_total = pi.get("phys_total") or gms.get("phys_total") or 0
        phys_avail = pi.get("phys_avail") or gms.get("phys_avail") or 0

        # Physical RAM composition, Task-Manager style.
        standby = sum(
            pdh.get(k, 0.0)
            for k in ("standby_normal", "standby_reserve", "standby_core")
        )
        free_zero = pdh.get("free_zero_list", 0.0)
        modified = pdh.get("modified_list", 0.0)
        in_use = max(0.0, phys_total - standby - free_zero - modified)

        # Process side. private_working_set is the honest "in RAM" figure;
        # PSAPI's WorkingSetSize also counts shared/mapped pages.
        priv_ws = pdh.get("proc_private_working_set")
        priv_bytes = pdh.get("proc_private_bytes") or pmc.get("private_bytes", 0)
        total_ws = pdh.get("proc_working_set") or pmc.get("working_set", 0)

        if priv_ws is None:
            # No per-process PDH counter. Best available approximation: assume
            # the whole working set is private. Flagged so the UI can say so.
            priv_ws = float(min(total_ws, priv_bytes))
            estimated = True
        else:
            estimated = False

        paged_out = max(0.0, priv_bytes - priv_ws)
        self._peak_paged_out = max(self._peak_paged_out, paged_out)

        # The gap between committed and resident is NOT automatically pagefile
        # traffic: a process can commit address space it never touches, and
        # untouched commit is never written anywhere. Measured on this machine,
        # ComfyUI sat at a 7.7 GB gap while the whole system pagefile held only
        # 1.7 GB - so the gap alone would have been a false alarm. System
        # pagefile usage is an upper bound on what this process can have
        # evicted, so it is carried alongside and used to gate the verdict.
        pagefile_used = pdh.get("pagefile_used", 0)
        evicted_max = min(paged_out, float(pagefile_used or 0))

        # Hard faults alone cannot tell pagefile traffic from mapped-file
        # traffic: \Memory\Page Reads/sec counts a read from ANY backing store,
        # so simply loading torch's DLLs or mmap'ing a .safetensors spikes it
        # while the pagefile is untouched. Measured here at 1330 reads/s with
        # zero pagefile growth. The rate of change of pagefile bytes is the one
        # signal that is pagefile-specific, so it is what attributes the fault.
        pagefile_rate = 0.0
        if self._prev_pagefile is not None and self._prev_t is not None:
            dt = max(1e-3, now - self._prev_t)
            pagefile_rate = (float(pagefile_used or 0) - self._prev_pagefile) / dt
        self._prev_pagefile = float(pagefile_used or 0)
        self._prev_t = now
        # Working set that is not private = file-backed pages, i.e. mmap'd
        # model files. Those evict back to the .safetensors, not the pagefile.
        mapped_ws = max(0.0, total_ws - priv_ws)

        models = self._models(now)

        snap = {
            "t": now,
            "sys": {
                "phys_total": phys_total,
                "phys_avail": phys_avail,
                "in_use": in_use,
                "standby": standby,
                "modified": modified,
                "free": free_zero,
                "cache_bytes": pdh.get("cache_bytes", 0.0),
                "commit_total": pi.get("commit_total", 0),
                "commit_limit": pi.get("commit_limit", 0),
                "pagefile_used": pdh.get("pagefile_used", 0),
                "pagefile_size": pdh.get("pagefile_size", 0),
                # bytes/sec the pagefile is growing (+) or shrinking (-)
                "pagefile_delta": pagefile_rate,
            },
            "proc": {
                "private_ws": priv_ws,
                "private_bytes": priv_bytes,
                "paged_out": paged_out,
                "paged_out_peak": self._peak_paged_out,
                # upper bound on what actually reached the pagefile
                "evicted_max": evicted_max,
                "untouched_commit": max(0.0, paged_out - evicted_max),
                "working_set": total_ws,
                "mapped_ws": mapped_ws,
                "private_ws_estimated": estimated,
                "page_faults_sec": pdh.get("proc_page_faults_sec", 0.0),
                "io_read_bytes_sec": pdh.get("proc_io_read_bytes_sec", 0.0),
            },
            "faults": {
                # Hard faults: pages actually read from disk to satisfy memory
                # access. Sustained non-zero here is the real thrash signal.
                "page_reads_sec": pdh.get("page_reads_sec", 0.0),
                "pages_input_sec": pdh.get("pages_input_sec", 0.0),
                "page_writes_sec": pdh.get("page_writes_sec", 0.0),
                "pages_output_sec": pdh.get("pages_output_sec", 0.0),
            },
            "models": models,
        }
        snap["verdict"] = verdict(snap)
        return snap

    def _models(self, now: float) -> dict:
        """Model introspection walks ComfyUI structures, so it is throttled to
        once every 3 s rather than run on every tick."""
        if now - self._model_cache_at > 3.0:
            self._model_cache = comfy_probe.model_summary()
            self._model_cache["gpus"] = comfy_probe.torch_devices()
            self._model_cache_at = now
        return self._model_cache

    # -- accessors --------------------------------------------------------

    def latest(self) -> dict:
        with self.lock:
            return dict(self._latest)

    def snapshot_history(self, seconds: Optional[int] = None) -> list:
        with self.lock:
            items = list(self.history)
        if seconds:
            cutoff = time.time() - seconds
            items = [s for s in items if s["t"] >= cutoff]
        return items

    def add_marker(self, label: str, note: str = "") -> dict:
        m = {"t": time.time(), "label": str(label)[:80], "note": str(note)[:200]}
        with self.lock:
            self.markers.append(m)
        return m

    def snapshot_markers(self, seconds: Optional[int] = None) -> list:
        with self.lock:
            items = list(self.markers)
        if seconds:
            cutoff = time.time() - seconds
            items = [m for m in items if m["t"] >= cutoff]
        return items

    def clear_markers(self) -> None:
        with self.lock:
            self.markers.clear()


def verdict(snap: dict) -> dict:
    """Turn the numbers into the one sentence the user actually wants.

    level: ok | mapped | warn | alarm
    """
    proc = snap["proc"]
    faults = snap["faults"]
    paged = proc["paged_out"]
    evicted = proc.get("evicted_max", paged)
    reads = faults.get("page_reads_sec", 0.0)
    mapped = proc["mapped_ws"]

    def gb(n):
        return f"{n / 1024 ** 3:.1f} GB"

    # Only per-process evidence is allowed to raise the level.
    #
    # evicted_max is min(this process's non-resident private memory, the
    # SYSTEM-WIDE pagefile). That is an upper bound, not an attribution: a
    # pagefile holding 1.7 GB of some other program's memory would otherwise
    # escalate ComfyUI to a warning while ComfyUI is perfectly healthy. So
    # evicted_max is reported in the text but only escalates when it is large
    # enough that it cannot plausibly be entirely someone else's, and hard
    # faulting - the only symptom that actually costs wall-clock time - is what
    # drives the alarm.
    # A hard fault says "this came off disk", not "this came off the pagefile".
    # Only a growing pagefile attributes it to the pagefile; otherwise the
    # likeliest source is a memory-mapped model file being paged in, which is
    # normal and expensive-but-not-pathological.
    pagefile_growing = snap["sys"].get("pagefile_delta", 0.0) > 1024 * 1024

    if reads >= HARD_FAULT_ALARM:
        if pagefile_growing or evicted >= PAGED_OUT_ALARM:
            return {
                "level": "alarm",
                "text": f"PAGEFILE - heavy hard faulting ({reads:.0f} reads/s) with the pagefile in play",
            }
        return {
            "level": "warn",
            "text": f"DISK - heavy hard faulting ({reads:.0f} reads/s), but the pagefile is not growing: likely mapped model files being read in",
        }
    if reads >= HARD_FAULT_WARN and evicted >= PAGED_OUT_WARN and pagefile_growing:
        return {
            "level": "alarm",
            "text": f"PAGEFILE - up to {gb(evicted)} out of RAM and the pagefile is growing while memory faults from disk",
        }
    if reads >= HARD_FAULT_WARN:
        return {
            "level": "warn",
            "text": f"DISK - {reads:.0f} hard page reads/s (pagefile or mapped files; not attributable to ComfyUI alone)",
        }
    if evicted >= PAGED_OUT_ALARM:
        return {
            "level": "warn",
            "text": f"PAGEFILE - up to {gb(evicted)} may be out of RAM, but idle (nothing is reading it back)",
        }
    if mapped > 1024 ** 3:
        return {
            "level": "mapped",
            "text": f"RAM - resident; {gb(mapped)} is memory-mapped model files (those evict to the model file, not the pagefile)",
        }
    if paged >= PAGED_OUT_WARN:
        # A big committed-vs-resident gap with an idle pagefile is address
        # space ComfyUI committed and never touched - it costs nothing.
        return {
            "level": "ok",
            "text": f"RAM - resident; the {gb(paged)} committed-but-not-resident gap is untouched commit (at most {gb(evicted)} could be on disk)",
        }
    return {"level": "ok", "text": "RAM - ComfyUI's memory is resident in physical RAM"}


SAMPLER = Sampler()
