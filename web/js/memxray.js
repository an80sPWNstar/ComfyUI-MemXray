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
const COLORS = {
    privateWs: "#46be78",
    privateBytes: "#e2544a",
    pagedBand: "rgba(226, 84, 74, 0.28)",
    // evicted_max sub-band: the portion of the paged-out gap that could
    // actually be on disk (bounded by system pagefile_used), drawn solid
    // and darker so it reads as "the real part" against the lighter,
    // merely-committed-but-untouched remainder above it.
    evictedBand: "rgba(122, 32, 27, 0.85)",
    workingSet: "#569ae8",
    inUse: "#6084c8",
    modified: "#e2544a",
    standby: "#6c7696",
    free: "#3a3e4c",
    hardReads: "#f0b042",
    hardReadsPagefile: "#e2544a", // bar color when the sample's pagefile actually grew
    marker: "#c882f0",
    verdict: {
        ok: "#46be78",
        mapped: "#569ae8",
        warn: "#f0b042",
        alarm: "#e2544a",
    },
};

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
    ctx.fillStyle = "#20232b";
    ctx.fillRect(0, 0, cssW, cssH);

    const t = state.series.t;
    const n = t.length;
    if (n < 2) {
        ctx.fillStyle = "#888";
        ctx.font = "12px sans-serif";
        ctx.fillText("waiting for samples...", 10, cssH / 2);
        return;
    }

    // window slice: only draw the trailing windowSeconds worth of samples
    const tMax = t[n - 1];
    const tMin = tMax - state.windowSeconds;
    let start = 0;
    while (start < n - 1 && t[start] < tMin) start++;
    const count = n - start;
    const xAt = (i) => ((t[i] - tMin) / Math.max(1, state.windowSeconds)) * cssW;

    const gap = 6;
    const hA = Math.round(cssH * 0.42);
    const hB = Math.round(cssH * 0.32);
    const hC = cssH - hA - hB - gap * 2;
    const yA0 = 0, yA1 = hA;
    const yB0 = hA + gap, yB1 = yB0 + hB;
    const yC0 = yB1 + gap, yC1 = yC0 + hC;

    drawPanelA(ctx, cssW, yA0, yA1, start, n, xAt);
    drawPanelB(ctx, cssW, yB0, yB1, start, n, xAt);
    drawPanelC(ctx, cssW, yC0, yC1, start, n, xAt);
    drawMarkers(ctx, cssW, 0, cssH, tMin, tMax);

    // panel separators
    ctx.strokeStyle = "#3a3e4c";
    ctx.beginPath();
    ctx.moveTo(0, yB0 - gap / 2); ctx.lineTo(cssW, yB0 - gap / 2);
    ctx.moveTo(0, yC0 - gap / 2); ctx.lineTo(cssW, yC0 - gap / 2);
    ctx.stroke();
}

// Wraps rather than running off the edge: the panel is user-resizable and the
// legends are long, so at the 560px default width they were being clipped
// mid-word ("...cause unpr").
function panelLabel(ctx, text, x, y, maxWidth) {
    ctx.fillStyle = "#9aa0ad";
    ctx.font = "11px sans-serif";
    if (!maxWidth || ctx.measureText(text).width <= maxWidth) {
        ctx.fillText(text, x, y);
        return y;
    }
    // legends separate their clauses with runs of spaces; break on those
    const parts = text.split(/\s{2,}/);
    let line = "";
    let ly = y;
    for (const part of parts) {
        const candidate = line ? `${line}   ${part}` : part;
        if (line && ctx.measureText(candidate).width > maxWidth) {
            ctx.fillText(line, x, ly);
            ly += 12;
            line = part;
        } else {
            line = candidate;
        }
    }
    if (line) ctx.fillText(line, x, ly);
    return ly;
}

