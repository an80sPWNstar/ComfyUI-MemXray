"""ComfyUI-MemXray: shows whether ComfyUI's cached model weights are actually
resident in physical RAM or have been evicted to the Windows pagefile - the
one distinction Task Manager never draws.

This file is intentionally defensive everywhere: a failure in any piece of
MemXray (missing nodes module, sampler start failure, route registration
failure) must never stop ComfyUI itself from booting.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("MemXray")

WEB_DIRECTORY = "./web"

NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}

try:
    from .memxray.nodes import NODE_CLASS_MAPPINGS as _NCM
    from .memxray.nodes import NODE_DISPLAY_NAME_MAPPINGS as _NDNM

    NODE_CLASS_MAPPINGS = _NCM
    NODE_DISPLAY_NAME_MAPPINGS = _NDNM
except Exception:
    log.exception("[MemXray] failed to load memxray.nodes; nodes will not be available")

if os.name != "nt":
    log.warning(
        "[MemXray] Windows-only: the private-bytes-vs-working-set distinction "
        "this pack measures is a Windows concept, so the sampler is not "
        "started on this platform. Nodes remain registered but will report "
        "no data."
    )
else:
    try:
        from .memxray.sampler import SAMPLER

        SAMPLER.start()
    except Exception:
        log.exception("[MemXray] failed to start background sampler")

    try:
        from .memxray.routes import install

        install()
    except Exception:
        log.exception("[MemXray] failed to install HTTP routes")

    try:
        from .memxray.sampler import SAMPLER as _SAMPLER

        static = _SAMPLER.static or {}
        log.info(
            "[MemXray] watching pid %s, PDH counters %s",
            static.get("pid", "?"),
            "available" if static.get("pdh") else "unavailable",
        )
    except Exception:
        log.exception("[MemXray] failed to report startup status")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
