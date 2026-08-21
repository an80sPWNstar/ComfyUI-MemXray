"""What ComfyUI *thinks* it is holding, so it can be compared against what
Windows says is actually in RAM.

ComfyUI keeps loaded models in comfy.model_management.current_loaded_models.
A model that has been offloaded from VRAM lives on as CPU tensors inside this
process - which is exactly the memory Windows may quietly evict to the
pagefile. Weights loaded from safetensors are usually memory-mapped instead,
which means they are file-backed and evict to the .safetensors file rather than
to the pagefile; the panel needs both facts to tell an honest story.

Every attribute access is defensive: model_management changes between ComfyUI
releases and a monitor must never be the thing that breaks a workflow.
"""

from __future__ import annotations

import logging

log = logging.getLogger("MemXray")


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def loaded_models() -> list[dict]:
    """One entry per model ComfyUI currently has loaded."""
    try:
        import comfy.model_management as mm
    except Exception:
        return []

    out: list[dict] = []
    for lm in list(_safe(lambda: mm.current_loaded_models, []) or []):
        model = _safe(lambda: lm.model)
        if model is None:
            continue  # weakref died; entry is about to be reaped

        total = _safe(lambda: int(model.model_size()), 0) or 0
        on_device = _safe(lambda: int(model.loaded_size()), 0) or 0
        load_dev = _safe(lambda: str(lm.device), "?")
        cur_dev = _safe(lambda: str(model.current_loaded_device()), load_dev)
        name = _safe(
            lambda: type(model.model).__name__,
            _safe(lambda: type(model).__name__, "model"),
        )

        # "cuda:1" -> 1, so the ledger can group models under the right card.
        dev_index = None
        if ":" in cur_dev:
            try:
                dev_index = int(cur_dev.rsplit(":", 1)[1])
            except ValueError:
                dev_index = None

        out.append(
            {
                "name": name,
                "device_index": dev_index,
                "total": total,
                # weights sitting on the compute device (VRAM, normally)
                "on_device": on_device,
                # weights parked in system memory - the pagefile candidates
                "offloaded": max(0, total - on_device),
                "device": cur_dev,
                "load_device": load_dev,
                "currently_used": bool(_safe(lambda: lm.currently_used, False)),
            }
        )
    return out


def torch_devices() -> list[dict]:
    """What TORCH believes about each GPU. Not the driver's answer.

    Measured on this box 2026-08-20, mid-run: `mem_get_info` returned a frozen
    constant on both visible cards - gpu0 free bit-identical at 393216000 for
    134 s while the allocator moved 1531 -> 1934 MiB, and gpu1 free at
    6485434367, which is not even page aligned. ComfyUI's own /system_stats
    reports the same figures, so the fiction is upstream of this pack; the
    stack running here (`--fast-disk`, comfy-aimdo, comfy-kitchen) hands torch
    a synthetic answer. torch also enumerates only the cards CUDA exposes: two
    of the three cards on that box, and the hidden one was holding 12 GB.

    So this stays as the PROCESS view - `torch_allocated`/`torch_reserved` are
    real, they come from torch's own allocator - and `nvml.merged_devices()`
    overrides total/used/free with the driver's numbers. Nothing should render
    the figures here as the truth about a card.
    """
    try:
        import torch
    except Exception:
        return []
    if not _safe(lambda: torch.cuda.is_available(), False):
        return []

    out = []
    for i in range(_safe(lambda: torch.cuda.device_count(), 0) or 0):
        # get_device_properties needs no CUDA context, so name and total are
        # always available. mem_get_info DOES need a context and raises on any
        # device ComfyUI has not touched yet - on a 3-GPU box that silently
        # dropped two cards entirely, so a failure here must not skip the card.
        props = _safe(lambda: torch.cuda.get_device_properties(i))
        total = int(getattr(props, "total_memory", 0) or 0)
        name = _safe(lambda: torch.cuda.get_device_name(i), f"cuda:{i}")
        # The UUID is how a torch index is matched to an NVML handle. Index is
        # not usable for that: CUDA_VISIBLE_DEVICES and CUDA_DEVICE_ORDER both
        # renumber, and on this box torch's cuda:1 is the driver's card 2.
        uuid = _safe(lambda: str(getattr(props, "uuid", "") or "") or None)

        free_total = _safe(lambda: torch.cuda.mem_get_info(i))
        if free_total:
            free, total = int(free_total[0]), int(free_total[1])
            used = total - free
        else:
            free = None
            used = None

        out.append(
            {
                "index": i,
                "uuid": uuid,
                "name": name,
                "total": total,
                "free": free,
                "used": used,
                # renamed on the way out by nvml.merged_devices(): these are
                # torch's claim, kept only so the panel can say it was rejected

                # reported even when free/used are unknown: these come from
                # torch's own allocator and never need a foreign context
                "torch_allocated": _safe(lambda: int(torch.cuda.memory_allocated(i)), 0),
                "torch_reserved": _safe(lambda: int(torch.cuda.memory_reserved(i)), 0),
            }
        )
    return out


def model_summary() -> dict:
    """Totals used by the verdict logic and the panel header."""
    models = loaded_models()
    return {
        "models": models,
        "count": len(models),
        "total_bytes": sum(m["total"] for m in models),
        "on_device_bytes": sum(m["on_device"] for m in models),
        "offloaded_bytes": sum(m["offloaded"] for m in models),
    }


def vram_state() -> dict:
    """ComfyUI's own VRAM policy - it decides how aggressively models are kept
    in system RAM, so it belongs next to the numbers."""
    try:
        import comfy.model_management as mm
    except Exception:
        return {}
    return {
        "vram_state": _safe(lambda: str(mm.vram_state).split(".")[-1], "?"),
        "cpu_state": _safe(lambda: str(mm.cpu_state).split(".")[-1], "?"),
        "torch_device": _safe(lambda: str(mm.get_torch_device()), "?"),
    }
