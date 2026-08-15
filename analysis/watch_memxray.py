"""Emit one line per MemXray state change worth acting on. Silence = nothing changed.

Damped on purpose: the raw verdict flaps mapped<->warn every time the fault
rate brushes its threshold, which is noise. A level must hold for CONFIRM
consecutive samples before it is reported. alarm reports immediately.
"""
import json
import time
import urllib.request

URL = "http://127.0.0.1:8188/memxray/stats"
G = 1024 ** 3
# MultiGPU trips at 85% used. Two thresholds, not one: a single line at 6 GB
# flaps every time a load breathes across it.
LOW_RAM_ENTER = 5.0       # say "tight" only below this
LOW_RAM_EXIT = 9.0        # say "recovered" only once clearly back above it
CONFIRM = 4               # ~8s at a 2s poll before a level change is believed
ALARM_COOLDOWN = 300      # seconds of quiet after an alarm before saying it again
VERDICT_MIN_GAP = 240     # floor between verdict messages - the level flaps for
                          # minutes at a time and every flip is not news
last_alarm_at = -1e9
last_verdict_at = -1e9
first_sample = True

reported_level = None     # last level actually announced
cand_level = None         # level currently accumulating confirmations
cand_n = 0
last_lowram = None
last_models = None
peak_reads = 0.0
fails = 0


def line(msg):
    print(msg, flush=True)


while True:
    try:
        with urllib.request.urlopen(URL, timeout=5) as r:
            d = json.loads(r.read().decode())
        fails = 0
    except Exception as e:
        fails += 1
        if fails == 3:
            line("MEMXRAY UNREACHABLE: %s" % e)
        time.sleep(3)
        continue

    n = d["now"]
    s, p, f, m = n["sys"], n["proc"], n["faults"], n["models"]

    # First sample after a (re)start establishes the baseline silently -
    # announcing "None -> warn" on every restart is noise, not an event.
    if first_sample:
        first_sample = False
        reported_level = n["verdict"]["level"]
        cand_level, cand_n = reported_level, CONFIRM
        last_lowram = (s["phys_avail"] / G) < LOW_RAM_ENTER
        last_models = tuple(sorted(x["name"] for x in m["models"]))
        time.sleep(2)
        continue
    lvl = n["verdict"]["level"]
    av = s["phys_avail"] / G
    pct = 100.0 * (1 - s["phys_avail"] / s["phys_total"])
    reads = f["page_reads_sec"]
    names = tuple(sorted(x["name"] for x in m["models"]))
    peak_reads = max(peak_reads, reads)

    # Known bug in sampler.py: once evicted_max parks above the alarm floor it
    # never decays, so ANY read spike reports "pagefile in play" even with the
    # pagefile static and RAM two-thirds free. Only relay an alarm that has a
    # second, independent reason to be believed - the pagefile actually growing,
    # or RAM genuinely tight. Otherwise treat it as the warn it really is.
    vtext = n["verdict"]["text"]
    if lvl == "alarm" and s["pagefile_delta"] <= 1024 * 1024 and av > LOW_RAM_ENTER:
        lvl = "warn"
        # say so, or the level and the text disagree and the line reads as a bug
        vtext = "[demoted from alarm - pagefile static, RAM healthy] " + vtext

    # alarm is not confirm-damped, but a flapping alarm must not re-announce
    # every few seconds - once said, stay quiet about it for ALARM_COOLDOWN.
    if lvl == "alarm" and time.time() - last_alarm_at < ALARM_COOLDOWN:
        reported_level = lvl
        cand_level, cand_n = lvl, CONFIRM
    elif lvl == "alarm" and reported_level != "alarm":
        last_alarm_at = time.time()
        line("ALARM | %s | availRAM %.2f GB (%.1f%% used) pagefile %.2f GB "
             "(delta %.0f B/s) reads/s %.0f ws %.2f GB pagedout %.2f GB"
             % (vtext, av, pct, s["pagefile_used"] / G,
                s["pagefile_delta"], reads, p["working_set"] / G, p["paged_out"] / G))
        reported_level = lvl
        cand_level, cand_n = lvl, CONFIRM
    elif lvl != reported_level:
        if lvl == cand_level:
            cand_n += 1
        else:
            cand_level, cand_n = lvl, 1
        if cand_n >= CONFIRM and time.time() - last_verdict_at >= VERDICT_MIN_GAP:
            last_verdict_at = time.time()
            line("VERDICT %s -> %s (held %ds) | %s | availRAM %.2f GB (%.1f%% used) "
                 "pagefile %.2f GB reads/s %.0f (peak %.0f)"
                 % (reported_level, lvl, CONFIRM * 2, vtext, av, pct,
                    s["pagefile_used"] / G, reads, peak_reads))
            reported_level = lvl
            peak_reads = 0.0
    else:
        cand_level, cand_n = lvl, 0

    if last_lowram is None:
        low = av < LOW_RAM_ENTER
    elif last_lowram:
        low = av < LOW_RAM_EXIT      # stay "tight" until clearly recovered
    else:
        low = av < LOW_RAM_ENTER     # stay "fine" until clearly tight
    if low != last_lowram:
        line("RAM %s: %.2f GB free (%.1f%% used) | ws %.2f GB | models %d tot %.2f GB off %.2f GB"
             % ("TIGHT" if low else "recovered", av, pct, p["working_set"] / G,
                m["count"], m["total_bytes"] / G, m["offloaded_bytes"] / G))
        last_lowram = low

    if names != last_models:
        line("MODELS %d -> %d | %s | tot %.2f GB off %.2f GB gpu %.2f GB availRAM %.2f GB"
             % (0 if last_models is None else len(last_models), len(names),
                ", ".join(names) or "(empty)", m["total_bytes"] / G,
                m["offloaded_bytes"] / G,
                m["gpus"][0]["used"] / G if m["gpus"] else 0.0, av))
        last_models = names

    time.sleep(2)
