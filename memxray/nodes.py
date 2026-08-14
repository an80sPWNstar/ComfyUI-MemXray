"""ComfyUI node classes for MemXray.

Four nodes, all reading from the shared SAMPLER (see sampler.py) so every node
sees the same one-sample-per-second history regardless of where it sits in the
graph:

- MemXrayMark   drop a labelled marker + text report (bracket a checkpoint load)
- MemXrayChart  render the timeline to an IMAGE so it can be saved with a run
- MemXrayDelta  "what did that cost me" - diff now vs N seconds ago
- MemXrayGuard  trip a warning/error if paged-out memory or hard faults cross
                a threshold, so a long batch run leaves evidence in the log

A monitoring node must never be the reason a workflow fails, so every FUNCTION
body is defensive - the sole deliberate exception is MemXrayGuard's "error"
on_trip mode, which is the whole point of that node.
"""

from __future__ import annotations

import logging

from .render import human, render_chart
from .sampler import SAMPLER

log = logging.getLogger("MemXray")


def _ensure_started() -> None:
    # Sampler.start() is idempotent (no-ops once a thread exists), so calling
    # it here is cheap insurance in case the pack's __init__ hasn't run yet.
    try:
        SAMPLER.start()
    except Exception:
        log.exception("[MemXray] sampler start failed")


def _current_snapshot() -> dict:
    """Latest sample, sampling on demand if the background thread has not
    produced one yet (e.g. the very first queue after ComfyUI starts)."""
    snap = SAMPLER.latest()
    if not snap:
        try:
            snap = SAMPLER.sample()
        except Exception:
            log.exception("[MemXray] on-demand sample failed")
            snap = {}
    return snap


class AnyType(str):
    """Type marker that compares unequal to nothing, so ComfyUI's input-type
    check (which rejects a link when the two type strings differ) never
    rejects it - it accepts a wire of any type."""

    def __ne__(self, other):
        return False


any_type = AnyType("*")


def _model_lines(models: dict) -> list[str]:
    model_list = models.get("models") or []
    if not model_list:
        return ["loaded models: none"]
    lines = [
        f"loaded models: {models.get('count', 0)}  "
        f"on-device {human(models.get('on_device_bytes', 0))}  "
        f"offloaded (system RAM) {human(models.get('offloaded_bytes', 0))}"
    ]
    for m in model_list:
        lines.append(
            f"  - {m.get('name', '?')}: total {human(m.get('total', 0))}  "
            f"device={m.get('device', '?')}  "
            f"offloaded={human(m.get('offloaded', 0))}"
        )
    return lines


