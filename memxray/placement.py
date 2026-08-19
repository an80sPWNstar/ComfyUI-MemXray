"""Per-model placement: which tier each loaded model's bytes are actually in.

The process-wide counters in `winmem` can say how much of ComfyUI is out of RAM.
They cannot say *whose* bytes those were, so the panel used to report one
process-level ceiling ("up to N GB may be on the pagefile") and leave the user
to guess which checkpoint it belonged to.

This module answers it per model, by measurement rather than apportionment:

  1. Walk the model's parameters and buffers for the address range of every
     storage. Torch hands over `data_ptr()` and `nbytes()` without reading a
     byte, so the walk never faults evicted weights back in.
  2. `VirtualQuery` each range: private memory (only the pagefile can back it)
     or a mapped file (evicts back to the .safetensors it was loaded from).
  3. `QueryWorkingSetEx` each range: how many of those pages are in this
     process's working set right now.

Four tiers come out of that, and only the last one is inferred at all:

  gpu       - on the compute device. From ComfyUI's own loaded_size.
  ram       - in this process's working set. Measured per page.
  pagefile  - private and out of the working set. Measured; the pagefile is the
              only store that can hold it.
  file      - mapped and out of the working set. Measured; it comes back from
              the model file, not the pagefile.

The one honest caveat: a page that has left the working set may still be
physically in RAM on the standby list. No user-mode API reports that per page,
so `pagefile` and `file` mean "out of RAM, and this is what backs it" - not
"currently sitting on the platter". Callers must word it that way.

Cost control: the walk plus the probes are far heavier than a PDH read, so the
sampler calls this on its own slower cadence and reuses the last result in
between.
"""

from __future__ import annotations

import logging
import threading
import time
from itertools import chain
from typing import Optional

from . import winmem

log = logging.getLogger("MemXray")

# How often the probe may actually run. Everything in between is served from
# the cache, keyed per model, so the figures hold still instead of blinking.
PLACEMENT_INTERVAL = 5.0

# Runaway guards. A model with more tensors than this gets probed up to the
# limit and reports the remainder as unattributed rather than stalling the
# sampler thread.
MAX_TENSORS = 40000
MAX_RANGES = 8000

TIERS = ("gpu", "ram", "pagefile", "file", "unknown")


def _empty(measured: bool = True) -> dict:
    return {
        "measured": measured,
        "gpu": 0,
        "ram": 0,
        "pagefile": 0,
        "file": 0,
        "unknown": 0,
        "granularity": 0,
        "exact": True,
    }


class Placement:
    """Throttled per-model placement, cached by model identity."""

    def __init__(self):
        self._cache: dict = {}
        self._at = 0.0
        self._last_cost = 0.0
        self._error: Optional[str] = None
        # The probe measured 0.5-0.7 s on a 40 GB, three-model MiniMax load, so
        # it does NOT run on the sampler thread: a 1 Hz snapshot must not be
        # delayed by half a second, and the HTTP handlers share that lock.
        self._busy = False
        self._kick_lock = threading.Lock()

    def annotate(self, summary: dict, now: Optional[float] = None) -> dict:
        """Attach `placement` to every model in a comfy_probe.model_summary()
        dict, plus panel-level totals. Safe to call every tick - the probe
        itself runs on its own thread, so this only ever reads the cache."""
        now = now or time.time()
        models = summary.get("models") or []
        if not models:
            self._cache = {}
            summary["placement_totals"] = _empty(winmem.residency_available())
            return summary

        if now - self._at >= PLACEMENT_INTERVAL:
            self._kick()

        totals = _empty(winmem.residency_available())
        for i, model in enumerate(models):
            place = self._cache.get(_key(i, model))
            if place is None:
                # A model that appeared since the last probe: report the split
                # ComfyUI itself knows and mark it unmeasured, rather than
                # showing zeroes or borrowing another model's numbers.
                place = _from_comfy(model)
            else:
                place = _reconcile(place, model)
            model["placement"] = place
            for tier in TIERS:
                totals[tier] += place.get(tier, 0)
            totals["granularity"] = max(totals["granularity"], place.get("granularity", 0))
            if not place.get("exact", True):
                totals["exact"] = False
            if not place.get("measured", False):
                totals["measured"] = False

        totals["probe_ms"] = round(self._last_cost * 1000, 1)
        totals["probe_age"] = max(0.0, now - self._at)
        if self._error:
            totals["error"] = self._error
        summary["placement_totals"] = totals
        return summary

    def _kick(self) -> None:
        """Start one probe in the background if none is in flight."""
        with self._kick_lock:
            if self._busy:
                return
            self._busy = True
        threading.Thread(
            target=self._refresh, name="MemXray-placement", daemon=True
        ).start()

    def _refresh(self) -> None:
        started = time.perf_counter()
        try:
            self._cache = _probe_all()
            self._error = None
        except Exception as exc:
            # Never let introspection of a live model graph take out the
            # sampler; keep the previous numbers and say why they are stale.
            self._error = f"{type(exc).__name__}: {exc}"
            log.debug("[MemXray] placement probe failed: %s", self._error)
        finally:
            self._last_cost = time.perf_counter() - started
            # Stamped at completion, not at kick: the interval is time between
            # probes finishing, so a slow probe cannot queue up behind itself.
            self._at = time.time()
            self._busy = False


