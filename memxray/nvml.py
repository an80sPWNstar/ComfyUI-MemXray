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
import os

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


# How far torch's claim about a card may sit from the driver's before the panel
# stops quoting it. Generous on purpose: torch's figure and NVML's are read at
# slightly different moments, and an allocator that just freed a block will
# disagree honestly by a few hundred MB.
TORCH_DISAGREE_BYTES = 256 * 1024 * 1024


def _norm_uuid(v) -> str | None:
    """NVML says 'GPU-8c1f...'; torch says '8c1f...'. Compare the tails."""
    if not v:
        return None
    out = str(v).strip().lower().strip("{}")
    for prefix in ("gpu-", "mig-"):
        if out.startswith(prefix):
            out = out[len(prefix):]
    return out or None


def _match_by_shape(drv: dict, torch_devs: list[dict], claimed: set) -> int | None:
    """Fallback pairing when a UUID is missing on either side: same reported
    name and the same total to within a couple of MB. Deliberately strict -
    pairing the wrong card would relabel a whole trace, which is the failure
    this module's device list was written to avoid."""
    total = drv.get("mem_total") or 0
    name = (drv.get("name") or "").strip()
    for i, t in enumerate(torch_devs):
        if i in claimed:
            continue
        if (t.get("name") or "").strip() != name:
            continue
        if abs(int(t.get("total") or 0) - int(total)) <= 2 * 1024 * 1024:
            return i
    return None


def merged_devices(torch_devs: list[dict] | None = None) -> list[dict]:
    """One row per card the DRIVER reports, with torch's process-level figures
    laid on top. This is what the panel should render.

    total/used/free come from NVML, because torch's cannot be trusted: on this
    box, mid-run, `mem_get_info` returned a frozen constant for both visible
    cards while the driver tracked the real churn, and torch enumerated two of
    three cards - the invisible one holding 12 GB. See `comfy_probe.torch_devices`
    for the measurement.

    What torch is still authoritative about is its own allocator, so
    `torch_allocated`/`torch_reserved` are carried through unchanged, as is the
    torch index - the ledger groups models by `cuda:N`, and only torch's
    numbering answers which card that is.

    `torch_used`/`torch_free`/`torch_disagrees` exist so the panel can say the
    quiet part out loud: this is what ComfyUI thinks, this is what the card
    says, they do not match.
    """
    torch_devs = list(torch_devs or [])
    drv = devices()
    if not drv:
        # No NVML at all (non-NVIDIA, no driver, pynvml missing). Torch's view
        # is then the only view there is - passed through, but labelled, so a
        # reader is never told a torch guess came from the driver.
        out = []
        for t in torch_devs:
            row = dict(t)
            row.update(
                torch_index=t.get("index"),
                nvml_index=None,
                source="torch",
                visible_to_torch=True,
                proc_used=None,
                torch_used=t.get("used"),
                torch_free=t.get("free"),
                torch_disagrees=False,
            )
            out.append(row)
        return out

    # Per-process VRAM, this process only. Expect None per card on Windows:
    # WDDM does not report per-process GPU memory, and nvidia-smi shows [N/A]
    # for every pid on this box. Implemented anyway - the same panel runs on
    # Linux and under TCC, where the figure is real - and the frontend has to
    # treat a null as "not available", never as zero.
    pid = os.getpid()
    mine: dict = {}
    for p in procs():
        if p.get("pid") != pid or p.get("used") is None:
            continue
        gi = p.get("gpu_index")
        # compute and graphics contexts of one process report the same
        # per-device total, so this takes the larger rather than adding them.
        mine[gi] = max(mine.get(gi, 0), int(p["used"]))

    by_uuid: dict = {}
    for i, t in enumerate(torch_devs):
        u = _norm_uuid(t.get("uuid"))
        if u is not None:
            by_uuid.setdefault(u, i)

    rows: list[dict] = []
    claimed: set = set()
    for d in drv:
        ti = by_uuid.get(_norm_uuid(d.get("uuid")))
        if ti is not None and ti in claimed:
            ti = None  # two cards reporting one UUID: pair the first, not both
        if ti is None:
            ti = _match_by_shape(d, torch_devs, claimed)
        t = torch_devs[ti] if ti is not None else {}
        if ti is not None:
            claimed.add(ti)

        total = d.get("mem_total") or int(t.get("total") or 0) or None
        used = d.get("mem_used")
        free = d.get("mem_free")
        t_used = t.get("used")
        disagrees = (
            isinstance(used, int)
            and isinstance(t_used, int)
            and abs(used - t_used) > TORCH_DISAGREE_BYTES
        )

        rows.append(
            {
                # Display id. The torch index when the card has one, so the
                # label matches the cuda:N the ledger talks about, and the NVML
                # index for a card ComfyUI cannot see at all.
                "index": t.get("index") if ti is not None else d.get("index"),
                "torch_index": t.get("index") if ti is not None else None,
                "nvml_index": d.get("index"),
                "uuid": d.get("uuid"),
                "name": d.get("name"),
                "total": total,
                "used": used,
                "free": free,
                "source": "nvml",
                "visible_to_torch": ti is not None,
                "proc_used": mine.get(d.get("index")),
                "torch_allocated": int(t.get("torch_allocated") or 0),
                "torch_reserved": int(t.get("torch_reserved") or 0),
                "torch_used": t_used,
                "torch_free": t.get("free"),
                "torch_disagrees": bool(disagrees),
                "util_gpu": d.get("util_gpu"),
                "temp_c": d.get("temp_c"),
            }
        )

    # A torch device NVML could not be paired with should still appear rather
    # than vanish: better a row marked as torch-sourced than a missing card.
    for i, t in enumerate(torch_devs):
        if i in claimed:
            continue
        row = dict(t)
        row.update(
            torch_index=t.get("index"),
            nvml_index=None,
            source="torch",
            visible_to_torch=True,
            proc_used=None,
            torch_used=t.get("used"),
            torch_free=t.get("free"),
            torch_disagrees=False,
        )
        rows.append(row)

    # Cards ComfyUI can use first, in its own numbering; the rest after, in the
    # driver's. Keeps cuda:0 at the top of the panel where people look for it.
    rows.sort(key=lambda r: (
        0 if r.get("torch_index") is not None else 1,
        r.get("torch_index") if r.get("torch_index") is not None else (r.get("nvml_index") or 0),
    ))
    return rows


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
