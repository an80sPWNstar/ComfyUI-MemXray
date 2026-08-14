// MemXray front-end panel.
// Shows whether ComfyUI's cached models live in physical RAM or got pushed
// out to the Windows pagefile. Pure vanilla JS, no build step, no deps
// beyond ComfyUI's own app/api modules.
// Three levels up, not the usual two: this file is served from
// /extensions/ComfyUI-MemXray/js/, so ../../ would resolve to /extensions/
// and 404. The core scripts live at /scripts/.
import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

const STORE_KEY = "memxray.panel";
const MAX_POINTS = 900; // matches backend history_seconds (900s @ 1Hz)
// ---- design tokens ---------------------------------------------------------
// The panel is an x-ray plate: bright means dense means present in RAM, dark
// means absence, and color appears only where something is actually wrong.
// The previous version filled the whole chart with red whenever memory was
// merely non-resident, which screamed "problem" while the verdict said "fine".
//
// Series hues (resident / mapped) and the warning hue were validated together
// with the dataviz palette validator on this surface: all pairs pass the
// lightness band, chroma floor, CVD separation and contrast checks in both
// light and dark mode. Do not nudge these by eye - re-run the validator.
const COLORS = {
    plate: "#0d1014",      // chart ground
    surface: "#12161b",    // panel ground
    raised: "#19202a",     // controls, strip track
    line: "#232b34",       // hairlines
    ink: "#e4eaf0",
    ink2: "#93a1af",
    ink3: "#5f6d7a",

    resident: "#2b9cbb",       // series 1 - private memory in RAM
    residentLift: "#4fc3e8",   // same hue, lighter step, for the edge line
    residentFill: "rgba(43, 156, 187, 0.22)",
    mapped: "#8073e6",         // series 2 - file-backed / mmap'd pages
    evicted: "#bd811e",        // status: warning - could be on disk
    // Deliberately faint: this band is an upper bound (capped by the
    // system-wide pagefile), not a measurement of ComfyUI's own eviction. A
    // saturated slab here reads as "2 GB swapped!" when the honest claim is
    // "at most 2 GB, most of it probably another process".
    evictedFill: "rgba(189, 129, 30, 0.15)",
    critical: "#e2564a",       // status: critical - reserved, never a series
    voidFill: "rgba(99, 112, 126, 0.07)", // untouched commit: absence, not a category
    voidEdge: "#39424d",

    // system RAM composition: one hue ramp (occupied -> free), not a rainbow
    inUse: "#2b6f86",
    modified: "#bd811e",
    standby: "#39454f",
    free: "#171e25",   // just off the plate: free RAM is absence, but must
                       // still be tellable from the panel ground

    marker: "#8073e6",
    verdict: {
        ok: "#4fc3e8",
        mapped: "#8073e6",
        warn: "#bd811e",
        alarm: "#e2564a",
    },
};

// Windows-only pack, so Windows faces are fair game: Bahnschrift is the
// DIN-derived condensed grotesque that ships with Win10+, which gives the
// readout an instrument-panel voice without loading a webfont.
const FONT_LABEL = 'Bahnschrift, "Segoe UI Variable Small", "Segoe UI", sans-serif';
const FONT_NUM = 'Consolas, "Cascadia Mono", ui-monospace, monospace';

// ---- small formatting helpers -------------------------------------------

function num(v) {
    // never let a hole in the payload become NaN/undefined on screen
    return typeof v === "number" && isFinite(v) ? v : 0;
}

function human(bytes) {
    bytes = num(bytes);
    if (bytes === 0) return "0 B";
    const neg = bytes < 0;
    bytes = Math.abs(bytes);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) {
        bytes /= 1024;
        i++;
    }
    const digits = bytes >= 100 || i === 0 ? 0 : bytes >= 10 ? 1 : 2;
    return (neg ? "-" : "") + bytes.toFixed(digits) + " " + units[i];
}

function formatRate(bytesPerSec) {
    // "+12.4 MB/s" / "0 B/s" / "-3.1 MB/s" style, for pagefile_delta
    const v = num(bytesPerSec);
    const sign = v > 0 ? "+" : v < 0 ? "-" : "";
    return sign + human(Math.abs(v)) + "/s";
}

function niceCeil(v) {
    // round a max value up to a "nice" number so the y-axis doesn't jitter
    if (v <= 0) return 1;
    const mag = Math.pow(10, Math.floor(Math.log10(v)));
    const steps = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    for (const s of steps) {
        if (v <= s * mag) return s * mag;
    }
    return 10 * mag;
}

// ---- shared state ---------------------------------------------------------
// One rolling dataset feeds every visible surface (sidebar tab, floating
// panel) so we never fetch history twice or drift between views.

const state = {
    static: null,
    series: {
        t: [], private_ws: [], private_bytes: [], paged_out: [], mapped_ws: [],
        in_use: [], modified: [], standby: [], free: [], phys_total: [],
        page_reads_sec: [], evicted_max: [], pagefile_delta: [],
    },
    markers: [],
    windowSeconds: 300,
    pollTimer: null,
    visibleSurfaces: new Set(), // ids of surfaces currently on screen
    lastVerdict: null,
    lastStatus: "ok", // "ok" | "unreachable"
    surfaces: new Map(), // id -> { root, canvas, ctx, header refs... }
};

function surfaceCount() {
    return state.visibleSurfaces.size;
}

// ---- polling ---------------------------------------------------------------

async function backfillHistory() {
    try {
        const resp = await api.fetchApi(`/memxray/history?seconds=${state.windowSeconds}`);
        const data = await resp.json();
        const series = (data && data.series) || {};
        const t = Array.isArray(series.t) ? series.t : [];
        state.series.t = t.slice(-MAX_POINTS);
        const keys = ["private_ws", "private_bytes", "paged_out", "mapped_ws",
            "in_use", "modified", "standby", "free", "phys_total", "page_reads_sec",
            "evicted_max", "pagefile_delta"];
        for (const k of keys) {
            const arr = Array.isArray(series[k]) ? series[k] : [];
            state.series[k] = arr.slice(-MAX_POINTS);
        }
        state.markers = Array.isArray(data.markers) ? data.markers : [];
        state.lastStatus = "ok";
    } catch (err) {
        state.lastStatus = "unreachable";
    }
}

function appendSample(now) {
    const sys = (now && now.sys) || {};
    const proc = (now && now.proc) || {};
    const faults = (now && now.faults) || {};
    state.series.t.push(num(now && now.t) || Date.now() / 1000);
    state.series.private_ws.push(num(proc.private_ws));
    state.series.private_bytes.push(num(proc.private_bytes));
    state.series.paged_out.push(num(proc.paged_out));
    state.series.mapped_ws.push(num(proc.mapped_ws));
    state.series.in_use.push(num(sys.in_use));
    state.series.modified.push(num(sys.modified));
    state.series.standby.push(num(sys.standby));
    state.series.free.push(num(sys.free));
    state.series.phys_total.push(num(sys.phys_total));
    state.series.page_reads_sec.push(num(faults.page_reads_sec));
    state.series.evicted_max.push(num(proc.evicted_max));
    state.series.pagefile_delta.push(num(sys.pagefile_delta));

    for (const k of Object.keys(state.series)) {
        if (state.series[k].length > MAX_POINTS) {
            state.series[k].shift();
        }
    }
}

