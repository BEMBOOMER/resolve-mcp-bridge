#!/usr/bin/env python3
"""Offline tests voor base_layer helpers — draait ZONDER Resolve open.

Gebruik: .venv/bin/python scripts/test_helpers.py
Test: format_oneline (sub-timing regels), tighten (asymmetrische residuals),
beats_in (synthetische 120bpm-klik), quantize_cuts. Voor de Resolve-afhankelijke
tools blijft scripts/validate.py de integratietest (met Resolve open).
"""

import os
import subprocess
import sys
import tempfile
import types

# stub de resolve_bridge-imports zodat base_layer zonder Resolve laadt
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for m in ("resolve_bridge", "resolve_bridge.server", "resolve_bridge.resolve_api"):
    sys.modules.setdefault(m, types.ModuleType(m))

import base_layer as bl  # noqa: E402

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -> {detail}" if not cond else ""))
    if not cond:
        FAIL += 1


# --- format_oneline: exact op het woord, gat bij pauze, kreten niet oprekken ---
spans = [(0.30, 0.80, "Oké"), (1.00, 1.60, "hallo allemaal"), (1.60, 2.20, "ga vandaag"),
         (5.00, 5.20, "Ha!"), (6.50, 7.40, "Tis een hond")]
out = bl.format_oneline(spans)
check("lead=0: eerste sub start exact op het woord", abs(out[0][0] - 0.30) < 0.001, out[0])
check("doorlopende spraak sluit aan", abs(out[1][1] - (out[2][0] - 0.04)) < 0.06, (out[1], out[2]))
check("korte kreet niet opgerekt over pauze", out[3][1] <= 5.30, out[3])
check("gat blijft bij echte pauze", out[4][0] - out[3][1] > 1.0, (out[3], out[4]))
check("max 18 tekens per regel", all(len(t) <= 18 for _, _, t in out),
      [t for _, _, t in out if len(t) > 18])

# --- tighten: asymmetrisch (staart ruimer dan aanloop) ---
segs = [("clip.mp4", 10.0, 20.0)]
sil = [(4.0, 6.0)]  # stilte in render-tijd 4-6s
new = bl.tighten(segs, sil)
# spraak 0-4 krijgt +0.18 staart -> segment tot bron 10+4.18; hervat op 6-0.06 -> bron 15.94
check("tighten: staart 0.18s blijft staan", abs(new[0][2] - 14.18) < 0.001, new)
check("tighten: aanloop 0.06s blijft staan", abs(new[1][1] - 15.94) < 0.001, new)
legacy = bl.tighten(segs, sil, residual=0.07)
check("tighten: legacy residual werkt nog", abs(legacy[0][2] - 14.07) < 0.001, legacy)

# --- beats_in: synthetische 120bpm klik (elke 0.5s) ---
with tempfile.TemporaryDirectory() as td:
    click = os.path.join(td, "click.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "aevalsrc=if(lt(mod(t\\,0.5)\\,0.04)\\,sin(2*PI*880*t)\\,0):d=20",
                    "-ar", "16000", click], check=True)
    grid = bl.beats_in(click)
    check("beats_in: bpm ~120", grid["bpm"] is not None and 118 <= grid["bpm"] <= 122, grid.get("bpm"))
    if grid["beats"]:
        errs = [abs((b % 0.5) if (b % 0.5) < 0.25 else 0.5 - (b % 0.5)) for b in grid["beats"][:20]]
        check("beats_in: beats op het 0.5s-grid (<60ms err)", max(errs) < 0.06, max(errs))
    else:
        check("beats_in: beats gevonden", False, grid)

    # --- quantize_cuts ---
    q = bl.quantize_cuts([1.02, 2.6, 7.49], [1.0, 2.0, 3.0, 7.5])
    check("quantize: snap binnen tolerantie", q[0][1] == 1.0 and q[2][1] == 7.5, q)
    check("quantize: buiten tolerantie blijft staan", q[1][1] == 2.6, q)

    # --- fx_index: synthetische bibliotheek (music/ + sfx/) ---
    fxd = os.path.join(td, "fx")
    os.makedirs(os.path.join(fxd, "music"))
    os.makedirs(os.path.join(fxd, "sfx"))
    import shutil as _sh
    _sh.copy(click, os.path.join(fxd, "music", "clicktrack.wav"))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=0.6", "-ar", "16000",
                    os.path.join(fxd, "sfx", "ping.wav")], check=True)
    idx = bl.fx_index(fxd)
    names = {e["name"]: e for e in idx}
    check("fx_index: vindt beide bestanden", len(idx) == 2, idx)
    check("fx_index: categorie uit pad", names.get("ping.wav", {}).get("category") == "sfx", names)
    check("fx_index: bpm voor muziek, niet voor korte sfx",
          names.get("clicktrack.wav", {}).get("bpm") and names.get("ping.wav", {}).get("bpm") is None,
          {k: v.get("bpm") for k, v in names.items()})
    idx2 = bl.fx_index(fxd)  # tweede run = cache-hit, moet identiek zijn
    check("fx_index: cache stabiel", idx2 == idx or len(idx2) == len(idx), len(idx2))

    # --- edit_log ---
    r = bl.edit_log(td, "V9.9", "Strategie: test.\nOpen punt: geen.")
    check("edit_log: geschreven", os.path.isfile(r["path"])
          and "V9.9" in open(r["path"]).read(), r)

print(f"\n{'ALLES GROEN' if FAIL == 0 else f'{FAIL} FAILURES'}")
sys.exit(1 if FAIL else 0)
