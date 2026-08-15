"""Driver-level GPU telemetry via NVML.

torch only sees the devices CUDA hands it - on this box torch reported one card
while the driver had three - and it cannot say which process owns a VRAM
allocation. NVML answers both, so it is sampled alongside torch rather than
instead of it.

Every query is individually guarded: a card that refuses one attribute must
still report the rest, and an unsupported attribute yields None rather than
dropping the card from the list.
"""

from __future__ import annotations

import logging

log = logging.getLogger("MemXray")

_nvml = None            # the imported module, or None
_loaded = False         # _load() has run; do not retry the import every sample
_init_error: str | None = None
_devices: list[tuple[int, object]] = []   # (nvml index, handle)


def _load() -> None:
    """Import pynvml and nvmlInit once. Never raises."""
    global _nvml, _loaded, _init_error, _devices
    if _loaded:
        return
    _loaded = True
    try:
        import pynvml  # nvidia-ml-py installs under the same name
    except Exception as exc:
        _init_error = "pynvml unavailable: %s" % exc
        return
    try:
        pynvml.nvmlInit()
    except Exception as exc:
        _init_error = "nvmlInit failed: %s" % exc
        return
    _nvml = pynvml
    try:
        count = pynvml.nvmlDeviceGetCount()
    except Exception as exc:
        _init_error = "nvmlDeviceGetCount failed: %s" % exc
        return
    # The handle list carries its own index. Appending bare handles would
    # renumber every card after one that failed to open, silently relabelling
    # the 3090 as the 5060 Ti in a whole trace.
    for i in range(count):
        try:
            _devices.append((i, pynvml.nvmlDeviceGetHandleByIndex(i)))
        except Exception as exc:
            log.warning("[MemXray] NVML device %s unavailable: %s", i, exc)


def available() -> bool:
    _load()
    return _nvml is not None and bool(_devices)


def init_error() -> str | None:
    _load()
    return _init_error


def _text(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def _q(fn, *args):
    """Run one NVML query, None on any failure."""
    try:
        return fn(*args)
    except Exception:
        return None


def devices() -> list[dict]:
    """One dict per GPU the driver knows about."""
    _load()
    if not available():
        return []

    n = _nvml
    out: list[dict] = []
    for index, h in _devices:
        d: dict = {"index": index}
        d["name"] = _text(_q(n.nvmlDeviceGetName, h))
        d["uuid"] = _text(_q(n.nvmlDeviceGetUUID, h))
        pci = _q(n.nvmlDeviceGetPciInfo, h)
        d["pci_bus_id"] = _text(getattr(pci, "busId", None)) if pci else None

        mi = _q(n.nvmlDeviceGetMemoryInfo, h)
        d["mem_total"] = getattr(mi, "total", None)
        d["mem_used"] = getattr(mi, "used", None)
        d["mem_free"] = getattr(mi, "free", None)
        # v2 memory info adds driver-reserved bytes; v1 has no such field.
        d["mem_reserved"] = getattr(mi, "reserved", None)

        bar = _q(n.nvmlDeviceGetBAR1MemoryInfo, h)
        d["bar1_total"] = getattr(bar, "bar1Total", None)
        d["bar1_used"] = getattr(bar, "bar1Used", None)

        util = _q(n.nvmlDeviceGetUtilizationRates, h)
        d["util_gpu"] = getattr(util, "gpu", None)
        d["util_mem"] = getattr(util, "memory", None)

        d["temp_c"] = _q(n.nvmlDeviceGetTemperature, h, n.NVML_TEMPERATURE_GPU)
        mw = _q(n.nvmlDeviceGetPowerUsage, h)
        d["power_w"] = None if mw is None else mw / 1000.0
        lim = _q(n.nvmlDeviceGetEnforcedPowerLimit, h)
        d["power_limit_w"] = None if lim is None else lim / 1000.0
        d["fan_pct"] = _q(n.nvmlDeviceGetFanSpeed, h)

        d["clock_sm_mhz"] = _q(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_SM)
        d["clock_mem_mhz"] = _q(n.nvmlDeviceGetClockInfo, h, n.NVML_CLOCK_MEM)
        d["pstate"] = _q(n.nvmlDeviceGetPerformanceState, h)
        d["throttle_reasons"] = _q(
            n.nvmlDeviceGetCurrentClocksThrottleReasons, h
        )

        pending = _q(n.nvmlDeviceGetRetiredPagesPendingStatus, h)
        d["ecc_pending"] = None if pending is None else bool(pending)

        out.append(d)
    return out


_SENTINEL = 2 ** 63  # driver's "not available" for usedGpuMemory


def procs() -> list[dict]:
    """Per-process VRAM across all cards, compute and graphics contexts both.

    Graphics contexts matter here: the desktop, the browser showing the ComfyUI
    panel and any game hold VRAM that ComfyUI cannot use, and attributing that
    to ComfyUI is exactly the mistake this table exists to prevent.
    """
    _load()
    if not available():
        return []

    n = _nvml
    out: list[dict] = []
    for index, h in _devices:
        for kind, fn in (
            ("compute", getattr(n, "nvmlDeviceGetComputeRunningProcesses", None)),
            ("graphics", getattr(n, "nvmlDeviceGetGraphicsRunningProcesses", None)),
        ):
            if fn is None:
                continue
            for p in (_q(fn, h) or []):
                used = getattr(p, "usedGpuMemory", None)
                if used is not None and used >= _SENTINEL:
                    used = None
                out.append(
                    {
                        "gpu_index": index,
                        "pid": getattr(p, "pid", None),
                        "kind": kind,
                        "used": used,
                    }
                )
    return out


def shutdown() -> None:
    global _nvml, _devices, _loaded
    if _nvml is not None:
        try:
            _nvml.nvmlShutdown()
        except Exception:
            pass
    _nvml = None
    _devices = []
    _loaded = False