async function pollOnce() {
    let payload = null;
    try {
        const resp = await api.fetchApi("/memxray/stats");
        payload = await resp.json();
        state.lastStatus = "ok";
    } catch (err) {
        state.lastStatus = "unreachable";
    }
    if (payload) {
        state.static = payload.static || state.static;
        state.lastVerdict = (payload.now && payload.now.verdict) || state.lastVerdict;
        if (Array.isArray(payload.markers)) state.markers = payload.markers;
        appendSample(payload.now);
        state._lastNow = payload.now || null;
    }
    redrawAll();
}

function ensurePolling() {
    if (state.pollTimer) return;
    // Poll only while something is visible - no point hammering the
    // backend (and burning a PDH sample) when nobody is looking.
    pollOnce();
    state.pollTimer = setInterval(pollOnce, 1000);
}

function stopPollingIfHidden() {
    if (surfaceCount() > 0) return;
    if (state.pollTimer) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
    }
}

async function onSurfaceShown(id) {
    const firstShow = surfaceCount() === 0;
    state.visibleSurfaces.add(id);
    if (firstShow) {
        await backfillHistory();
    }
    ensurePolling();
    redrawAll();
}

function onSurfaceHidden(id) {
    state.visibleSurfaces.delete(id);
    stopPollingIfHidden();
}

// ---- canvas drawing ---------------------------------------------------------
// Three stacked panels sharing one x axis, oldest sample on the left.

const PAGEFILE_GROWTH_THRESHOLD = 1024 * 1024; // 1 MB/s, "the pagefile is growing"
const PAD_L = 10;
const PAD_R = 10;
const TITLE_BAND = 22;   // room for the title + legend, so text never sits on data
const AXIS_BAND = 16;    // time axis under the last panel

// The geometry of the last render, kept so pointer events can map an x back to
// a sample index without recomputing the layout.
let lastGeo = null;

function drawPanels(canvas, ctx) {
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 400;
    const cssH = canvas.clientHeight || 400;
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
        canvas.width = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    ctx.fillStyle = COLORS.plate;
    ctx.fillRect(0, 0, cssW, cssH);

    const t = state.series.t;
    const n = t.length;
    if (n < 2) {
        ctx.fillStyle = COLORS.ink3;
        ctx.font = `11px ${FONT_LABEL}`;
        ctx.fillText("ACQUIRING", PAD_L, cssH / 2);
        lastGeo = null;
        return;
    }

    const tMax = t[n - 1];
    const tMin = tMax - state.windowSeconds;
    let start = 0;
    while (start < n - 1 && t[start] < tMin) start++;

    const x0 = PAD_L;
    const x1 = cssW - PAD_R;
    const plotW = Math.max(10, x1 - x0);
    const xAt = (i) => x0 + ((t[i] - tMin) / Math.max(1, state.windowSeconds)) * plotW;

    const gap = 12;
    const usable = cssH - gap * 2 - AXIS_BAND;
    // Panel A carries the actual question, so it gets the space; system RAM is
    // context and stays a thin ribbon.
    const hA = Math.round(usable * 0.50);
    const hB = Math.round(usable * 0.20);
    const hC = usable - hA - hB;
    const panels = [
        { y0: 0, y1: hA },
        { y0: hA + gap, y1: hA + gap + hB },
        { y0: hA + hB + gap * 2, y1: hA + hB + gap * 2 + hC },
    ];

    lastGeo = { x0, x1, plotW, tMin, tMax, start, n, panels, cssW, cssH };

    drawPanelA(ctx, x0, x1, panels[0], start, n, xAt);
    drawPanelB(ctx, x0, x1, panels[1], start, n, xAt);
    drawPanelC(ctx, x0, x1, panels[2], start, n, xAt);
    drawTimeAxis(ctx, x0, x1, panels[2].y1, cssH);
    drawMarkers(ctx, panels, tMin, tMax, x0, plotW);
    drawCrosshair(ctx, panels, xAt, start, n);
}

// Title band: label left, legend right. Both live ABOVE the plot rect, which
// is the whole point - the old version drew them over the data.
function panelHeader(ctx, x0, x1, yTop, title, legend) {
    ctx.font = `10px ${FONT_LABEL}`;
    ctx.textBaseline = "middle";
    const midY = yTop + TITLE_BAND / 2;

    ctx.fillStyle = COLORS.ink2;
    ctx.letterSpacing = "0.08em";
    ctx.fillText(title.toUpperCase(), x0, midY);
    const titleW = ctx.measureText(title.toUpperCase()).width;
    ctx.letterSpacing = "0px";

    if (!legend || !legend.length) return;
    ctx.font = `10px ${FONT_LABEL}`;
    const items = legend.map((it) => ({ ...it, w: ctx.measureText(it.label).width + 16 }));
    // If the legend cannot fit beside the title, drop the least important
    // entries rather than overlapping or clipping mid-word.
    let shown = items;
    while (shown.length > 1 && totalWidth(shown) > x1 - x0 - titleW - 14) shown = shown.slice(0, -1);
    if (totalWidth(shown) > x1 - x0 - titleW - 14) return;

    let x = x1 - totalWidth(shown);
    for (const it of shown) {
        ctx.fillStyle = it.color;
        if (it.line) {
            ctx.fillRect(x, midY - 1, 9, 2);
        } else {
            roundRect(ctx, x, midY - 4, 9, 8, 2);
            ctx.fill();
        }
        ctx.fillStyle = COLORS.ink3;
        ctx.fillText(it.label, x + 13, midY);
        x += it.w;
    }
}

function totalWidth(items) {
    return items.reduce((a, it) => a + it.w, 0);
}

function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
}

// Hairline grid, one shade off the surface - never dashed.
function plotGrid(ctx, x0, x1, py0, py1) {
    ctx.strokeStyle = COLORS.line;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const f of [0.5, 1]) {
        const y = Math.round(py1 - (py1 - py0) * f) + 0.5;
        ctx.moveTo(x0, y);
        ctx.lineTo(x1, y);
    }
    ctx.moveTo(x0, Math.round(py1) + 0.5);
    ctx.lineTo(x1, Math.round(py1) + 0.5);
    ctx.stroke();
}

// Scale readout, right-aligned on its own plate so it never sits on a mark.
function scaleTag(ctx, x1, py0, text) {
    ctx.font = `9px ${FONT_NUM}`;
    ctx.textBaseline = "middle";
    const w = ctx.measureText(text).width;
    ctx.fillStyle = COLORS.plate;
    ctx.fillRect(x1 - w - 6, py0 + 1, w + 6, 12);
    ctx.fillStyle = COLORS.ink3;
    ctx.fillText(text, x1 - w - 2, py0 + 7);
}