class MemXrayMark:
    """Drops a labelled marker on the timeline and returns a human-readable
    report of memory state at this instant - the node to wire before/after a
    checkpoint loader to see exactly what it cost."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "label": ("STRING", {"default": "mark", "multiline": False}),
            },
            "optional": {
                "any": (any_type, {}),
                "note": ("STRING", {"default": "", "multiline": True}),
            },
        }

    RETURN_TYPES = (any_type, "STRING")
    RETURN_NAMES = ("passthrough", "report")
    FUNCTION = "mark"
    CATEGORY = "MemXray"

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        # Always different so this node (and its console log) re-runs every
        # queue instead of being cached away as "unchanged".
        return float("nan")

    def mark(self, label="mark", any=None, note=""):
        _ensure_started()
        try:
            SAMPLER.add_marker(label, note)
            snap = _current_snapshot()
            if not snap:
                report = f"[{label}] MemXray: no sample available yet"
                log.info(report)
                return (any, report)

            verdict = snap.get("verdict", {})
            proc = snap.get("proc", {})
            sys_ = snap.get("sys", {})
            faults = snap.get("faults", {})
            models = snap.get("models", {})

            lines = [f"[{label}] {verdict.get('text', '?')}"]
            lines.append(f"private working set (in RAM): {human(proc.get('private_ws', 0))}")
            lines.append(
                f"committed but not resident (gap): {human(proc.get('paged_out', 0))} "
                f"(peak {human(proc.get('paged_out_peak', 0))})"
            )
            # The gap above is not automatically pagefile traffic - some of it
            # can be commit the process never touched. evicted_max (the gap
            # capped at the system pagefile's own size) is the honest upper
            # bound on what could actually be sitting on disk.
            lines.append(f"evicted (upper bound): {human(proc.get('evicted_max', 0))}")
            lines.append(f"mapped / file-backed: {human(proc.get('mapped_ws', 0))}")
            lines.append(f"committed (private bytes): {human(proc.get('private_bytes', 0))}")
            lines.append(
                f"pagefile: {human(sys_.get('pagefile_used', 0))} / "
                f"{human(sys_.get('pagefile_size', 0))}"
            )
            lines.append(
                f"hard page reads/sec: {faults.get('page_reads_sec', 0.0):.0f} "
                f"(disk-backed memory access; pagefile or mapped files)"
            )
            lines.append(
                f"system RAM - in use: {human(sys_.get('in_use', 0))}  "
                f"standby: {human(sys_.get('standby', 0))}  "
                f"free: {human(sys_.get('free', 0))}"
            )
            if note:
                lines.append(f"note: {note}")
            lines.extend(_model_lines(models))

            report = "\n".join(lines)
            log.info(report)
            return (any, report)
        except Exception as e:
            log.exception("[MemXray] mark failed")
            return (any, f"MemXray: mark failed ({e})")


class MemXrayChart:
    """Renders the memory timeline (3 panels + loaded-model footer) to an
    IMAGE, so it can be saved right next to the generation it explains."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "seconds": ("INT", {"default": 300, "min": 10, "max": 900, "step": 10}),
                "width": ("INT", {"default": 1200, "min": 480, "max": 4096, "step": 8}),
                "height": ("INT", {"default": 720, "min": 320, "max": 2160, "step": 8}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "chart"
    CATEGORY = "MemXray"

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        return float("nan")

    def chart(self, seconds=300, width=1200, height=720):
        _ensure_started()
        # Imported lazily so the pack still loads on a broken torch install;
        # only this node (which must emit an IMAGE tensor) needs them.
        import numpy as np
        import torch

        img = None
        try:
            history = SAMPLER.snapshot_history(seconds)
            markers = SAMPLER.snapshot_markers(seconds)
            img = render_chart(history, markers, SAMPLER.static, width=width, height=height)
        except Exception:
            log.exception("[MemXray] chart render failed")

        if img is None:
            from PIL import Image
            img = Image.new("RGB", (width, height), (18, 18, 22))

        if img.mode != "RGB":
            img = img.convert("RGB")

        # ComfyUI IMAGE convention: float32 in [0, 1], batch-first, channels-last.
        arr = np.asarray(img).astype(np.float32) / 255.0
        arr = arr[None, ...]  # [1, H, W, 3]
        tensor = torch.from_numpy(arr)
        return (tensor,)


class MemXrayDelta:
    """Diffs the current snapshot against the oldest sample within the last
    `seconds_back` seconds - answers "what did that node cost me?"."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "seconds_back": ("INT", {"default": 30, "min": 2, "max": 900}),
            },
            "optional": {
                "any": (any_type, {}),
            },
        }

    RETURN_TYPES = (any_type, "STRING", "FLOAT", "FLOAT")
    RETURN_NAMES = ("passthrough", "report", "paged_out_delta_gb", "in_ram_delta_gb")
    FUNCTION = "delta"
    CATEGORY = "MemXray"

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        return float("nan")

    def delta(self, seconds_back=30, any=None):
        _ensure_started()
        try:
            window = SAMPLER.snapshot_history(seconds_back)
            if len(window) < 2:
                report = (
                    f"MemXray Delta: not enough history yet "
                    f"({len(window)} sample(s) in last {seconds_back}s)"
                )
                return (any, report, 0.0, 0.0)

            old = window[0]
            cur = _current_snapshot() or window[-1]

            old_proc, cur_proc = old.get("proc", {}), cur.get("proc", {})
            old_sys, cur_sys = old.get("sys", {}), cur.get("sys", {})

            d_priv_ws = cur_proc.get("private_ws", 0.0) - old_proc.get("private_ws", 0.0)
            d_priv_bytes = cur_proc.get("private_bytes", 0.0) - old_proc.get("private_bytes", 0.0)
            d_paged_out = cur_proc.get("paged_out", 0.0) - old_proc.get("paged_out", 0.0)
            d_mapped_ws = cur_proc.get("mapped_ws", 0.0) - old_proc.get("mapped_ws", 0.0)
            d_pagefile = cur_sys.get("pagefile_used", 0.0) - old_sys.get("pagefile_used", 0.0)
            # Page Reads/sec counts hard faults against ANY backing store -
            # the pagefile AND memory-mapped files (torch DLLs, .safetensors),
            # so a high rate here does not by itself mean the pagefile was
            # touched. pagefile_delta (bytes/sec the pagefile itself grew) is
            # the one signal that is pagefile-specific.
            max_hard_reads = max((h.get("faults", {}).get("page_reads_sec", 0.0) for h in window), default=0.0)
            max_pagefile_rate = max((h.get("sys", {}).get("pagefile_delta", 0.0) for h in window), default=0.0)

            paged_out_delta_gb = d_paged_out / (1024 ** 3)
            in_ram_delta_gb = d_priv_ws / (1024 ** 3)

            lines = [
                f"delta over last {seconds_back}s ({len(window)} samples in window):",
                f"private working set (RAM): {human(d_priv_ws)}",
                f"private bytes (committed): {human(d_priv_bytes)}",
                f"paged out (committed but not resident): {human(d_paged_out)}",
                f"mapped / file-backed: {human(d_mapped_ws)}",
                f"pagefile used: {human(d_pagefile)}",
                f"max hard page reads/sec observed: {max_hard_reads:.0f} "
                f"(disk-backed memory access; pagefile or mapped files)",
                f"max pagefile growth rate observed: {human(max_pagefile_rate)}/s"
                + (" <- pagefile actually grew during this window" if max_pagefile_rate > 1024 * 1024 else ""),
            ]
            report = "\n".join(lines)
            return (any, report, paged_out_delta_gb, in_ram_delta_gb)
        except Exception as e:
            log.exception("[MemXray] delta failed")
            return (any, f"MemXray: delta failed ({e})", 0.0, 0.0)


class MemXrayGuard:
    """Passthrough that logs a WARNING (and optionally raises) once the
    process has more than a threshold of memory paged out or hard faults are
    running hot - so a long unattended batch leaves evidence in the log."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # Measured against evicted_max (see sampler.verdict), not raw
                # paged_out - raw paged_out includes committed-but-untouched
                # address space that never reaches disk, which false-tripped
                # this guard at "2.65GB" on an idle machine whose whole system
                # pagefile only held 1.75GB.
                "paged_out_limit_gb": ("FLOAT", {"default": 2.0, "min": 0.0, "max": 512.0, "step": 0.5,
                                                  "tooltip": "Threshold on evicted_max (min of paged-out bytes and the "
                                                              "system pagefile size) - an upper bound on actual eviction."}),
                "hard_reads_limit": ("FLOAT", {"default": 100.0, "min": 0.0, "max": 100000.0, "step": 10.0}),
                "on_trip": (["warn", "error"],),
            },
            "optional": {
                "any": (any_type, {}),
            },
        }

    RETURN_TYPES = (any_type, "STRING")
    RETURN_NAMES = ("passthrough", "status")
    FUNCTION = "guard"
    CATEGORY = "MemXray"

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        return float("nan")

    def guard(self, paged_out_limit_gb=2.0, hard_reads_limit=100.0, on_trip="warn", any=None):
        _ensure_started()
        # Data gathering is defensive - a probe failure must never itself trip
        # the guard. The deliberate raise below is outside this try/except.
        tripped = False
        status = "MemXray Guard: probe failed, treating as not tripped"
        try:
            snap = _current_snapshot()
            proc = snap.get("proc", {})
            faults = snap.get("faults", {})
            # evicted_max (not raw paged_out) - see INPUT_TYPES comment above.
            evicted_gb = proc.get("evicted_max", 0.0) / (1024 ** 3)
            gap_gb = proc.get("paged_out", 0.0) / (1024 ** 3)
            hard_reads = faults.get("page_reads_sec", 0.0)

            tripped = evicted_gb > paged_out_limit_gb or hard_reads > hard_reads_limit
            if tripped:
                status = (
                    f"MemXray Guard TRIPPED: evicted<={evicted_gb:.2f}GB "
                    f"(limit {paged_out_limit_gb}GB), gap {gap_gb:.2f}GB, "
                    f"hard_reads={hard_reads:.0f}/s (limit {hard_reads_limit}/s)"
                )
                log.warning(status)
            else:
                status = (
                    f"MemXray Guard OK: evicted<={evicted_gb:.2f}GB, gap {gap_gb:.2f}GB, "
                    f"hard_reads={hard_reads:.0f}/s"
                )
        except Exception:
            log.exception("[MemXray] guard probe failed")

        if tripped and on_trip == "error":
            raise RuntimeError(status)
        return (any, status)


NODE_CLASS_MAPPINGS = {
    "MemXrayMark": MemXrayMark,
    "MemXrayChart": MemXrayChart,
    "MemXrayDelta": MemXrayDelta,
    "MemXrayGuard": MemXrayGuard,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MemXrayMark": "Mem X-Ray: Mark",
    "MemXrayChart": "Mem X-Ray: Chart",
    "MemXrayDelta": "Mem X-Ray: Delta",
    "MemXrayGuard": "Mem X-Ray: Guard",
}
