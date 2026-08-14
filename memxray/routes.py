"""HTTP API served off ComfyUI's own aiohttp app.

  GET  /memxray/stats               latest snapshot + static info
  GET  /memxray/history?seconds=300 column-oriented backfill for the graphs
  POST /memxray/marker              drop a labelled marker on the timeline
  POST /memxray/clear_markers
"""

from __future__ import annotations

import logging

from aiohttp import web

from .sampler import SAMPLER

log = logging.getLogger("MemXray")

# Column layout for the compact history payload. The frontend reads this list
# rather than hard-coding indices, so adding a series here is a one-line change.
HISTORY_COLUMNS = [
    ("t", lambda s: round(s["t"], 2)),
    ("phys_total", lambda s: s["sys"]["phys_total"]),
    # headroom: what is still available before Windows starts reclaiming.
    # Available Bytes already counts the standby cache as reclaimable, which is
    # what "free RAM" means in practice.
    ("phys_avail", lambda s: int(s["sys"].get("phys_avail", 0))),
    ("in_use", lambda s: int(s["sys"]["in_use"])),
    ("standby", lambda s: int(s["sys"]["standby"])),
    ("modified", lambda s: int(s["sys"]["modified"])),
    ("free", lambda s: int(s["sys"]["free"])),
    ("pagefile_used", lambda s: int(s["sys"]["pagefile_used"])),
    ("commit_total", lambda s: int(s["sys"]["commit_total"])),
    ("private_ws", lambda s: int(s["proc"]["private_ws"])),
    ("private_bytes", lambda s: int(s["proc"]["private_bytes"])),
    ("paged_out", lambda s: int(s["proc"]["paged_out"])),
    ("evicted_max", lambda s: int(s["proc"].get("evicted_max", 0))),
    ("pagefile_delta", lambda s: int(s["sys"].get("pagefile_delta", 0))),
    ("mapped_ws", lambda s: int(s["proc"]["mapped_ws"])),
    ("page_reads_sec", lambda s: round(s["faults"]["page_reads_sec"], 2)),
    ("pages_input_sec", lambda s: round(s["faults"]["pages_input_sec"], 2)),
    ("proc_faults_sec", lambda s: round(s["proc"]["page_faults_sec"], 2)),
]


def _history_payload(seconds: int) -> dict:
    rows = SAMPLER.snapshot_history(seconds)
    cols = {name: [] for name, _ in HISTORY_COLUMNS}
    for row in rows:
        for name, get in HISTORY_COLUMNS:
            try:
                cols[name].append(get(row))
            except Exception:
                cols[name].append(0)
    return {
        "columns": [name for name, _ in HISTORY_COLUMNS],
        "series": cols,
        "count": len(rows),
        "markers": SAMPLER.snapshot_markers(seconds),
    }


def register(routes) -> None:
    @routes.get("/memxray/stats")
    async def stats(request):
        latest = SAMPLER.latest()
        if not latest:
            # Sampler thread has not produced its first tick yet.
            latest = SAMPLER.sample()
        return web.json_response(
            {
                "static": SAMPLER.static,
                "now": latest,
                "markers": SAMPLER.snapshot_markers(120),
            }
        )

    @routes.get("/memxray/history")
    async def history(request):
        try:
            seconds = int(request.query.get("seconds", "300"))
        except ValueError:
            seconds = 300
        seconds = max(10, min(seconds, SAMPLER.static.get("history_seconds", 900)))
        return web.json_response(_history_payload(seconds))

    @routes.post("/memxray/marker")
    async def marker(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        m = SAMPLER.add_marker(body.get("label", "mark"), body.get("note", ""))
        return web.json_response({"ok": True, "marker": m})

    @routes.post("/memxray/clear_markers")
    async def clear_markers(request):
        SAMPLER.clear_markers()
        return web.json_response({"ok": True})


def install() -> bool:
    """Attach the routes to the running PromptServer. Returns False (without
    raising) if ComfyUI's server is not up - the nodes still work."""
    try:
        from server import PromptServer

        # Absent entirely until ComfyUI constructs the server; custom nodes
        # normally load after that, but the pack must import standalone too.
        instance = getattr(PromptServer, "instance", None)
        if instance is None:
            return False
        register(instance.routes)
        return True
    except Exception:
        log.exception("[MemXray] failed to register HTTP routes")
        return False