function drawPanelA(ctx, x0, x1, panel, start, n, xAt) {
    const s = state.series;
    const py0 = panel.y0 + TITLE_BAND;
    const py1 = panel.y1;

    let maxV = 1;
    for (let i = start; i < n; i++) {
        maxV = Math.max(maxV, s.private_bytes[i], s.private_ws[i] + s.mapped_ws[i]);
    }
    maxV = niceCeil(maxV);
    const yOf = (v) => py1 - (num(v) / maxV) * (py1 - py0);

    panelHeader(ctx, x0, x1, panel.y0, "Process memory", [
        { label: "in RAM", color: COLORS.resident },
        { label: "evicted ≤", color: COLORS.evicted },
        { label: "untouched", color: COLORS.voidEdge },
        { label: "mapped", color: COLORS.mapped, line: true },
    ]);
    plotGrid(ctx, x0, x1, py0, py1);

    const evictedTop = (i) => {
        const gapV = Math.max(0, s.private_bytes[i] - s.private_ws[i]);
        return num(s.private_ws[i]) + Math.min(num(s.evicted_max[i]), gapV);
    };

    // Absence is drawn as absence: the untouched-commit band is a faint void,
    // not a red alarm. Only the evicted slice - memory that could genuinely be
    // on disk - earns the warning hue.
    fillBand(ctx, start, n, xAt, (i) => yOf(evictedTop(i)), (i) => yOf(s.private_bytes[i]), COLORS.voidFill);
    fillBand(ctx, start, n, xAt, (i) => yOf(s.private_ws[i]), (i) => yOf(evictedTop(i)), COLORS.evictedFill);

    // resident area - the dense part of the plate
    ctx.beginPath();
    ctx.moveTo(xAt(start), py1);
    for (let i = start; i < n; i++) ctx.lineTo(xAt(i), yOf(s.private_ws[i]));
    ctx.lineTo(xAt(n - 1), py1);
    ctx.closePath();
    ctx.fillStyle = COLORS.residentFill;
    ctx.fill();

    strokeSeries(ctx, start, n, xAt, (i) => yOf(s.private_bytes[i]), COLORS.voidEdge, 1);
    // Dashed, because this edge is a ceiling rather than a measured value -
    // the dash is the uncertainty, and it keeps the warning hue to a hairline
    // instead of a slab.
    ctx.setLineDash([3, 3]);
    strokeSeries(ctx, start, n, xAt, (i) => yOf(evictedTop(i)), COLORS.evicted, 1.5);
    ctx.setLineDash([]);
    strokeSeries(ctx, start, n, xAt, (i) => yOf(s.private_ws[i] + s.mapped_ws[i]), COLORS.mapped, 1.5);
    strokeSeries(ctx, start, n, xAt, (i) => yOf(s.private_ws[i]), COLORS.residentLift, 2);

    scaleTag(ctx, x1, py0, human(maxV));
}

function fillBand(ctx, start, n, xAt, topFn, bottomFn, fill) {
    ctx.beginPath();
    ctx.moveTo(xAt(start), topFn(start));
    for (let i = start; i < n; i++) ctx.lineTo(xAt(i), topFn(i));
    for (let i = n - 1; i >= start; i--) ctx.lineTo(xAt(i), bottomFn(i));
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
}

function strokeSeries(ctx, start, n, xAt, yFn, color, width) {
    ctx.beginPath();
    for (let i = start; i < n; i++) {
        const y = yFn(i);
        i === start ? ctx.moveTo(xAt(i), y) : ctx.lineTo(xAt(i), y);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineJoin = "round";
    ctx.stroke();
}

function drawPanelB(ctx, x0, x1, panel, start, n, xAt) {
    const s = state.series;
    const py0 = panel.y0 + TITLE_BAND;
    const py1 = panel.y1;
    const h = py1 - py0;

    panelHeader(ctx, x0, x1, panel.y0, "System RAM", [
        { label: "in use", color: COLORS.inUse },
        { label: "modified", color: COLORS.modified },
        { label: "standby", color: COLORS.standby },
        { label: "free", color: COLORS.free },
    ]);

    // Stacked areas, not one bar per sample: at 1 Hz over 15 minutes the bars
    // are sub-pixel and the panel reads as empty.
    const total = (i) => Math.max(1, s.phys_total[i]);
    let lower = null;
    const segs = [
        ["in_use", COLORS.inUse],
        ["modified", COLORS.modified],
        ["standby", COLORS.standby],
        ["free", COLORS.free],
    ];
    for (const [key, color] of segs) {
        const upper = [];
        for (let i = start; i < n; i++) {
            const base = lower ? lower[i - start] : py1;
            upper.push(base - (num(s[key][i]) / total(i)) * h);
        }
        ctx.beginPath();
        ctx.moveTo(xAt(start), upper[0]);
        for (let i = start; i < n; i++) ctx.lineTo(xAt(i), upper[i - start]);
        for (let i = n - 1; i >= start; i--) {
            ctx.lineTo(xAt(i), lower ? lower[i - start] : py1);
        }
        ctx.closePath();
        ctx.fillStyle = color;
        ctx.fill();
        lower = upper;
    }

    scaleTag(ctx, x1, py0, human(state.series.phys_total[n - 1]));
}

function drawPanelC(ctx, x0, x1, panel, start, n, xAt) {
    const s = state.series;
    const py0 = panel.y0 + TITLE_BAND;
    const py1 = panel.y1;
    const h = py1 - py0;

    let maxV = 1;
    for (let i = start; i < n; i++) maxV = Math.max(maxV, s.page_reads_sec[i]);
    maxV = niceCeil(maxV);

    panelHeader(ctx, x0, x1, panel.y0, "Hard faults / s", [
        { label: "pagefile", color: COLORS.critical },
        { label: "mapped file", color: COLORS.evicted },
    ]);
    plotGrid(ctx, x0, x1, py0, py1);

    // \Memory\Page Reads/sec fires for a fault against ANY backing store, so a
    // bar only earns the critical hue when that same sample's pagefile
    // actually grew. Amber means "disk, cause unproven".
    const barW = Math.max(1, (x1 - x0) / Math.max(1, n - start) - 1);
    for (let i = start; i < n; i++) {
        const v = num(s.page_reads_sec[i]);
        if (v <= 0) continue;
        const seg = (v / maxV) * h;
        const growing = num(s.pagefile_delta[i]) > PAGEFILE_GROWTH_THRESHOLD;
        ctx.fillStyle = growing ? COLORS.critical : COLORS.evicted;
        const bx = Math.max(x0, xAt(i) - barW / 2);
        const bw = Math.min(barW, x1 - bx);
        // rounded data-end, anchored to the baseline
        roundRect(ctx, bx, py1 - seg, Math.max(1, bw), seg, Math.min(2, bw / 2));
        ctx.fill();
    }

    scaleTag(ctx, x1, py0, `${maxV.toFixed(0)}/s`);
}

function drawTimeAxis(ctx, x0, x1, yTop, cssH) {
    ctx.font = `9px ${FONT_NUM}`;
    ctx.textBaseline = "middle";
    ctx.fillStyle = COLORS.ink3;
    const y = yTop + AXIS_BAND / 2;
    const win = state.windowSeconds;
    // Round to one decimal rather than to whole minutes: a 5m window's
    // midpoint is 2.5m, and rounding it to "-3m" mislabels the axis.
    const stamp = (secs) => {
        if (secs < 60) return `-${Math.round(secs)}s`;
        const m = secs / 60;
        return `-${m % 1 === 0 ? m.toFixed(0) : m.toFixed(1)}m`;
    };
    ctx.textAlign = "left";
    ctx.fillText(stamp(win), x0, y);
    ctx.textAlign = "center";
    ctx.fillText(stamp(win / 2), (x0 + x1) / 2, y);
    ctx.textAlign = "right";
    ctx.fillText("now", x1, y);
    ctx.textAlign = "left";
}

function drawMarkers(ctx, panels, tMin, tMax, x0, plotW) {
    const span = Math.max(1, tMax - tMin);
    ctx.font = `9px ${FONT_LABEL}`;
    ctx.textBaseline = "middle";
    for (const m of state.markers) {
        const mt = num(m && m.t);
        if (mt < tMin || mt > tMax) continue;
        const x = x0 + ((mt - tMin) / span) * plotW;
        ctx.strokeStyle = COLORS.marker;
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 3]);
        ctx.beginPath();
        for (const p of panels) {
            ctx.moveTo(x, p.y0 + TITLE_BAND);
            ctx.lineTo(x, p.y1);
        }
        ctx.stroke();
        ctx.setLineDash([]);
        const label = (m && m.label) || "";
        if (label) {
            // A marker dropped "now" sits at the right edge, where its label
            // would print straight through the scale tag. Flip it to the left
            // of its own line there, and plate it so it never sits on a mark.
            const lw = ctx.measureText(label).width;
            const x1 = x0 + plotW;
            const flip = x + 5 + lw > x1 - 52;
            const lx = flip ? x - 5 - lw : x + 5;
            const ly = panels[0].y0 + TITLE_BAND + 7;
            ctx.fillStyle = COLORS.plate;
            ctx.fillRect(lx - 2, ly - 6, lw + 4, 12);
            ctx.fillStyle = COLORS.marker;
            ctx.fillText(label, lx, ly);
        }
    }
}