function drawPanelA(ctx, w, y0, y1, start, n, xAt) {
    const h = y1 - y0;
    const s = state.series;
    // scale off the larger of private_bytes and the combined working set,
    // since either line can be the tallest depending on load state
    let maxV = 1;
    for (let i = start; i < n; i++) {
        maxV = Math.max(maxV, s.private_bytes[i], s.private_ws[i] + s.mapped_ws[i]);
    }
    maxV = niceCeil(maxV);
    const yOf = (v) => y1 - (v / maxV) * (h - 16);

    // the band between private_bytes (committed) and private_ws (resident)
    // IS the paged-out memory - this is the headline of the whole tool, so
    // it gets filled first, underneath everything else.
    ctx.beginPath();
    ctx.moveTo(xAt(start), yOf(s.private_ws[start]));
    for (let i = start; i < n; i++) ctx.lineTo(xAt(i), yOf(s.private_ws[i]));
    for (let i = n - 1; i >= start; i--) ctx.lineTo(xAt(i), yOf(s.private_bytes[i]));
    ctx.closePath();
    ctx.fillStyle = COLORS.pagedBand;
    ctx.fill();

    // sub-band at the bottom of the gap: evicted_max is min(paged_out,
    // system pagefile_used) - the part of the gap that could actually be
    // on disk. Drawn solid/darker so it reads as "the real part"; the
    // lighter band above it (already filled) is committed address space
    // the process never touched, which is NOT proof of pagefile use.
    ctx.beginPath();
    ctx.moveTo(xAt(start), yOf(s.private_ws[start]));
    for (let i = start; i < n; i++) ctx.lineTo(xAt(i), yOf(s.private_ws[i]));
    for (let i = n - 1; i >= start; i--) {
        const evicted = Math.min(num(s.evicted_max[i]), Math.max(0, s.private_bytes[i] - s.private_ws[i]));
        ctx.lineTo(xAt(i), yOf(s.private_ws[i] + evicted));
    }
    ctx.closePath();
    ctx.fillStyle = COLORS.evictedBand;
    ctx.fill();

    // private_ws filled area (resident, "actually in RAM")
    ctx.beginPath();
    ctx.moveTo(xAt(start), y1);
    for (let i = start; i < n; i++) ctx.lineTo(xAt(i), yOf(s.private_ws[i]));
    ctx.lineTo(xAt(n - 1), y1);
    ctx.closePath();
    ctx.fillStyle = "rgba(70, 190, 120, 0.25)";
    ctx.fill();

    // total working set incl. mapped files (thin blue line)
    ctx.beginPath();
    for (let i = start; i < n; i++) {
        const v = s.private_ws[i] + s.mapped_ws[i];
        i === start ? ctx.moveTo(xAt(i), yOf(v)) : ctx.lineTo(xAt(i), yOf(v));
    }
    ctx.strokeStyle = COLORS.workingSet;
    ctx.lineWidth = 1;
    ctx.stroke();

    // private_bytes line (committed)
    ctx.beginPath();
    for (let i = start; i < n; i++) {
        i === start ? ctx.moveTo(xAt(i), yOf(s.private_bytes[i])) : ctx.lineTo(xAt(i), yOf(s.private_bytes[i]));
    }
    ctx.strokeStyle = COLORS.privateBytes;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // private_ws line on top for a crisp edge
    ctx.beginPath();
    for (let i = start; i < n; i++) {
        i === start ? ctx.moveTo(xAt(i), yOf(s.private_ws[i])) : ctx.lineTo(xAt(i), yOf(s.private_ws[i]));
    }
    ctx.strokeStyle = COLORS.privateWs;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    panelLabel(ctx, `ComfyUI process private memory (incl. mapped files) - max ${human(maxV)}`, 4, y0 + 12);
    panelLabel(ctx, "dark red = evicted (<= pagefile used)   light red = untouched commit", 4, y0 + 24, w - 8);
}

function drawPanelB(ctx, w, y0, y1, start, n, xAt) {
    const h = y1 - y0;
    const s = state.series;
    const count = n - start;
    const barW = Math.max(1, w / Math.max(1, count));
    for (let i = start; i < n; i++) {
        const total = Math.max(1, s.phys_total[i]);
        const inUse = num(s.in_use[i]) / total;
        const modified = num(s.modified[i]) / total;
        const standby = num(s.standby[i]) / total;
        const free = num(s.free[i]) / total;
        const x = xAt(i);
        let y = y1;
        const segs = [
            [inUse, COLORS.inUse],
            [modified, COLORS.modified],
            [standby, COLORS.standby],
            [free, COLORS.free],
        ];
        for (const [frac, color] of segs) {
            const seg = frac * (h - 14);
            ctx.fillStyle = color;
            ctx.fillRect(x, y - seg, Math.max(1, barW), seg);
            y -= seg;
        }
    }
    panelLabel(ctx, "System physical RAM (in-use / modified / standby / free)", 4, y0 + 12);
}

function drawPanelC(ctx, w, y0, y1, start, n, xAt) {
    const h = y1 - y0;
    const s = state.series;
    let maxV = 1;
    for (let i = start; i < n; i++) maxV = Math.max(maxV, s.page_reads_sec[i]);
    maxV = niceCeil(maxV);
    const count = n - start;
    const barW = Math.max(1, w / Math.max(1, count));
    // \Memory\Page Reads/sec (the counter behind page_reads_sec) fires for
    // ANY fault against backing store - the pagefile AND memory-mapped
    // files. torch/mmap loading a checkpoint spikes this with zero
    // pagefile growth, so a bar only turns red (implicating the pagefile)
    // when that sample's pagefile_delta also grew by more than 1 MB/s -
    // amber otherwise, meaning "disk activity, cause unproven".
    const PAGEFILE_GROWTH_THRESHOLD = 1024 * 1024; // 1 MB/s
    for (let i = start; i < n; i++) {
        const v = num(s.page_reads_sec[i]);
        const seg = (v / maxV) * (h - 14);
        const growing = num(s.pagefile_delta[i]) > PAGEFILE_GROWTH_THRESHOLD;
        ctx.fillStyle = growing ? COLORS.hardReadsPagefile : COLORS.hardReads;
        ctx.fillRect(xAt(i), y1 - seg, Math.max(1, barW), seg);
    }
    panelLabel(ctx, `Hard faults - memory served from disk (pagefile OR mapped files) - peak ${maxV.toFixed(0)}/s`, 4, y0 + 12);
    panelLabel(ctx, "red bar = faulting AND pagefile grew (implicates the pagefile)   amber = disk activity, cause unproven", 4, y0 + 24, w - 8);
}