def _key(index: int, model: dict) -> tuple:
    # name+size is not unique (four VAEs of the same class happen), so the
    # position in current_loaded_models is part of the identity.
    return (index, model.get("name"), model.get("total"), model.get("device_index"))


def _reconcile(place: dict, model: dict) -> dict:
    """Make the tiers add up to the size ComfyUI reports for the model.

    Measured on a live MiniMax load: the walk found 13.33 GB of a model
    model_size() called 14.61 GB. Weights held somewhere other than a registered
    parameter or buffer are invisible to the walk, and silently reporting the
    smaller total would make the panel disagree with the size everyone already
    knows. The shortfall is carried as `unknown` - "off the card, not located" -
    rather than being spread across the measured tiers.
    """
    total = max(0, int(model.get("total") or 0))
    walked = sum(max(0, int(place.get(t) or 0)) for t in TIERS)
    if total <= walked:
        return place
    out = dict(place)
    out["unknown"] = int(place.get("unknown") or 0) + (total - walked)
    out["walked"] = walked
    return out


def _from_comfy(model: dict) -> dict:
    """Fallback: the two-way split ComfyUI reports, marked unmeasured so the UI
    can say the per-page probe has not run for this model yet."""
    place = _empty(measured=False)
    place["gpu"] = max(0, int(model.get("on_device") or 0))
    place["unknown"] = max(0, int(model.get("offloaded") or 0))
    return place


def _probe_all() -> dict:
    """One pass over every loaded model. Returns {key: placement}."""
    try:
        import comfy.model_management as mm
    except Exception:
        return {}

    out: dict = {}
    for i, lm in enumerate(list(getattr(mm, "current_loaded_models", []) or [])):
        model = getattr(lm, "model", None)
        if model is None:
            continue
        key = (
            i,
            _model_name(model),
            _int(getattr(model, "model_size", lambda: 0)()),
            _device_index(lm, model),
        )
        out[key] = _probe_model(model)
    return out


def _model_name(model) -> str:
    inner = getattr(model, "model", None)
    if inner is not None:
        return type(inner).__name__
    return type(model).__name__


def _device_index(lm, model) -> Optional[int]:
    dev = None
    for src in (model, lm):
        get = getattr(src, "current_loaded_device", None)
        if callable(get):
            try:
                dev = str(get())
                break
            except Exception:
                pass
    if dev is None:
        dev = str(getattr(lm, "device", "") or "")
    if ":" in dev:
        try:
            return int(dev.rsplit(":", 1)[1])
        except ValueError:
            return None
    return None