function drawCrosshair(ctx, panels, xAt, start, n) {
    const hv = state.hover;
    if (!hv || hv.index == null || hv.index < start || hv.index >= n) return;
    const x = xAt(hv.index);
    ctx.strokeStyle = COLORS.ink3;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const p of panels) {
        ctx.moveTo(x, p.y0 + TITLE_BAND);
        ctx.lineTo(x, p.y1);
    }
    ctx.stroke();
}

// ---- header (plain DOM, text stays selectable) -----------------------------

// One word per level, so the eyebrow says the state and the sentence explains
// it. The backend's own text is rendered verbatim underneath.
const LEVEL_WORD = { ok: "in RAM", mapped: "in RAM", warn: "mixed", alarm: "pagefile" };

function updateHeader(surf) {
    const st = state.static;
    const now = state._lastNow;
    const verdict = state.lastVerdict;

    if (state.lastStatus === "unreachable") {
        surf.verdictRow.dataset.level = "warn";
        surf.verdictHead.textContent = "no signal";
        surf.verdictText.textContent = "backend unreachable - retrying every second";
        return;
    }

    if (verdict) {
        const level = verdict.level || "ok";
        surf.verdictRow.dataset.level = level;
        surf.verdictHead.textContent = LEVEL_WORD[level] || level;
        surf.verdictText.textContent = verdict.text || "";
    } else {
        surf.verdictRow.dataset.level = "";
        surf.verdictHead.textContent = "acquiring";
        surf.verdictText.textContent = "waiting for the first sample";
    }

    const proc = (now && now.proc) || {};
    const sys = (now && now.sys) || {};
    const models = (now && now.models) || {};

    const resident = num(proc.private_ws);
    const mapped = num(proc.mapped_ws);
    const gapV = num(proc.paged_out);
    const evicted = Math.min(num(proc.evicted_max), gapV);
    const untouched = Math.max(0, gapV - evicted);

    // Hero: the answer, big. Split value from unit so the unit stays quiet.
    const heroParts = human(resident).split(" ");
    surf.heroFigure.textContent = heroParts[0];
    surf.heroLabel.textContent = `${heroParts[1] || "B"} resident in RAM`;
    surf.heroAside.textContent = `of ${human(resident + gapV + mapped)} held`;

    // Density strip - flex-grow carries the proportion, so the segments always
    // sum to the whole without any width arithmetic.
    const total = Math.max(1, resident + evicted + untouched + mapped);
    const setSeg = (el, v) => {
        el.style.flexGrow = String(v / total);
        // a zero-width segment would still show its 2px gap; hide it instead
        el.style.display = v > 0 ? "" : "none";
    };
    setSeg(surf.segs.resident, resident);
    setSeg(surf.segs.evicted, evicted);
    setSeg(surf.segs.untouched, untouched);
    setSeg(surf.segs.mapped, mapped);

    surf.keys.resident.value.textContent = human(resident);
    surf.keys.evicted.value.textContent = human(evicted);
    surf.keys.untouched.value.textContent = human(untouched);
    surf.keys.mapped.value.textContent = human(mapped);
    // The evicted figure is a ceiling, not a measurement - say so where it is
    // read, not only in the docs.
    surf.keys.evicted.item.title = "upper bound: min(non-resident private bytes, system pagefile in use)";
    surf.keys.untouched.item.title = "committed address space that was never touched - never written to disk";

    surf.tiles.committed.value.textContent = human(proc.private_bytes);
    surf.tiles.pagefile.value.textContent = human(sys.pagefile_used);
    surf.tiles.pagefile.sub.textContent = `of ${human(sys.pagefile_size)}`;
    const reads = num(now && now.faults && now.faults.page_reads_sec);
    surf.tiles.hardReads.value.textContent = `${reads.toFixed(0)}/s`;
    // pagefile_delta is the one number that actually confirms pagefile growth
    // (page_reads_sec fires for mapped-file faults too), so it is the only
    // readout allowed to turn critical.
    const pfDelta = num(sys.pagefile_delta);
    const growing = pfDelta > 1024 * 1024;
    surf.tiles.pagefileTrend.value.textContent = formatRate(pfDelta);
    surf.tiles.pagefileTrend.value.dataset.state = growing ? "critical" : "";
    surf.tiles.hardReads.sub.textContent = growing ? "pagefile growing" : "disk, cause unproven";

    const modelList = Array.isArray(models.models) ? models.models : [];
    const gpus = Array.isArray(models.gpus) ? models.gpus : [];
    let modelsLine = `${num(models.count)} model(s) - on device ${human(models.on_device_bytes)} - offloaded ${human(models.offloaded_bytes)}`;
    if (gpus.length) {
        modelsLine += "  |  " + gpus.map((g) => `GPU${num(g.index)} ${human(g.used)}/${human(g.total)}`).join("  ");
    }
    surf.modelsLine.textContent = modelsLine;

    const pdhOff = st && st.pdh === false;
    const wsEstimated = proc.private_ws_estimated === true;
    if (pdhOff || wsEstimated) {
        const errTxt = (st && st.pdh_error) || "unknown";
        surf.caveat.style.display = "";
        surf.caveat.textContent = `private working set unavailable - figures approximate (PDH: ${errTxt})`;
    } else {
        surf.caveat.style.display = "none";
    }
}

