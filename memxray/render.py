"""PIL chart renderer for the MemXray Chart node.

Same three panels as the live web panel, drawn server-side so a run can be
saved next to the image it produced. No matplotlib dependency - ComfyUI already
ships Pillow and this only needs lines and rectangles.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

BG = (18, 18, 22)
GRID = (44, 46, 54)
TEXT = (222, 224, 232)
DIM = (140, 146, 160)

RESIDENT = (70, 190, 120)     # private memory in RAM
PAGED = (226, 84, 74)         # private memory evicted
MAPPED = (86, 154, 232)       # file-backed / mmap'd pages
STANDBY = (108, 118, 150)
FREE = (58, 62, 76)
INUSE = (96, 132, 200)
FAULTS = (240, 176, 66)
MARKER = (200, 130, 240)

LEVEL_COLOR = {
    "ok": RESIDENT,
    "mapped": MAPPED,
    "warn": FAULTS,
    "alarm": PAGED,
}


def _font(size: int):
    for name in ("segoeui.ttf", "DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024
    return f"{n:.1f} TB"


def _x_for(i: int, count: int, x0: int, x1: int) -> float:
    if count <= 1:
        return x1
    return x0 + (x1 - x0) * i / (count - 1)


def _panel(draw, box, title, font_s):
    x0, y0, x1, y1 = box
    draw.rectangle([x0, y0, x1, y1], fill=(24, 25, 30), outline=GRID)
    for f in (0.25, 0.5, 0.75):
        y = y0 + (y1 - y0) * f
        draw.line([x0 + 1, y, x1 - 1, y], fill=GRID)
    draw.text((x0 + 8, y0 + 5), title, font=font_s, fill=DIM)


def render_chart(history: list[dict], markers: list[dict], static: dict,
                 width: int = 1200, height: int = 720) -> Image.Image:
    """history: raw snapshots from Sampler.snapshot_history()."""
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    f_title = _font(24)
    f_big = _font(19)
    f_s = _font(13)

    if not history:
        d.text((30, 30), "MemXray: no samples yet", font=f_title, fill=TEXT)
        return img

    last = history[-1]
    n = len(history)
    span = max(1.0, last["t"] - history[0]["t"])

    # ---- header -------------------------------------------------------
    v = last.get("verdict", {"level": "ok", "text": ""})
    col = LEVEL_COLOR.get(v["level"], TEXT)
    d.text((24, 18), "ComfyUI Memory X-Ray", font=f_title, fill=TEXT)
    d.rectangle([24, 56, 24 + 14, 56 + 14], fill=col)
    d.text((46, 52), v["text"], font=f_big, fill=col)

    p = last["proc"]
    s = last["sys"]
    line2 = (
        f"in RAM {human(p['private_ws'])}   "
        f"committed {human(p['private_bytes'])}   "
        f"gap {human(p['paged_out'])} (evicted <= {human(p.get('evicted_max', 0))})   "
        f"mapped {human(p['mapped_ws'])}   "
        f"pagefile {human(s['pagefile_used'])} / {human(s['pagefile_size'])}   "
        f"hard reads/s {last['faults']['page_reads_sec']:.0f}"
    )
    d.text((24, 80), line2, font=f_s, fill=DIM)

    left, right = 70, width - 24
    panels = [
        (108, 300, "ComfyUI process private memory"),
        (318, 470, "System physical RAM composition"),
        (488, 600, "Hard faults (pages read from disk / sec)"),
    ]
    for (y0, y1, title) in panels:
        _panel(d, (left, y0, right, y1), title, f_s)

    def marker_lines():
        t0 = history[0]["t"]
        for m in markers:
            if m["t"] < t0 or m["t"] > last["t"]:
                continue
            frac = (m["t"] - t0) / span
            x = left + (right - left) * frac
            for (y0, y1, _t) in panels:
                d.line([x, y0 + 1, x, y1 - 1], fill=MARKER)
            d.text((x + 3, panels[0][0] + 3), m["label"][:24], font=f_s, fill=MARKER)

    # ---- panel 1: process private memory ------------------------------
    y0, y1 = panels[0][0], panels[0][1]
    peak = max(max(h["proc"]["private_bytes"] for h in history),
               max(h["proc"]["working_set"] for h in history), 1)
    scale = (y1 - y0 - 18) / peak

    def yv(val):
        return y1 - 2 - val * scale

    res_pts, com_pts, ws_pts, evi_pts = [], [], [], []
    for i, h in enumerate(history):
        x = _x_for(i, n, left + 1, right - 1)
        pw = h["proc"]["private_ws"]
        res_pts.append((x, yv(pw)))
        com_pts.append((x, yv(h["proc"]["private_bytes"])))
        ws_pts.append((x, yv(h["proc"]["working_set"])))
        evi_pts.append((x, yv(pw + h["proc"].get("evicted_max", 0))))

    # filled area under resident private memory
    d.polygon([(left + 1, y1 - 2)] + res_pts + [(right - 1, y1 - 2)],
              fill=(28, 66, 46))
    # Everything between resident and committed is simply not in RAM. Only the
    # lower slice of it can actually be on disk (bounded by pagefile usage);
    # the rest is commit that was never touched, so it is drawn fainter.
    d.polygon(res_pts + list(reversed(com_pts)), fill=(58, 32, 32))
    d.polygon(res_pts + list(reversed(evi_pts)), fill=(112, 40, 38))
    d.line(ws_pts, fill=MAPPED, width=1)
    d.line(res_pts, fill=RESIDENT, width=2)
    d.line(com_pts, fill=PAGED, width=2)
    d.text((right - 150, y0 + 5), human(peak), font=f_s, fill=DIM)
    d.text((left + 8, y0 + 20),
           "green = in RAM   bright red band = could be on disk   faint red = untouched commit   blue = incl. mapped files",
           font=f_s, fill=DIM)

    # ---- panel 2: system RAM composition -------------------------------
    # Stacked areas rather than one vertical line per sample: with a short
    # history a per-sample line is a single pixel and the panel reads as empty.
    y0, y1 = panels[1][0], panels[1][1]
    total = max(1, last["sys"]["phys_total"])
    h_px = y1 - y0 - 24
    xs = [_x_for(i, n, left + 1, right - 1) for i in range(n)]
    lower = [float(y1 - 2)] * n
    for key, color in (("in_use", INUSE), ("modified", PAGED),
                       ("standby", STANDBY), ("free", FREE)):
        upper = [
            lower[i] - (history[i]["sys"].get(key, 0) or 0) / total * h_px
            for i in range(n)
        ]
        pts = [(xs[i], upper[i]) for i in range(n)]
        pts += [(xs[i], lower[i]) for i in range(n - 1, -1, -1)]
        if n == 1:
            d.rectangle([xs[0] - 2, upper[0], xs[0] + 2, lower[0]], fill=color)
        else:
            d.polygon(pts, fill=color)
        lower = upper
    d.text((left + 8, y0 + 20),
           f"blue = in use   red = modified   grey = standby cache   dark = free    total {human(total)}",
           font=f_s, fill=DIM)

    # ---- panel 3: hard faults ------------------------------------------
    y0, y1 = panels[2][0], panels[2][1]
    fpeak = max(1.0, max(h["faults"]["page_reads_sec"] for h in history))
    fh = y1 - y0 - 24
    bw = max(1.0, (right - left - 2) / max(1, n)) * 0.9
    for i, h in enumerate(history):
        val = h["faults"]["page_reads_sec"]
        if val <= 0:
            continue
        x = xs[i]
        top = y1 - 2 - (val / fpeak) * fh
        # clamp, or the first and last bars hang outside the panel frame
        bx0 = max(left + 1, x - bw / 2)
        bx1 = min(right - 1, x + bw / 2)
        d.rectangle([bx0, top, bx1, y1 - 2], fill=FAULTS)
    d.text((right - 150, y0 + 5), f"peak {fpeak:.0f}/s", font=f_s, fill=DIM)

    marker_lines()

    # ---- footer: loaded models -----------------------------------------
    y = 616
    models = last.get("models", {})
    d.text((24, y), f"Loaded models: {models.get('count', 0)}   "
                    f"on device {human(models.get('on_device_bytes', 0))}   "
                    f"in system RAM {human(models.get('offloaded_bytes', 0))}",
           font=f_s, fill=TEXT)
    y += 20
    for m in (models.get("models") or [])[:4]:
        d.text((36, y),
               f"- {m['name']}  {human(m['total'])}  device={m['device']}  "
               f"offloaded={human(m['offloaded'])}",
               font=f_s, fill=DIM)
        y += 17

    d.text((24, height - 22),
           f"window {span:.0f}s   {n} samples   pid {static.get('pid')}   "
           f"source {static.get('pdh_process_set') or 'PSAPI'}",
           font=f_s, fill=DIM)
    return img