def _int(v) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def _probe_model(model) -> dict:
    """gpu/ram/pagefile/file bytes for one ComfyUI ModelPatcher."""
    place = _empty(winmem.residency_available())
    module = _torch_module(model)
    if module is None:
        place["measured"] = False
        return place

    cpu_ranges, gpu_bytes, truncated = _storage_ranges(module)
    place["gpu"] = gpu_bytes
    if truncated:
        place["exact"] = False

    if not winmem.residency_available():
        place["measured"] = False
        place["unknown"] = sum(b - a for a, b in cpu_ranges)
        return place

    for a, b in _merge(cpu_ranges):
        _classify(a, b, place)
    return place


def _torch_module(model):
    """ComfyUI wraps the real nn.Module in a ModelPatcher; the weights live on
    the inner one. Both shapes are accepted because model_management has
    changed this more than once."""
    inner = getattr(model, "model", None)
    if hasattr(inner, "named_parameters"):
        return inner
    if hasattr(model, "named_parameters"):
        return model
    return None


def _storage_ranges(module):
    """Address ranges of every CPU-side storage, plus total device bytes.

    Deduplicated by (address, size): ComfyUI ties weights together often enough
    that counting a shared storage twice would inflate a model past its own
    size. Overlapping views are handled later by _merge.
    """
    seen: set = set()
    cpu: list = []
    gpu_bytes = 0
    count = 0
    truncated = False

    for _name, t in chain(
        module.named_parameters(recurse=True),
        module.named_buffers(recurse=True),
    ):
        if t is None:
            continue
        count += 1
        if count > MAX_TENSORS or len(cpu) > MAX_RANGES:
            truncated = True
            break
        try:
            dev = t.device.type
        except Exception:
            continue
        addr, nbytes = _range_of(t)
        if not addr or nbytes <= 0:
            continue
        if (addr, nbytes) in seen:
            continue
        seen.add((addr, nbytes))
        if dev == "cpu":
            cpu.append((addr, addr + nbytes))
        else:
            # A device pointer is not a host address - never hand it to
            # VirtualQuery. Device bytes are counted, not probed.
            gpu_bytes += nbytes
    return cpu, gpu_bytes, truncated


def _range_of(t):
    """(address, bytes) of the storage behind a tensor. Metadata only: no page
    of the tensor is read, so an evicted weight stays evicted."""
    try:
        st = t.untyped_storage()
        return int(st.data_ptr()), int(st.nbytes())
    except Exception:
        pass
    try:
        return int(t.data_ptr()), int(t.numel()) * int(t.element_size())
    except Exception:
        return 0, 0


def _merge(ranges: list) -> list:
    """Union of possibly-overlapping ranges - two tensors can be views on one
    mapping, and probing the overlap twice would double-count it."""
    if not ranges:
        return []
    ranges = sorted(ranges)
    out = [list(ranges[0])]
    for a, b in ranges[1:]:
        if a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _classify(a: int, b: int, place: dict) -> None:
    """Split [a,b) by backing store, then measure residency of each piece.

    Walks regions rather than assuming one: a merged range can span a private
    allocation and a file mapping, and calling the whole thing private would
    blame the pagefile for safetensors pages.
    """
    addr = a
    while addr < b:
        region = winmem.region_at(addr)
        if region is None or region["end"] <= addr:
            place["unknown"] += b - addr
            return
        end = min(b, region["end"])
        length = end - addr
        if not region["committed"]:
            # Reserved but not committed: holds nothing, backs nothing.
            place["unknown"] += length
        else:
            res = winmem.working_set_residency(addr, length)
            if res is None:
                place["unknown"] += length
            else:
                place["ram"] += res["resident"]
                tier = "file" if region["kind"] in ("mapped", "image") else "pagefile"
                place[tier] += res["out_of_ram"]
                place["granularity"] = max(place["granularity"], res["granularity"])
                if not res["exact"]:
                    place["exact"] = False
        addr = end


PLACEMENT = Placement()