// Maps a pointer x back to a sample index using the geometry of the last
// render, so the hit target is the whole column rather than the 1px mark.
function indexAtX(px) {
    const g = lastGeo;
    if (!g) return null;
    if (px < g.x0 - 6 || px > g.x1 + 6) return null;
    const t = state.series.t;
    const frac = (px - g.x0) / Math.max(1, g.plotW);
    const target = g.tMin + frac * Math.max(1, state.windowSeconds);
    let best = null;
    let bestD = Infinity;
    for (let i = g.start; i < g.n; i++) {
        const d = Math.abs(t[i] - target);
        if (d < bestD) { bestD = d; best = i; }
    }
    return best;
}

function updateTip(tip, wrap, hover) {
    if (!hover || hover.index == null) {
        tip.style.display = "none";
        return;
    }
    const i = hover.index;
    const s = state.series;
    const age = Math.max(0, Math.round(s.t[s.t.length - 1] - s.t[i]));
    const gapV = Math.max(0, num(s.private_bytes[i]) - num(s.private_ws[i]));
    const evicted = Math.min(num(s.evicted_max[i]), gapV);
    const rows = [
        ["in RAM", human(s.private_ws[i]), COLORS.residentLift],
        ["evicted ≤", human(evicted), COLORS.evicted],
        ["untouched", human(gapV - evicted), COLORS.voidEdge],
        ["mapped", human(s.mapped_ws[i]), COLORS.mapped],
        ["faults", `${num(s.page_reads_sec[i]).toFixed(0)}/s`,
            num(s.pagefile_delta[i]) > PAGEFILE_GROWTH_THRESHOLD ? COLORS.critical : COLORS.evicted],
    ];
    tip.innerHTML =
        `<div class="memxray-tip-time">${age === 0 ? "now" : `-${age}s`}</div>` +
        rows.map(([label, value, color]) =>
            `<div class="memxray-tip-row"><span style="color:${color}">${label}</span><b>${value}</b></div>`
        ).join("");

    tip.style.display = "block";
    // flip the tip to the other side of the cursor near the right/bottom edge
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    let x = hover.x + 14;
    let y = hover.y + 14;
    if (x + tw > w - 4) x = hover.x - tw - 14;
    if (y + th > h - 4) y = Math.max(4, hover.y - th - 14);
    tip.style.left = `${Math.max(4, x)}px`;
    tip.style.top = `${Math.max(4, y)}px`;
}

function redrawAll() {
    for (const surf of state.surfaces.values()) {
        if (!state.visibleSurfaces.has(surf.id)) continue;
        updateHeader(surf);
        try {
            drawPanels(surf.canvas, surf.ctx);
        } catch (err) {
            // a drawing error must never propagate into ComfyUI's own loop
            console.error("[MemXray] draw failed", err);
        }
    }
}

// ---- style injection (once) ------------------------------------------------

