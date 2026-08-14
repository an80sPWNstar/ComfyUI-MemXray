# Handoff

Append-only. Newest entry wins; earlier entries are history, not instructions.
Verify any claim here against the code before acting on it.

## 2026-08-13 22:01 -- initial build + three-view redesign

Built the pack from scratch and shipped it to
`github.com/an80sPWNstar/ComfyUI-MemXray` (private, MIT). Repo at `b4cfe43`,
working tree clean, in sync with origin.

Live install is a directory junction, not a copy:
`D:\ComfyUI_Installs\New Main\ComfyUI\custom_nodes\ComfyUI-MemXray` ->
`E:\vs_code_projects\ComfyUI-MemXray`. Edit the project dir; ComfyUI sees it
immediately. JS changes need only a browser refresh, Python changes need a
ComfyUI restart.

State: backend, all four HTTP endpoints, all four nodes, and the panel's four
views (Tiers / Models / Headroom / Details) are verified against the live
server on port 8188.

**Not yet verified, and the reason to be careful tomorrow:**
- The Models ledger has only ever been seen in its empty state. No models were
  loaded during the build.
- The pagefile alarm path (red fault bars, `verdict.level == "alarm"`) has
  never fired on real data. It is coded and unit-reasoned, not witnessed.
Both need a model load big enough to force eviction. Until then, treat the
red/alarm rendering as untested.

**Do not "fix" these, they are deliberate:**
- No per-model pagefile figure. Windows attributes eviction per process, not
  per allocation; the Models view reports the process-level ceiling once
  instead. Adding a per-checkpoint number would be fabrication.
- `evicted_max` is drawn as a dashed hairline over a faint tint, never a filled
  slab. It is an upper bound capped by the system-wide pagefile, most of which
  may belong to other processes.
- The chart palette was validated with the dataviz palette checker (lightness
  band, chroma floor, CVD separation, contrast). Do not nudge those hexes by
  eye - re-run the validator.
- `web/js/memxray.js` imports `../../../scripts/app.js` with THREE levels, not
  the usual two, because the file is served from `/extensions/<pack>/js/`.

Measured facts that are easy to re-derive wrongly, with the numbers:
- A committed-vs-resident gap is not pagefile use. Observed 16.40 GB committed
  / 8.69 GB resident (7.72 GB gap) while the whole system pagefile held
  1.75 GB. The gap was overwhelmingly untouched commit.
- `\Memory\Page Reads/sec` is not a pagefile counter. Importing torch and
  mmap'ing a checkpoint hit 1330 reads/s with zero pagefile growth. Only
  pagefile growth alongside faults escalates the verdict.
