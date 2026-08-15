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

## 2026-08-14 19:35 -- MiniMax-H3 session: all four verdict levels witnessed

Eleven H3 generations run against the live panel. Repo at `b8d4757`.
All session data is committed under `analysis/` - 8,645 samples in
`analysis/memxray_runs.db`, raw JSONL and all three ComfyUI log rotations in
`analysis/raw/`. Rebuild with `python analysis/ingest.py`. Start at
`analysis/README.md`.

**Retire these caveats from the 2026-08-13 entry - both are now verified:**
- The Models ledger has been exercised at 39.55 GB across four models
  (MiniMaxH3, MiniMaxH3TEModel_, MiniMaxH3VideoVAE, MiniMaxH3AudioVAE),
  with populate, reorder, partial-release and full-empty transitions all seen.
- `alarm` has fired on real data, including one correctly-attributed pagefile
  thrash: paged_out 42.21 GB, untouched 22.72 GB, leaving 19.49 GB genuinely
  evicted and exactly matching the system pagefile. `ok`, `mapped`, `warn`
  and `alarm` have all now been observed live.

**The thing that matters most, and it is not in the panel at all:**
Commit is the binding constraint on this box, not physical RAM. During run 5
(16:37-16:44, standard resolution, reported as unremarkable at the time)
Windows grew the pagefile from 32 GB to 67 GB and `commit_total` reached
100.0% of `commit_limit` in 8 samples inside a 50 s window. Each time Windows
extended the pagefile, which raised the limit and averted an allocation
failure - a fixed-size pagefile fails there. Peak seen anywhere: 129.9 GB
against a 130.9 GB limit. Watching `phys_avail` and the verdict missed this
entirely; both looked survivable at 91-95%.

**Three open bugs in `memxray/sampler.py`, all with evidence in the findings table:**
- `alarm-latch` (sampler.py:305) - the alarm accepts `evicted_max >=
  PAGED_OUT_ALARM` as an alternative to `pagefile_growing`. `evicted_max` is
  `min(paged_out, pagefile_used)` and the pagefile does not shrink promptly, so
  after any heavy run every later read spike reports "with the pagefile in
  play" while the pagefile is static. 470 of 883 alarms were false by this
  route, including 10 during a completely idle hour with 39 GB free. It does
  self-clear once Windows reclaims pagefile space below the floor.
- `verdict-strobe` - the level is computed per-sample with no hysteresis, so it
  flips alarm/warn/mapped every few seconds under load and the panel strobes.
- `alarm-text-mismatch` - when the alarm is reached via the `evicted_max` floor
  the message still claims pagefile involvement, so text and trigger disagree.

**Environment change made this session, keep it:** `--disable-dynamic-vram` was
removed from the ComfyUI launch flags. With it set, loading was estimate-based
(`loaded partially; 8484 MB loaded, 8832 MB offloaded`), the GPU capped at
9.84 GB, and RAM stayed pinned for minutes after a job - which let
`comfyui-multigpu`'s idle-only 85% check fire and kill the `prompt_worker`
thread via a `None` `model_finalizer` (`comfy/model_management.py:812`,
crash captured at 13:43:07 in `analysis/raw/comfyui_8188.prev.log`). Without
the flag, dynamic VRAM uses the full 15.92 GB card and releases memory *before*
the job ends; eight subsequent runs produced zero trips and zero exceptions.
The MultiGPU crash path is unfixed and still armed - it just no longer gets
the conditions it needs.

Do not conclude a run was healthy from wall-clock alone: the 800 s high-res run
and a 786 s 30-step run are near-identical in duration and opposite in cause
(19.50 GB evicted at 98.9% commit vs 3.36 GB at 90.0%).

`web/js/memxray.js` gained a "What's been happening" recap under the Tiers
lanes (`d4b51c0`). Written and syntax-checked, but never rendered in a browser -
ComfyUI was closed before a refresh. Verify it draws before trusting it.