function injectStyle() {
    if (document.getElementById("memxray-style")) return;
    const style = document.createElement("style");
    style.id = "memxray-style";
    style.textContent = `
.memxray-root {
    --mx-plate: ${COLORS.plate};
    --mx-surface: ${COLORS.surface};
    --mx-raised: ${COLORS.raised};
    --mx-line: ${COLORS.line};
    --mx-ink: ${COLORS.ink};
    --mx-ink-2: ${COLORS.ink2};
    --mx-ink-3: ${COLORS.ink3};
    --mx-resident: ${COLORS.resident};
    --mx-resident-lift: ${COLORS.residentLift};
    --mx-mapped: ${COLORS.mapped};
    --mx-evicted: ${COLORS.evicted};
    --mx-critical: ${COLORS.critical};
    --mx-label: ${FONT_LABEL};
    --mx-num: ${FONT_NUM};

    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    background: var(--mx-surface);
    color: var(--mx-ink);
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: 12px;
    box-sizing: border-box;
    padding: 12px;
    gap: 10px;
}
.memxray-root * { box-sizing: border-box; }

/* --- verdict: a colored rule, not a colored blob ------------------------ */
.memxray-verdict-row {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 8px 10px 8px 0;
    border-left: 2px solid var(--mx-ink-3);
    padding-left: 10px;
    line-height: 1.35;
}
.memxray-verdict-row[data-level="ok"] { border-left-color: var(--mx-resident-lift); }
.memxray-verdict-row[data-level="mapped"] { border-left-color: var(--mx-mapped); }
.memxray-verdict-row[data-level="warn"] { border-left-color: var(--mx-evicted); }
.memxray-verdict-row[data-level="alarm"] { border-left-color: var(--mx-critical); }
.memxray-verdict-head {
    font-family: var(--mx-label);
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--mx-ink-3);
    display: block;
    margin-bottom: 2px;
}
.memxray-verdict-row[data-level="ok"] .memxray-verdict-head { color: var(--mx-resident-lift); }
.memxray-verdict-row[data-level="mapped"] .memxray-verdict-head { color: var(--mx-mapped); }
.memxray-verdict-row[data-level="warn"] .memxray-verdict-head { color: var(--mx-evicted); }
.memxray-verdict-row[data-level="alarm"] .memxray-verdict-head { color: var(--mx-critical); }
.memxray-verdict-text { font-size: 12.5px; color: var(--mx-ink); }
.memxray-dot { display: none; }

/* --- hero + density strip: the signature ------------------------------- */
.memxray-hero { display: flex; align-items: baseline; gap: 10px; }
.memxray-hero-figure {
    font-size: 30px;
    font-weight: 300;
    letter-spacing: -0.01em;
    line-height: 1;
    color: var(--mx-ink);
    font-variant-numeric: proportional-nums;
}
.memxray-hero-label {
    font-family: var(--mx-label);
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--mx-ink-3);
}
.memxray-hero-aside {
    margin-left: auto;
    font-family: var(--mx-num);
    font-size: 10.5px;
    color: var(--mx-ink-2);
    text-align: right;
    font-variant-numeric: tabular-nums;
}

.memxray-strip { display: flex; flex-direction: column; gap: 7px; }
.memxray-strip-track {
    display: flex;
    height: 16px;
    background: var(--mx-plate);
    border-radius: 3px;
    overflow: hidden;
    gap: 2px; /* 2px surface gap between segments, never a border */
}
.memxray-seg { min-width: 0; transition: flex-grow 300ms ease; }
.memxray-seg-resident { background: var(--mx-resident); }
.memxray-seg-evicted { background: var(--mx-evicted); }
.memxray-seg-untouched { background: ${COLORS.voidEdge}; }
.memxray-seg-mapped { background: var(--mx-mapped); }
.memxray-strip-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 14px;
    font-size: 10.5px;
    color: var(--mx-ink-2);
}
.memxray-key { display: flex; align-items: center; gap: 5px; white-space: nowrap; }
.memxray-key-swatch { width: 8px; height: 8px; border-radius: 2px; flex: none; }
.memxray-key-value {
    font-family: var(--mx-num);
    font-variant-numeric: tabular-nums;
    color: var(--mx-ink);
}
.memxray-key-muted { color: var(--mx-ink-3); }

/* --- secondary readouts ------------------------------------------------- */
.memxray-readouts {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 18px;
    padding-top: 8px;
    border-top: 1px solid var(--mx-line);
}
.memxray-readout { display: flex; flex-direction: column; gap: 2px; min-width: 62px; }
.memxray-readout-label {
    font-family: var(--mx-label);
    font-size: 9px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--mx-ink-3);
}
.memxray-readout-value {
    font-family: var(--mx-num);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    color: var(--mx-ink);
}
.memxray-readout-value[data-state="critical"] { color: var(--mx-critical); }
.memxray-readout-sub { font-size: 9px; color: var(--mx-ink-3); }

.memxray-models-line {
    font-family: var(--mx-num);
    font-size: 10px;
    color: var(--mx-ink-3);
    font-variant-numeric: tabular-nums;
}
.memxray-caveat {
    font-size: 10.5px;
    color: var(--mx-evicted);
    background: rgba(189, 129, 30, 0.10);
    border-left: 2px solid var(--mx-evicted);
    padding: 5px 8px;
}

/* --- controls ----------------------------------------------------------- */
.memxray-controls { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
/* ComfyUI ships global input styling that otherwise wins on specificity and
   renders this as a white box in the middle of a dark instrument panel. */
.memxray-root .memxray-controls input[type="text"] {
    background: var(--mx-plate) !important;
    border: 1px solid var(--mx-line) !important;
    color: var(--mx-ink) !important;
    border-radius: 3px;
    padding: 4px 7px;
    font-size: 11px;
    width: 96px;
    font-family: inherit;
    box-shadow: none;
}
.memxray-root .memxray-controls input[type="text"]::placeholder { color: var(--mx-ink-3); }
.memxray-controls input[type="text"]:focus-visible,
.memxray-controls button:focus-visible,
.memxray-controls select:focus-visible {
    outline: 2px solid var(--mx-resident-lift);
    outline-offset: 1px;
}
.memxray-controls button, .memxray-controls select {
    background: var(--mx-raised);
    border: 1px solid var(--mx-line);
    color: var(--mx-ink-2);
    border-radius: 3px;
    padding: 4px 10px;
    font-family: var(--mx-label);
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    cursor: pointer;
}
.memxray-controls button:hover, .memxray-controls select:hover {
    color: var(--mx-ink);
    border-color: var(--mx-ink-3);
}
.memxray-spacer { margin-left: auto; }

.memxray-canvas-wrap { flex: 1 1 auto; min-height: 240px; position: relative; }
.memxray-canvas-wrap canvas { width: 100%; height: 100%; display: block; border-radius: 3px; }
.memxray-tip {
    position: absolute;
    pointer-events: none;
    display: none;
    background: rgba(9, 12, 16, 0.96);
    border: 1px solid var(--mx-line);
    border-radius: 3px;
    padding: 7px 9px;
    font-family: var(--mx-num);
    font-size: 10.5px;
    font-variant-numeric: tabular-nums;
    color: var(--mx-ink-2);
    white-space: nowrap;
    z-index: 3;
    box-shadow: 0 4px 16px rgba(0,0,0,0.55);
}
.memxray-tip b { color: var(--mx-ink); font-weight: 500; }
.memxray-tip-row { display: flex; justify-content: space-between; gap: 14px; }
.memxray-tip-time {
    font-family: var(--mx-label);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--mx-ink-3);
    font-size: 9px;
    margin-bottom: 4px;
}

/* --- shells ------------------------------------------------------------- */
.memxray-float-panel {
    position: fixed;
    z-index: 9999;
    background: var(--mx-surface, #12161b);
    border: 1px solid #232b34;
    border-radius: 6px;
    box-shadow: 0 18px 50px rgba(0,0,0,0.62);
    display: none;
    flex-direction: column;
    min-width: 460px;
    min-height: 460px;
    width: 580px;
    height: 680px;
    overflow: hidden;
    resize: both;
}
.memxray-float-panel.memxray-visible { display: flex; }
.memxray-float-header {
    background: #12161b;
    padding: 9px 12px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: move;
    user-select: none;
    border-bottom: 1px solid #232b34;
    flex: none;
}
.memxray-float-title {
    font-family: ${FONT_LABEL};
    font-size: 11px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: #93a1af;
}
.memxray-float-close {
    background: transparent; border: none; color: #5f6d7a; cursor: pointer;
    font-size: 14px; line-height: 1; padding: 2px 6px;
}
.memxray-float-close:hover { color: #e2564a; }
.memxray-float-body { flex: 1; min-height: 0; }

.memxray-toggle-btn {
    position: fixed;
    right: 12px;
    bottom: 12px;
    z-index: 9998;
    background: #12161b;
    border: 1px solid #232b34;
    color: #93a1af;
    border-radius: 3px;
    padding: 7px 12px;
    font-family: ${FONT_LABEL};
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    cursor: pointer;
}
.memxray-toggle-btn:hover { color: #e4eaf0; border-color: #5f6d7a; }

@media (prefers-reduced-motion: reduce) {
    .memxray-seg { transition: none; }
}
`;
    document.head.appendChild(style);
}

// ---- surface construction ---------------------------------------------------
// One function builds the DOM for either the sidebar tab body or the
// floating panel body, so there is no duplicated markup/drawing code.

let surfaceCounter = 0;

