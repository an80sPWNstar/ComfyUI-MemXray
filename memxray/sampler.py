"""Background sampler: one snapshot per second into a ring buffer.

Sampling runs whether or not the UI is open, so opening the panel mid-run still
shows the history that led up to now - which is the whole point when the
question is "did loading that checkpoint push me into the pagefile?".
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import deque
from typing import Optional

from . import comfy_probe, nvml, placement, winmem

log = logging.getLogger("MemXray")

SAMPLE_INTERVAL = 1.0
HISTORY_SECONDS = 900  # 15 minutes at 1 Hz

# Thresholds for the verdict line. Deliberately conservative: a monitor that
# cries pagefile at 50 MB of eviction is noise.
PAGED_OUT_WARN = 512 * 1024 * 1024
PAGED_OUT_ALARM = 2 * 1024 * 1024 * 1024
HARD_FAULT_WARN = 50.0    # page reads/sec, system wide
HARD_FAULT_ALARM = 300.0

# How long the verdict must sit at a LOWER level before the reported level is
# allowed to drop. Without this the level flips alarm/warn/mapped every few
# seconds under load and the panel strobes; escalation is still immediate.
VERDICT_HOLD_SECONDS = 10.0
LEVEL_RANK = {"ok": 0, "mapped": 1, "warn": 2, "alarm": 3}


class Sampler:
    def __init__(self):
        self.history: deque = deque(maxlen=HISTORY_SECONDS)
        self.markers: deque = deque(maxlen=200)
        self.lock = threading.Lock()
        # sample() mutates rate/peak state (_prev_pagefile, _peak_paged_out,
        # _model_cache), and it is called both by the background thread and
        # on demand from HTTP/node threads before the first tick - so those
        # mutations are serialized separately from the history lock.
        self._sample_lock = threading.Lock()
        self._start_lock = threading.Lock()
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
        self._held_verdict: Optional[dict] = None
        self._below_since: Optional[float] = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None:
                return
            self._start()

    def _start(self) -> None:
        try:
            import psutil

            self.process_name = os.path.splitext(
                psutil.Process(self.pid).name()
            )[0]
        except Exception:
            self.process_name = os.path.splitext(
                os.path.basename(sys.executable or "python")
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
            # Whether the GPU rows are driver-sourced. When this is false the
            # panel is back to quoting torch, which has been caught inventing
            # VRAM figures on this stack - so it has to be visible, not silent.
            "nvml": nvml.available(),
            "nvml_error": nvml.init_error(),
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
        nvml.shutdown()

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
        with self._sample_lock:
            return self._sample()

    def _sample(self) -> dict:
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
        snap["verdict"] = self._settled_verdict(verdict(snap), now)
        return snap

    def _settled_verdict(self, raw: dict, now: float) -> dict:
        """Hysteresis: escalate immediately, de-escalate only after the raw
        level has stayed lower for VERDICT_HOLD_SECONDS. Stops the reported
        level strobing alarm/warn/mapped every few seconds under load."""
        held = self._held_verdict
        if held is None or LEVEL_RANK.get(raw["level"], 0) >= LEVEL_RANK.get(held["level"], 0):
            self._held_verdict = raw
            self._below_since = None
            return raw
        if self._below_since is None:
            self._below_since = now
        if now - self._below_since >= VERDICT_HOLD_SECONDS:
            self._held_verdict = raw
            self._below_since = None
            return raw
        return held

    def _models(self, now: float) -> dict:
        """Model introspection walks ComfyUI structures, so it is throttled to
        once every 3 s rather than run on every tick."""
        if now - self._model_cache_at > 3.0:
            self._model_cache = comfy_probe.model_summary()
            # Driver figures win over torch's. torch is kept for its allocator
            # numbers and its device numbering only - see
            # nvml.merged_devices() for why its VRAM totals are not trusted.
            self._model_cache["gpus"] = nvml.merged_devices(
                comfy_probe.torch_devices()
            )
            self._model_cache_at = now
        # Per-model placement (which tier each model's bytes are actually in)
        # carries its own, slower throttle: the tensor walk plus per-page probes
        # cost far more than a PDH read. Cheap when it serves the cache.
        placement.PLACEMENT.annotate(self._model_cache, now)
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
        # Only a growing pagefile earns the alarm. Accepting a large
        # evicted_max as an alternative latches: the pagefile does not shrink
        # promptly after a heavy run, so every later read spike - including
        # mapped-file loads on an idle machine - would keep reporting
        # "pagefile in play" for hours. Measured on the 2026-08-14 session:
        # that route produced 470 of 883 alarms, all false; requiring growth
        # keeps every genuinely-attributed one.
        if pagefile_growing:
            return {
                "level": "alarm",
                "text": f"PAGEFILE - heavy hard faulting ({reads:.0f} reads/s) while the pagefile is growing",
            }
        if evicted >= PAGED_OUT_ALARM:
            return {
                "level": "warn",
                "text": f"DISK - heavy hard faulting ({reads:.0f} reads/s); up to {gb(evicted)} sits on the pagefile but it is not growing - reading back evicted memory or mapped model files",
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