function drawMarkers(ctx, w, y0, y1, tMin, tMax) {
    const span = Math.max(1, tMax - tMin);
    ctx.strokeStyle = COLORS.marker;
    ctx.fillStyle = COLORS.marker;
    ctx.font = "10px sans-serif";
    ctx.lineWidth = 1;
    for (const m of state.markers) {
        const mt = num(m && m.t);
        if (mt < tMin || mt > tMax) continue;
        const x = ((mt - tMin) / span) * w;
        ctx.beginPath();
        ctx.moveTo(x, y0);
        ctx.lineTo(x, y1);
        ctx.stroke();
        const label = (m && m.label) || "";
        if (label) ctx.fillText(label, x + 3, y0 + 10);
    }
}

// ---- header (plain DOM, text stays selectable) -----------------------------

function updateHeader(surf) {
    const st = state.static;
    const now = state._lastNow;
    const verdict = state.lastVerdict;

    if (state.lastStatus === "unreachable") {
        surf.verdictText.textContent = "backend unreachable";
        surf.verdictDot.style.background = COLORS.verdict.warn;
        return;
    }

    if (verdict) {
        const level = verdict.level || "ok";
        surf.verdictText.textContent = verdict.text || "";
        surf.verdictDot.style.background = COLORS.verdict[level] || COLORS.verdict.ok;
    } else {
        surf.verdictText.textContent = "waiting for data...";
        surf.verdictDot.style.background = "#6c7696";
    }

    const proc = (now && now.proc) || {};
    const sys = (now && now.sys) || {};
    const models = (now && now.models) || {};

    surf.tiles.inRam.value.textContent = human(proc.private_ws);
    surf.tiles.pagedOut.value.textContent = human(proc.paged_out);
    surf.tiles.pagedOut.sub.textContent = `peak ${human(proc.paged_out_peak)}`;
    // paged_out is just committed-minus-resident - it is NOT proof memory
    // reached the pagefile. Flag it when most of the gap is untouched
    // commit rather than real eviction, so the number doesn't get
    // misread as "this much hit disk".
    const pagedOutV = num(proc.paged_out);
    const untouchedV = num(proc.untouched_commit);
    if (pagedOutV > 0 && untouchedV > pagedOutV / 2) {
        surf.tiles.pagedOut.note.textContent = "mostly untouched commit";
        surf.tiles.pagedOut.note.style.display = "";
    } else {
        surf.tiles.pagedOut.note.style.display = "none";
    }
    surf.tiles.evictedMax.value.textContent = human(proc.evicted_max);
    surf.tiles.evictedMax.sub.textContent = "upper bound";
    surf.tiles.mapped.value.textContent = human(proc.mapped_ws);
    surf.tiles.committed.value.textContent = human(proc.private_bytes);
    surf.tiles.pagefile.value.textContent = `${human(sys.pagefile_used)} / ${human(sys.pagefile_size)}`;
    surf.tiles.hardReads.value.textContent = `${num(now && now.faults && now.faults.page_reads_sec).toFixed(1)}/s`;
    // pagefile_delta is the one number that actually confirms pagefile
    // growth (as opposed to page_reads_sec, which fires for mapped-file
    // faults too) - red only when it is growing meaningfully.
    const pfDelta = num(sys.pagefile_delta);
    surf.tiles.pagefileTrend.value.textContent = formatRate(pfDelta);
    surf.tiles.pagefileTrend.value.style.color = pfDelta > 1024 * 1024 ? COLORS.privateBytes : "";

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
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    background: #20232b;
    color: #d8dae0;
    font-family: sans-serif;
    font-size: 12px;
    box-sizing: border-box;
    padding: 8px;
    gap: 6px;
}
.memxray-root * { box-sizing: border-box; }
.memxray-verdict-row { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 600; }
.memxray-dot { width: 10px; height: 10px; border-radius: 50%; background: #6c7696; flex: none; }
.memxray-tiles { display: flex; flex-wrap: wrap; gap: 6px; }
.memxray-tile {
    background: #2a2e38;
    border: 1px solid #3a3e4c;
    border-radius: 4px;
    padding: 4px 8px;
    min-width: 84px;
}
.memxray-tile .memxray-tile-label { font-size: 10px; color: #9aa0ad; }
.memxray-tile .memxray-tile-value { font-size: 13px; font-weight: 600; }
.memxray-tile .memxray-tile-sub { font-size: 9px; color: #7a8090; }
.memxray-tile .memxray-tile-note { font-style: italic; }
.memxray-models-line { font-size: 11px; color: #b8bcc6; }
.memxray-caveat {
    font-size: 10px;
    color: #f0b042;
    background: rgba(240, 176, 66, 0.12);
    border: 1px solid rgba(240, 176, 66, 0.4);
    border-radius: 3px;
    padding: 3px 6px;
}
.memxray-controls { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.memxray-controls input[type="text"] {
    background: #2a2e38; border: 1px solid #3a3e4c; color: #d8dae0;
    border-radius: 3px; padding: 3px 6px; font-size: 11px; width: 90px;
}
.memxray-controls button, .memxray-controls select {
    background: #34384a; border: 1px solid #454a5e; color: #d8dae0;
    border-radius: 3px; padding: 3px 8px; font-size: 11px; cursor: pointer;
}
.memxray-controls button:hover { background: #3d4256; }
.memxray-canvas-wrap { flex: 1; min-height: 0; position: relative; }
.memxray-canvas-wrap canvas { width: 100%; height: 100%; display: block; }

.memxray-float-panel {
    position: fixed;
    z-index: 9999;
    background: #20232b;
    border: 1px solid #3a3e4c;
    border-radius: 6px;
    box-shadow: 0 6px 24px rgba(0,0,0,0.5);
    display: none;
    flex-direction: column;
    min-width: 420px;
    min-height: 420px;
    width: 560px;
    height: 620px;
    overflow: hidden;
    resize: both;
}
.memxray-float-panel.memxray-visible { display: flex; }
.memxray-float-header {
    background: #2a2e38;
    padding: 6px 10px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: move;
    user-select: none;
    border-bottom: 1px solid #3a3e4c;
    flex: none;
}
.memxray-float-title { font-weight: 600; font-size: 12px; }
.memxray-float-close {
    background: transparent; border: none; color: #b8bcc6; cursor: pointer;
    font-size: 14px; line-height: 1; padding: 2px 6px;
}
.memxray-float-close:hover { color: #e2544a; }
.memxray-float-body { flex: 1; min-height: 0; }

.memxray-toggle-btn {
    position: fixed;
    right: 12px;
    bottom: 12px;
    z-index: 9998;
    background: #34384a;
    border: 1px solid #454a5e;
    color: #d8dae0;
    border-radius: 20px;
    padding: 6px 12px;
    font-size: 11px;
    cursor: pointer;
    opacity: 0.85;
}
.memxray-toggle-btn:hover { opacity: 1; }
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

    const verdictRow = document.createElement("div");
    verdictRow.className = "memxray-verdict-row";
    const dot = document.createElement("span");
    dot.className = "memxray-dot";
    const verdictText = document.createElement("span");
    verdictText.textContent = "waiting for data...";
    verdictRow.appendChild(dot);
    verdictRow.appendChild(verdictText);

    const tilesRow = document.createElement("div");
    tilesRow.className = "memxray-tiles";
    function makeTile(label) {
        const tile = document.createElement("div");
        tile.className = "memxray-tile";
        const l = document.createElement("div");
        l.className = "memxray-tile-label";
        l.textContent = label;
        const v = document.createElement("div");
        v.className = "memxray-tile-value";
        v.textContent = "-";
        const s = document.createElement("div");
        s.className = "memxray-tile-sub";
        // extra dim line, hidden unless a tile needs a second note (e.g.
        // "mostly untouched commit" on the Paged out tile)
        const note = document.createElement("div");
        note.className = "memxray-tile-sub memxray-tile-note";
        note.style.display = "none";
        tile.appendChild(l);
        tile.appendChild(v);
        tile.appendChild(s);
        tile.appendChild(note);
        tilesRow.appendChild(tile);
        return { root: tile, value: v, sub: s, note };
    }
    const tiles = {
        inRam: makeTile("In RAM"),
        pagedOut: makeTile("Paged out"),
        evictedMax: makeTile("Evicted (max)"),
        mapped: makeTile("Mapped files"),
        committed: makeTile("Committed"),
        pagefile: makeTile("Pagefile"),
        pagefileTrend: makeTile("Pagefile trend"),
        hardReads: makeTile("Hard reads/s"),
    };

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

    root.appendChild(verdictRow);
    root.appendChild(tilesRow);
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
        verdictDot: dot, verdictText,
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