function buildSurface(container) {
    const id = "surf-" + (surfaceCounter++);
    const root = document.createElement("div");
    root.className = "memxray-root";

    // Verdict: an eyebrow naming the state, then the sentence. The level is
    // carried by a left rule and the eyebrow color, so it never becomes a
    // saturated block competing with the data.
    const verdictRow = document.createElement("div");
    verdictRow.className = "memxray-verdict-row";
    verdictRow.setAttribute("role", "status");
    const verdictBody = document.createElement("div");
    const verdictHead = document.createElement("span");
    verdictHead.className = "memxray-verdict-head";
    verdictHead.textContent = "acquiring";
    const verdictText = document.createElement("span");
    verdictText.className = "memxray-verdict-text";
    verdictText.textContent = "waiting for the first sample";
    verdictBody.appendChild(verdictHead);
    verdictBody.appendChild(verdictText);
    verdictRow.appendChild(verdictBody);
    const dot = document.createElement("span"); // kept for older callers; hidden by CSS
    dot.className = "memxray-dot";
    verdictRow.appendChild(dot);

    // Hero: one number answers the question. Eight equal tiles answered none.
    const hero = document.createElement("div");
    hero.className = "memxray-hero";
    const heroFigure = document.createElement("span");
    heroFigure.className = "memxray-hero-figure";
    heroFigure.textContent = "--";
    const heroLabel = document.createElement("span");
    heroLabel.className = "memxray-hero-label";
    heroLabel.textContent = "resident in RAM";
    const heroAside = document.createElement("span");
    heroAside.className = "memxray-hero-aside";
    hero.appendChild(heroFigure);
    hero.appendChild(heroLabel);
    hero.appendChild(heroAside);

    // The signature element: everything ComfyUI holds, as one strip. Bright
    // means present in RAM; the dark segment is absence. Part-to-whole at a
    // glance, four segments, which is what a strip is actually good at.
    const strip = document.createElement("div");
    strip.className = "memxray-strip";
    const track = document.createElement("div");
    track.className = "memxray-strip-track";
    const segs = {};
    const legend = document.createElement("div");
    legend.className = "memxray-strip-legend";
    const keys = {};
    for (const [key, cls, label] of [
        ["resident", "memxray-seg-resident", "in RAM"],
        // the "<=" is load-bearing: this figure is a ceiling, not a measurement
        ["evicted", "memxray-seg-evicted", "evicted ≤"],
        ["untouched", "memxray-seg-untouched", "untouched"],
        ["mapped", "memxray-seg-mapped", "mapped"],
    ]) {
        const seg = document.createElement("div");
        seg.className = `memxray-seg ${cls}`;
        seg.style.flexGrow = "0";
        seg.title = label;
        track.appendChild(seg);
        segs[key] = seg;

        const item = document.createElement("span");
        item.className = "memxray-key";
        const sw = document.createElement("span");
        sw.className = `memxray-key-swatch ${cls}`;
        const name = document.createElement("span");
        name.textContent = label;
        const val = document.createElement("span");
        val.className = "memxray-key-value";
        val.textContent = "--";
        item.appendChild(sw);
        item.appendChild(name);
        item.appendChild(val);
        legend.appendChild(item);
        keys[key] = { value: val, item };
    }
    strip.appendChild(track);
    strip.appendChild(legend);

    // Secondary readouts - the numbers you check after the headline.
    const readouts = document.createElement("div");
    readouts.className = "memxray-readouts";
    function makeReadout(label) {
        const wrap = document.createElement("div");
        wrap.className = "memxray-readout";
        const l = document.createElement("span");
        l.className = "memxray-readout-label";
        l.textContent = label;
        const v = document.createElement("span");
        v.className = "memxray-readout-value";
        v.textContent = "--";
        const s = document.createElement("span");
        s.className = "memxray-readout-sub";
        wrap.appendChild(l);
        wrap.appendChild(v);
        wrap.appendChild(s);
        readouts.appendChild(wrap);
        return { root: wrap, value: v, sub: s };
    }
    const tiles = {
        committed: makeReadout("committed"),
        pagefile: makeReadout("pagefile"),
        pagefileTrend: makeReadout("trend"),
        hardReads: makeReadout("hard faults"),
    };
    // kept so updateHeader can address them uniformly
    tiles.inRam = { value: heroFigure, sub: heroAside, note: heroAside };
    tiles.pagedOut = { value: keys.untouched.value, sub: heroAside, note: heroAside };
    tiles.evictedMax = { value: keys.evicted.value, sub: heroAside };
    tiles.mapped = { value: keys.mapped.value, sub: heroAside };

    const modelsLine = document.createElement("div");
    modelsLine.className = "memxray-models-line";
    modelsLine.textContent = "models: -";

    const caveat = document.createElement("div");
    caveat.className = "memxray-caveat";
    caveat.style.display = "none";

    const controls = document.createElement("div");
    controls.className = "memxray-controls";

    const markInput = document.createElement("input");
    markInput.type = "text";
    markInput.placeholder = "label";
    markInput.value = "mark";

    const markBtn = document.createElement("button");
    markBtn.textContent = "Mark";
    markBtn.addEventListener("click", async () => {
        const label = markInput.value.trim() || "mark";
        try {
            await api.fetchApi("/memxray/marker", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ label, note: "" }),
            });
        } catch (err) {
            console.error("[MemXray] marker post failed", err);
        }
    });

    const clearBtn = document.createElement("button");
    clearBtn.textContent = "Clear marks";
    clearBtn.addEventListener("click", async () => {
        try {
            await api.fetchApi("/memxray/clear_markers", { method: "POST" });
            state.markers = [];
        } catch (err) {
            console.error("[MemXray] clear markers failed", err);
        }
    });

    const windowSelect = document.createElement("select");
    for (const [label, secs] of [["60s", 60], ["5m", 300], ["15m", 900]]) {
        const opt = document.createElement("option");
        opt.value = String(secs);
        opt.textContent = label;
        if (secs === state.windowSeconds) opt.selected = true;
        windowSelect.appendChild(opt);
    }
    windowSelect.addEventListener("change", async () => {
        state.windowSeconds = parseInt(windowSelect.value, 10) || 300;
        await backfillHistory();
        redrawAll();
    });

    controls.appendChild(markInput);
    controls.appendChild(markBtn);
    controls.appendChild(clearBtn);
    controls.appendChild(windowSelect);

    const canvasWrap = document.createElement("div");
    canvasWrap.className = "memxray-canvas-wrap";
    const canvas = document.createElement("canvas");
    canvasWrap.appendChild(canvas);
    const ctx = canvas.getContext("2d");

    // Hover layer: a crosshair plus a readout of that sample. Tooltips
    // enhance here, they never gate - every value is also on the strip.
    const tip = document.createElement("div");
    tip.className = "memxray-tip";
    canvasWrap.appendChild(tip);

    canvas.addEventListener("pointermove", (e) => {
        const rect = canvas.getBoundingClientRect();
        const px = e.clientX - rect.left;
        const py = e.clientY - rect.top;
        const idx = indexAtX(px);
        state.hover = idx == null ? null : { index: idx, x: px, y: py };
        updateTip(tip, canvasWrap, state.hover);
        try {
            drawPanels(canvas, ctx);
        } catch (err) {
            console.error("[MemXray] hover redraw failed", err);
        }
    });
    canvas.addEventListener("pointerleave", () => {
        state.hover = null;
        tip.style.display = "none";
        try {
            drawPanels(canvas, ctx);
        } catch (err) {
            console.error("[MemXray] hover redraw failed", err);
        }
    });

    root.appendChild(verdictRow);
    root.appendChild(hero);
    root.appendChild(strip);
    root.appendChild(readouts);
    root.appendChild(modelsLine);
    root.appendChild(caveat);
    root.appendChild(controls);
    root.appendChild(canvasWrap);
    container.appendChild(root);

    // redraw when the surface itself changes size (e.g. floating panel resize)
    const resizeObserver = new ResizeObserver(() => {
        if (state.visibleSurfaces.has(id)) {
            try {
                drawPanels(canvas, ctx);
            } catch (err) {
                console.error("[MemXray] resize redraw failed", err);
            }
        }
    });
    resizeObserver.observe(canvasWrap);

    const surf = {
        id, root, canvas, ctx,
        verdictDot: dot, verdictText, verdictRow, verdictHead,
        heroFigure, heroLabel, heroAside,
        segs, keys, tip,
        tiles, modelsLine, caveat,
        resizeObserver,
    };
    state.surfaces.set(id, surf);
    return surf;
}

// ---- floating draggable/resizable panel ------------------------------------

function loadPanelPrefs() {
    try {
        const raw = localStorage.getItem(STORE_KEY);
        if (!raw) return {};
        return JSON.parse(raw) || {};
    } catch (err) {
        return {};
    }
}

function savePanelPrefs(prefs) {
    try {
        localStorage.setItem(STORE_KEY, JSON.stringify(prefs));
    } catch (err) {
        // storage full/unavailable - not fatal, panel just won't remember state
    }
}

function createFloatingPanel() {
    injectStyle();
    const prefs = loadPanelPrefs();

    const panel = document.createElement("div");
    panel.className = "memxray-float-panel";
    panel.style.left = (prefs.x != null ? prefs.x : 80) + "px";
    panel.style.top = (prefs.y != null ? prefs.y : 80) + "px";
    panel.style.width = (prefs.w || 560) + "px";
    panel.style.height = (prefs.h || 620) + "px";

    const header = document.createElement("div");
    header.className = "memxray-float-header";
    const title = document.createElement("span");
    title.className = "memxray-float-title";
    title.textContent = "Mem X-Ray";
    const closeBtn = document.createElement("button");
    closeBtn.className = "memxray-float-close";
    closeBtn.textContent = "✕";
    header.appendChild(title);
    header.appendChild(closeBtn);

    const body = document.createElement("div");
    body.className = "memxray-float-body";

    panel.appendChild(header);
    panel.appendChild(body);
    document.body.appendChild(panel);

    const surf = buildSurface(body);

    function persist() {
        const rect = panel.getBoundingClientRect();
        savePanelPrefs({
            visible: panel.classList.contains("memxray-visible"),
            x: rect.left, y: rect.top, w: rect.width, h: rect.height,
        });
    }

    function show() {
        panel.classList.add("memxray-visible");
        onSurfaceShown(surf.id);
        persist();
    }
    function hide() {
        panel.classList.remove("memxray-visible");
        onSurfaceHidden(surf.id);
        persist();
    }
    function toggle() {
        if (panel.classList.contains("memxray-visible")) hide();
        else show();
    }

    closeBtn.addEventListener("click", hide);

    // drag via header
    let dragging = false, dragDX = 0, dragDY = 0;
    header.addEventListener("pointerdown", (e) => {
        if (e.target === closeBtn) return;
        dragging = true;
        const rect = panel.getBoundingClientRect();
        dragDX = e.clientX - rect.left;
        dragDY = e.clientY - rect.top;
        header.setPointerCapture(e.pointerId);
    });
    header.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        panel.style.left = Math.max(0, e.clientX - dragDX) + "px";
        panel.style.top = Math.max(0, e.clientY - dragDY) + "px";
    });
    header.addEventListener("pointerup", (e) => {
        if (!dragging) return;
        dragging = false;
        try { header.releasePointerCapture(e.pointerId); } catch (err) { /* noop */ }
        persist();
    });

    // CSS `resize: both` handles the corner grip since the panel is
    // position:fixed with a normal block child; ResizeObserver on the
    // canvas wrap (set up in buildSurface) keeps the canvas sized to match.
    const panelResizeObserver = new ResizeObserver(() => persist());
    panelResizeObserver.observe(panel);

    if (prefs.visible) show();

    return { panel, show, hide, toggle };
}

function createToggleButton(onClick) {
    const btn = document.createElement("button");
    btn.className = "memxray-toggle-btn";
    btn.textContent = "Mem X-Ray";
    btn.title = "Toggle Mem X-Ray panel (Alt+M)";
    btn.addEventListener("click", onClick);
    document.body.appendChild(btn);
    return btn;
}

// ---- extension registration -------------------------------------------------

// Declared before registerExtension, not after: the command callback closes
// over this binding, and a `let` declared below the registration sits in the
// temporal dead zone for anything that fires during module evaluation.
let floatingPanelApi = null;

app.registerExtension({
    name: "MemXray.Panel",

    commands: [
        {
            id: "MemXray.TogglePanel",
            label: "Toggle Mem X-Ray Panel",
            function: () => {
                if (floatingPanelApi) floatingPanelApi.toggle();
            },
        },
    ],

    keybindings: [
        {
            commandId: "MemXray.TogglePanel",
            combo: { key: "m", alt: true },
        },
    ],

    async setup() {
        injectStyle();

        // Always provide the floating panel + toggle button, regardless of
        // whether a sidebar tab is also available.
        floatingPanelApi = createFloatingPanel();
        createToggleButton(() => floatingPanelApi.toggle());

        // The declarative `keybindings` block above only binds on ComfyUI
        // builds that expose a keybinding store; this one registers the
        // command but never wires the combo, so Alt+M did nothing. A plain
        // listener is the portable path. Ignored while typing in a field.
        window.addEventListener("keydown", (e) => {
            if (!e.altKey || e.ctrlKey || e.metaKey) return;
            if ((e.key || "").toLowerCase() !== "m") return;
            const t = e.target;
            const tag = t && t.tagName ? t.tagName.toLowerCase() : "";
            if (tag === "input" || tag === "textarea" || (t && t.isContentEditable)) return;
            e.preventDefault();
            if (floatingPanelApi) floatingPanelApi.toggle();
        });

        // Sidebar tab is a bonus surface when the host ComfyUI build
        // supports it; older builds simply skip this block.
        if (app.extensionManager && typeof app.extensionManager.registerSidebarTab === "function") {
            let sidebarSurf = null;
            app.extensionManager.registerSidebarTab({
                id: "memxray",
                icon: "pi pi-chart-line",
                title: "Mem X-Ray",
                tooltip: "ComfyUI memory: RAM vs pagefile",
                type: "custom",
                render: (el) => {
                    sidebarSurf = buildSurface(el);
                    onSurfaceShown(sidebarSurf.id);
                    // sidebar tabs don't give us a hide callback directly;
                    // an IntersectionObserver on the mount element covers
                    // both "tab switched away" and "panel closed".
                    const io = new IntersectionObserver((entries) => {
                        for (const entry of entries) {
                            if (entry.isIntersecting) onSurfaceShown(sidebarSurf.id);
                            else onSurfaceHidden(sidebarSurf.id);
                        }
                    });
                    io.observe(el);
                },
            });
        }
    },
});
