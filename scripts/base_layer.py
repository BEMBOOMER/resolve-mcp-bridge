"""Reusable base-layer builder: segments in -> vertical timeline + subs + burn render.

Workflow per video (Roelofs shortform-regels: max 60s, alle stiltes eruit):
  1. build(name, segments)      — nieuwe verticale timeline, fill-zoom per clip
  2. check_render + whisper     — ground-truth spraakkaart van de échte timeline
  3. captions uit die kaart     — per definitie gesynct; korte regels (<=18 tekens)
  4. final render met burn-in   — Deliver-instelling 'Burn into video' moet aan staan
"""

import glob
import json
import os
import re
import shutil
import subprocess
import time

from resolve_bridge import resolve_api as ra
from resolve_bridge import server as srv

MODEL = os.environ.get(
    "RESOLVE_BRIDGE_WHISPER_MODEL",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "models", "ggml-large-v3-turbo-q5_0.bin"))
WHISPER = (os.environ.get("RESOLVE_BRIDGE_WHISPER_BIN")
           or shutil.which("whisper-cli")
           or "/opt/homebrew/bin/whisper-cli")
SCRATCH = os.path.expanduser("~/.cache/resolve-mcp-bridge/work")
os.makedirs(SCRATCH, exist_ok=True)


SKIP_BINS = ("Timelines", "FX", "SFX", "AI", "COMPOUND", "b-roll")


def discover():
    """Mappen met raw clips in het open project + of er al een timeline naar vernoemd is.

    Returns: [{"folder", "video" (mapnaam = videonaam), "clips": [{name, path, duration_s}],
               "timeline_exists": bool}]
    """
    _, project = ra.get_project()
    timelines = {project.GetTimelineByIndex(i).GetName()
                 for i in range(1, int(project.GetTimelineCount()) + 1)}
    byfolder = {}
    for folder, clip in ra.iter_media_pool_clips(project.GetMediaPool()):
        if any(s.lower() in folder.lower() for s in SKIP_BINS):
            continue
        path = clip.GetClipProperty("File Path") or ""
        tp = clip.GetClipProperty("Type") or ""
        if not path or "Video" not in tp or "Audio" not in tp:
            continue
        try:
            fps = float(clip.GetClipProperty("FPS"))
            h, m, s, f = (int(x) for x in (clip.GetClipProperty("Duration") or "0:0:0:0")
                          .replace(";", ":").split(":"))
            dur = h * 3600 + m * 60 + s + f / max(fps, 1)
        except (TypeError, ValueError):
            dur = None
        byfolder.setdefault(folder, []).append(
            {"name": clip.GetName(), "path": path, "duration_s": round(dur, 1) if dur else None})
    out = []
    for folder, clips in sorted(byfolder.items()):
        video = folder.rstrip("/").split("/")[-1]
        out.append({"folder": folder, "video": video, "clips": clips,
                    "timeline_exists": video in timelines})
    return out


def free_timeline_name(wanted):
    """Eerste vrije timeline-naam: 'X', anders 'X 2', 'X 3', ... Nooit iets overschrijven."""
    _, project = ra.get_project()
    existing = {project.GetTimelineByIndex(i).GetName()
                for i in range(1, int(project.GetTimelineCount()) + 1)}
    if wanted not in existing:
        return wanted
    n = 2
    while f"{wanted} {n}" in existing:
        n += 1
    return f"{wanted} {n}"


def fill_zoom(clip, tw=1080, th=1920):
    """Zoom needed so a clip fills a tw x th canvas (Resolve zoom is relative to fit)."""
    res = clip.GetClipProperty("Resolution") or ""
    try:
        w, h = (int(x) for x in res.split("x"))
    except ValueError:
        return 1.0
    fit = min(tw / w, th / h)
    fill = max(tw / w, th / h)
    return round(fill / fit + 0.005, 3)


def build(name, segments, fps_note=None):
    """segments: [(path, in_sec, out_sec), ...] in gewenste volgorde."""
    _, project = ra.get_project()
    existing = [project.GetTimelineByIndex(i).GetName()
                for i in range(1, int(project.GetTimelineCount()) + 1)]
    if name in existing:
        srv.delete_timeline(name)
    r = srv.build_edit(name, [{"clip": p, "in_sec": a, "out_sec": b} for p, a, b in segments])
    if r.get("status") != "success":
        raise RuntimeError(f"build_edit: {r}")
    resolve, project = ra.get_project()
    tl = ra.get_timeline(project, name)
    tl.SetSetting("useCustomSettings", "1")
    tl.SetSetting("timelineResolutionWidth", "1080")
    tl.SetSetting("timelineResolutionHeight", "1920")
    project.SetCurrentTimeline(tl)
    mp = project.GetMediaPool()
    items = tl.GetItemListInTrack("video", 1) or []
    transforms = []
    for i, item in enumerate(items, 1):
        mpi = item.GetMediaPoolItem()
        transforms.append({"index": i, "zoom": fill_zoom(mpi) if mpi else 1.0})
    srv.set_transforms(transforms, track=1, timeline_name=name)
    dur = (items[-1].GetEnd() - items[0].GetStart()) / ra.timeline_fps(tl) if items else 0
    return {"clips": len(items), "duration": round(dur, 2)}


def render(name, custom_name, target_dir):
    """Render een timeline naar target_dir. Retourneert het ECHTE bestandspad.

    Exporteren is in principe Roelofs domein — gebruik dit voor check-renders naar de
    scratchpad, of voor finals ALLEEN op zijn expliciete vraag (export_finals()).
    Burn-in subs: een API-render erft de actuele Deliver-instelling (Subtitle Settings >
    Burn into video); die dropdown zet Roelof eenmalig per project in de UI.
    Bugfix 2026-07-07: custom_name mag met of zonder .mp4; het teruggegeven pad wordt
    geverifieerd op schijf (voorheen kwam er 'naam.mp4.mp4' terug)."""
    base = custom_name[:-4] if custom_name.lower().endswith(".mp4") else custom_name
    resolve, project = ra.get_project()
    tl = ra.get_timeline(project, name)
    project.SetCurrentTimeline(tl)
    resolve.OpenPage("deliver")
    project.DeleteAllRenderJobs()
    project.SetCurrentRenderFormatAndCodec("mp4", "H264")
    project.SetRenderSettings({"TargetDir": target_dir, "CustomName": base,
                               "SelectAllFrames": True, "ExportVideo": True, "ExportAudio": True})
    job = project.AddRenderJob()
    project.StartRendering(job)
    while project.IsRenderingInProgress():
        time.sleep(1)
    resolve.OpenPage("edit")
    status = project.GetRenderJobStatus(job).get("JobStatus")
    if status != "Complete":
        raise RuntimeError(f"render {base}: {status}")
    expected = os.path.join(target_dir, base + ".mp4")
    if os.path.isfile(expected) and os.path.getsize(expected) > 0:
        return expected
    # Resolve kan de naam uniek maken ("naam 1.mp4") — pak de nieuwste match
    matches = sorted(glob.glob(os.path.join(target_dir, base + "*.mp4")),
                     key=os.path.getmtime, reverse=True)
    if matches and os.path.getsize(matches[0]) > 0:
        return matches[0]
    raise RuntimeError(f"render {base}: job Complete maar geen output in {target_dir}")


def listen(mp4):
    """Whisper op een render: [(start, end, text)] — ground truth voor captions/QA."""
    wav = os.path.join(SCRATCH, "listen.wav")
    base = os.path.join(SCRATCH, "listen")
    subprocess.run(["ffmpeg", "-y", "-i", mp4, "-vn", "-ac", "1", "-ar", "16000", wav],
                   capture_output=True, check=True)
    subprocess.run([WHISPER, "-m", MODEL, "-f", wav,
                    "-l", "nl", "-oj", "-of", base, "-np"], capture_output=True, check=True)
    with open(base + ".json") as f:
        raw = json.load(f)
    out = []
    for seg in raw.get("transcription", []):
        text = seg.get("text", "").strip()
        if text:
            out.append((seg["offsets"]["from"] / 1000.0, seg["offsets"]["to"] / 1000.0, text))
    return out


def silences_in(mp4, noise_db=-26.0, min_dur=0.30):
    """Pauzes in een (check)render: [(start, end)] in render-tijd."""
    proc = subprocess.run(["ffmpeg", "-i", mp4, "-af",
                           f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"],
                          capture_output=True, text=True)
    out, start = [], None
    for line in proc.stderr.splitlines():
        if "silence_start:" in line:
            start = float(line.split("silence_start:")[1].strip())
        elif "silence_end:" in line and start is not None:
            end = float(line.split("silence_end:")[1].split("|")[0].strip())
            out.append((round(start, 2), round(end, 2)))
            start = None
    return out


def tighten(segments, silences, end=None, residual=None, pre=0.06, post=0.18):
    """Knip render-tijd-stiltes terug het segmentenlijstje in (shortform: alles eruit).

    segments: [(path, in_sec, out_sec)] zoals gebouwd; silences in render-tijd van
    diezelfde volgorde. Geeft een nieuwe segmentenlijst terug.

    ASYMMETRISCHE residuals (Roelof-feedback 2026-07-07: "je cut vaak te snel, de zin
    is niet volledig gezegd"): `post` = wat er ná het einde van spraak blijft staan
    vóór de knip (staart/verval van het laatste woord — ruim nemen, whisper- en
    silencedetect-eindes zitten structureel te vroeg), `pre` = aanloop vóór het
    volgende woord. Oude symmetrische `residual`-kwarg blijft werken.
    """
    if residual is not None:
        pre = post = residual
    # render-tijd -> (path, bron-tijd) stukken
    bounds = []
    t = 0.0
    for path, a, b in segments:
        bounds.append((t, t + (b - a), path, a))
        t += b - a
    total = end if end is not None else t

    spans, cur = [], 0.0
    for a, b in silences:
        # stilte begint op a (= einde spraak): hou `post` staart; eindigt op b: hou `pre` aanloop
        ca, cb = a + post, b - pre
        if cb <= ca or ca >= total:
            continue
        if ca > cur:
            spans.append((cur, min(ca, total)))
        cur = cb
    if total > cur:
        spans.append((cur, total))

    def locate(rt, side):
        for lo, hi, path, src0 in bounds:
            if (side == "L" and lo <= rt < hi) or (side == "R" and lo < rt <= hi):
                return path, src0 + (rt - lo)
        lo, hi, path, src0 = bounds[-1]
        return path, src0 + (min(rt, hi) - lo)

    new_segments = []
    for a, b in spans:
        cuts = [a] + [hi for _, hi, _, _ in bounds[:-1] if a < hi < b] + [b]
        for i in range(len(cuts) - 1):
            pa, sa = locate(cuts[i], "L")
            pb, sb = locate(cuts[i + 1], "R")
            if pa == pb and sb - sa > 0.05:
                new_segments.append((pa, round(sa, 3), round(sb, 3)))
    return new_segments


def wrap(text, width=18):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


# whisper verhaspelt eigennamen; corrigeer vóór captioning
BRANDFIX = {"Prompto": "Pronto", "prompto": "Pronto", "Bonscoach": "bondscoach",
            "Wonenscoach": "bondscoach", "Tunesiën": "Tunesië", "Mascato": "Moscato",
            "Foutuil": "fauteuil", "Hoogbaaag": "Hoekbaaaank", "Scandy": "Scandi", "Nederland-Sweden": "Nederland-Zweden",
            "Pront te wonen": "Pronto Wonen", "Pronto wonen": "Pronto Wonen", "Promte Wonen": "Pronto Wonen",
            "Promte wonen": "Pronto Wonen", "Mijn bevriendinnen": "Meubelvriendinnen",
            "Mijn vriendinnen": "Meubelvriendinnen", "Meubelvriendinnen": "Meubelvriendinnen"}


def _oneline_chunks(text, max_chars):
    """Splits een zin in ZO WEINIG mogelijk ÉÉN-regel-brokjes van <= max_chars.
    Een tweeregelige zin wordt gebalanceerd gesplitst (geen los kort woord)."""
    text = text.strip()
    words = text.split()
    if not words:
        return [text]
    if len(text) <= max_chars:
        return [text]
    # past het in twee gebalanceerde regels? kies de splitsing met de kortste langste regel
    best = None
    for k in range(1, len(words)):
        l1, l2 = " ".join(words[:k]), " ".join(words[k:])
        if len(l1) <= max_chars and len(l2) <= max_chars:
            score = max(len(l1), len(l2))
            if best is None or score < best[0]:
                best = (score, [l1, l2])
    if best:
        return best[1]
    # meer dan twee regels nodig: greedy vullen
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def format_oneline(spans, lead=0.0, min_dur=0.0, max_hold=3.2, max_chars=18, gap=0.04, pause_gap=0.35):
    """Roelofs shortform-subregels: ALTIJD één regel (nooit 2 lagen), EXACT op het gesproken
    woord (lead=0.0 — Roelof-correctie 2026-07-07 na review v1-v3: subs moeten precies vallen
    wanneer het gezegd wordt, niet 0,3s eerder en zeker niet 2-3s off). Opeenvolgende captions
    in doorlopende spraak sluiten op elkaar aan (contiguous), MAAR bij een echte pauze
    (> pause_gap) eindigt de caption op de spraak en blijft er een gat — korte kreten ("Ha!",
    "Ja!") worden NIET opgerekt (geen min-duur-padding). Een zin die niet op één regel
    past wordt in ZO WEINIG mogelijk opeenvolgende één-regel-captions gesplitst.
    max_chars=18 zodat de regel bij Roelofs vaste stijl NIET off-screen loopt (2026-07-07:
    subs liepen uit beeld). Vaste subtitle-track-stijl (zet Roelof in de Inspector, kan niet
    via de API): font Manrope, Face ExtraBold, Size 43, Color wit, Stroke 0, Alignment center.
    spans: [(start, end, text)] -> [(a, b, oneline_text)].
    """
    raw = []
    for a, b, text in spans:
        for wrong, right in BRANDFIX.items():
            text = text.replace(wrong, right)
        text = text.replace(" ", " ").replace("\n", " ").strip()
        chunks = _oneline_chunks(text, max_chars)
        step = (b - a) / len(chunks) if chunks else 0
        for i, ch in enumerate(chunks):
            raw.append([a + i * step, a + (i + 1) * step, ch])
    prev_end = 0.0
    for c in raw:                       # EXACT op het woord (lead=0.0), nooit overlappend met vorige
        c[0] = max(c[0] - lead, prev_end, 0.0)
        if c[1] < c[0] + 0.2:
            c[1] = c[0] + 0.2            # minimale zichtbaarheid; korte kreten niet oprekken
        prev_end = c[1]
    for i, c in enumerate(raw):          # aansluiten binnen doorlopende spraak; gat laten bij echte pauze
        speech_end = c[1]
        if i + 1 < len(raw):
            next_start = raw[i + 1][0]
            if next_start - speech_end <= pause_gap:     # doorlopende spraak -> tot net vóór de volgende
                room = next_start - gap - c[0]
                c[1] = c[0] + min(max(room, 0.2), max_hold)
            else:                                        # echte pauze -> eindig op de spraak, gat blijft
                c[1] = max(speech_end, c[0] + 0.2)
        else:
            c[1] = max(speech_end, c[0] + 0.2)
    return [(round(a, 3), round(b, 3), t) for a, b, t in raw]


def _srt(entries):
    def ts(s):
        ms = int(round(s * 1000))
        return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"
    return "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(entries, 1))


def captions_from_listen(name, spans, lead=0.0, max_chars=18, **_legacy):
    """spans (uit listen() op de finale render) -> één-regel shortform-subs op de timeline.
    lead=0.0: subs vallen EXACT op het gesproken woord (Roelof-correctie 2026-07-07). Oude
    min_dur-kwarg wordt genegeerd (geen padding meer)."""
    entries = format_oneline(spans, lead=lead, max_chars=max_chars)
    return srv.add_subtitles_srt(_srt(entries), timeline_name=name)


# ---------------------------------------------------------------------------
# Audio op de timeline: muziekbed + SFX (2026-07-07)
# ---------------------------------------------------------------------------

def _get_timeline(name):
    _, project = ra.get_project()
    tl = ra.get_timeline(project, name)
    project.SetCurrentTimeline(tl)
    return project, tl


def _media_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(out.stdout.strip())


def timeline_duration(name):
    """Duur van video-track 1 in seconden (0.0 bij lege timeline)."""
    _, tl = _get_timeline(name)
    items = tl.GetItemListInTrack("video", 1) or []
    if not items:
        return 0.0
    fps = ra.timeline_fps(tl)
    return (items[-1].GetEnd() - items[0].GetStart()) / fps


def place_audio(name, path, at_sec=0.0, track_index=2):
    """Plaats een audiobestand op de timeline op `at_sec` (timeline-relatief).

    VALKUIL die dit oplost: AppendToTimeline's `recordFrame` is ABSOLUUT — een
    timeline start op 01:00:00:00 = frame 360000 bij 100fps, dus recordFrame 0
    belandt op -3600s (vóór de timeline, onzichtbaar). Hier: tl.GetStartFrame()
    + at_sec * fps. Verifieert de plaatsing en ruimt een misplaatst item op.
    """
    project, tl = _get_timeline(name)
    fps = ra.timeline_fps(tl)
    while int(tl.GetTrackCount("audio")) < track_index:
        if not tl.AddTrack("audio"):
            raise RuntimeError(f"place_audio: kon audiotrack {track_index} niet aanmaken")
    mp = project.GetMediaPool()
    imported = mp.ImportMedia([os.path.abspath(path)])
    if not imported:
        raise RuntimeError(f"place_audio: import van {path} faalde")
    item = imported[0]
    dur_f = int(round(_media_duration(path) * fps))
    rec = int(tl.GetStartFrame()) + int(round(at_sec * fps))
    res = mp.AppendToTimeline([{"mediaPoolItem": item, "startFrame": 0,
                                "endFrame": max(dur_f - 1, 1), "mediaType": 2,
                                "trackIndex": track_index, "recordFrame": rec}])
    if not (res and res[0]):
        raise RuntimeError(f"place_audio: AppendToTimeline faalde voor {path}")
    # verifieer: staat er nu een item op deze track dat op at_sec begint?
    t0 = int(tl.GetStartFrame())
    placed = [(it.GetStart() - t0) / fps for it in (tl.GetItemListInTrack("audio", track_index) or [])]
    if not any(abs(p - at_sec) < 0.05 for p in placed):
        raise RuntimeError(f"place_audio: item niet op {at_sec:.2f}s gevonden (wel: {placed})")
    return {"status": "success", "at_sec": round(at_sec, 3), "track": track_index,
            "duration_s": round(dur_f / fps, 2)}


def music_bed(name, track_path, level_db=-16.0, fade_in=0.2, fade_out=0.5,
              duration=None, track_index=2):
    """Muziekbed onder een timeline: track voorbewerken (niveau + fades, geknipt op
    timeline-duur) en op audiotrack `track_index` vanaf 0.0 plaatsen.

    level_db=-16: bed ~16dB onder dialoog (Roelof-niveau, geverifieerd op V6.4-sjabloon).
    Muziek-gedreven video's (catwalk/montage): hoger niveau bewust kiezen, bijv. -6.
    """
    dur = duration or timeline_duration(name)
    if dur <= 0:
        raise RuntimeError("music_bed: timeline heeft geen duur")
    bed = os.path.join(SCRATCH, f"bed_{re.sub(r'[^A-Za-z0-9]+', '_', name)}.wav")
    fo_start = max(dur - fade_out, 0)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", track_path, "-t", f"{dur:.3f}",
                    "-af", f"volume={level_db}dB,afade=t=in:st=0:d={fade_in},"
                           f"afade=t=out:st={fo_start:.3f}:d={fade_out}",
                    "-ar", "48000", bed], check=True)
    return place_audio(name, bed, at_sec=0.0, track_index=track_index)


def sfx_at(name, path, at_sec, gain_db=0.0, track_index=3):
    """SFX strak op een actie-beat plaatsen. Bepaal `at_sec` met een frame-extract
    van het actie-moment, niet op de gok. gain_db past het niveau aan (bijv. -6)."""
    src = path
    if abs(gain_db) > 0.01:
        src = os.path.join(SCRATCH, f"sfx_{abs(hash((path, gain_db, at_sec)))}.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path,
                        "-af", f"volume={gain_db}dB", "-ar", "48000", src], check=True)
    return place_audio(name, src, at_sec=at_sec, track_index=track_index)


# ---------------------------------------------------------------------------
# Beat-grid: cut op de beat (2026-07-07)
# ---------------------------------------------------------------------------

def beats_in(audio_path, min_bpm=70.0, max_bpm=180.0, sr=16000, hop=256):
    """Beat-grid van een muziektrack: {"bpm", "beats": [sec, ...]}.

    Pure-python (audioop): energie-envelope -> onset-flux -> tempo via
    autocorrelatie (parabolisch verfijnd) -> beat-fase die de flux maximaliseert.
    Werkt prima op beat-heldere shortform-tracks; voor ambient/rubato niet bruikbaar
    (dan geeft lage confidence). Gebruik met quantize_cuts() om cutpunten te snappen.
    """
    import audioop
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", audio_path, "-vn",
                           "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                          capture_output=True, check=True)
    raw = proc.stdout
    n = len(raw) // (2 * hop)
    if n < int(5 * sr / hop):
        return {"bpm": None, "beats": [], "confidence": 0.0, "note": "track te kort"}
    env = [audioop.rms(raw[i * 2 * hop:(i + 1) * 2 * hop], 2) for i in range(n)]
    flux = [max(0.0, env[i] - env[i - 1]) for i in range(1, n)]
    m = max(flux) or 1.0
    flux = [f / m for f in flux]
    fr = sr / hop                                   # envelope-framerate
    lo, hi = int(fr * 60 / max_bpm), int(fr * 60 / min_bpm)
    mean = sum(flux) / len(flux)
    dev = [f - mean for f in flux]
    def ac(lag):
        s = sum(dev[i] * dev[i - lag] for i in range(lag, len(dev)))
        return s / (len(dev) - lag)
    scores = {lag: ac(lag) for lag in range(lo, min(hi + 1, len(dev) // 2))}
    if not scores:
        return {"bpm": None, "beats": [], "confidence": 0.0, "note": "bereik te klein"}
    best = max(scores, key=scores.get)
    # parabolische verfijning rond de beste lag
    y0, y1, y2 = (scores.get(best - 1, scores[best]), scores[best],
                  scores.get(best + 1, scores[best]))
    denom = (y0 - 2 * y1 + y2) or 1e-9
    lag = best + 0.5 * (y0 - y2) / denom
    # beat-fase: offset die de som van flux op gridpunten maximaliseert
    best_off, best_score = 0.0, -1.0
    off = 0.0
    while off < lag:
        s, k = 0.0, 0
        while True:
            idx = int(round(off + k * lag))
            if idx >= len(flux):
                break
            s += flux[idx]
            k += 1
        if s > best_score:
            best_score, best_off = s, off
        off += 0.25
    beats = []
    t = best_off
    while t < len(flux):
        beats.append(round((t + 1) * hop / sr, 3))   # +1: flux[i] hoort bij env[i+1]
        t += lag
    on_grid = best_score / max(len(beats), 1)
    return {"bpm": round(60.0 * fr / lag, 1), "beats": beats,
            "confidence": round(min(on_grid / (mean + 1e-9) / 3.0, 1.0), 2)}


def quantize_cuts(cut_times, beats, tol=0.25):
    """Snap cutpunten (render-tijd, seconden) op het dichtstbijzijnde beat-moment.

    Alleen verschoven als het verschil <= tol; anders blijft de cut staan (spraak
    wint van muziek). Returns: [(oud, nieuw, versprongen_bool)].
    """
    out = []
    for t in cut_times:
        nearest = min(beats, key=lambda b: abs(b - t)) if beats else t
        if abs(nearest - t) <= tol:
            out.append((round(t, 3), round(nearest, 3), True))
        else:
            out.append((round(t, 3), round(t, 3), False))
    return out


# ---------------------------------------------------------------------------
# QA-rapport: valideer als kijker, niet als programmeur (2026-07-07)
# ---------------------------------------------------------------------------

def qa_report(name, mp4=None, out_dir=None):
    """Automatische kwaliteitscontrole van een timeline. Levert report.md + report.json
    + contactsheet + eerste/laatste frame in out_dir (default: SCRATCH/qa/<naam>).

    Checks: duur, gaten op video-track 1, subs aanwezig + SYNC-verificATIE (sub-start
    vs whisper-spraak-onset op de render — de fout die Roelof eerder handmatig moest
    herstellen), afgekapte laatste woorden (spraak die doorloopt tot vlak voor een cut),
    dode lucht, loudness (dialoog vs geheel). Geen render meegegeven -> maakt zelf een
    check-render. LEEST alleen; verandert niets aan de timeline.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    out_dir = out_dir or os.path.join(SCRATCH, "qa", safe)
    os.makedirs(out_dir, exist_ok=True)
    if mp4 is None:
        mp4 = render(name, f"qa_{safe}", out_dir)

    rep = {"timeline": name, "render": mp4}
    dur = _media_duration(mp4)
    rep["duration_s"] = round(dur, 2)
    rep["within_60s"] = dur <= 60.5

    info = srv.get_timeline_items(name)
    fps = info.get("fps", 100.0)
    vids = next((t["items"] for t in info["tracks"] if t["type"] == "video" and t["track"] == 1), [])
    t0 = vids[0]["start_sec"] if vids else 0.0
    gaps = []
    for a, b in zip(vids, vids[1:]):
        if b["start_sec"] - a["end_sec"] > 1.0 / fps:
            gaps.append((round(a["end_sec"] - t0, 2), round(b["start_sec"] - t0, 2)))
    rep["video_gaps"] = gaps
    subs = next((t["items"] for t in info["tracks"] if t["type"] == "subtitle"), [])
    rep["sub_count"] = len(subs)
    audio_tracks = [t["track"] for t in info["tracks"] if t["type"] == "audio" and t["items"]]
    rep["audio_tracks"] = audio_tracks
    rep["has_music_bed"] = any(t >= 2 for t in audio_tracks)

    spans = listen(mp4)
    rep["transcript"] = [t for _, _, t in spans]
    # sub-sync: elke spraak-onset moet een sub-start binnen 0.25s hebben
    sync = []
    if subs:
        sub_starts = [s["start_sec"] - t0 for s in subs]
        for a, _, txt in spans:
            nearest = min(sub_starts, key=lambda s: abs(s - a))
            sync.append({"speech_at": round(a, 2), "sub_at": round(nearest, 2),
                         "offset": round(nearest - a, 2), "text": txt[:40]})
        worst = max((abs(s["offset"]) for s in sync), default=0.0)
        rep["sub_sync"] = {"checks": sync, "worst_offset_s": worst, "ok": worst <= 0.25}
    else:
        rep["sub_sync"] = {"checks": [], "worst_offset_s": None,
                           "ok": None, "note": "geen subs op het spoor"}
    # afgekapte zinnen: spraak die doorloopt tot < post-residual vóór een cut of het einde
    cut_points = [b["start_sec"] - t0 for b in vids[1:]] + [dur]
    clipped = []
    for _, b, txt in spans:
        for c in cut_points:
            if -0.02 < c - b < 0.12:
                clipped.append({"speech_end": round(b, 2), "cut_at": round(c, 2),
                                "text": txt[-45:]})
    rep["possibly_clipped"] = clipped
    rep["dead_air"] = [(a, b) for a, b in silences_in(mp4, noise_db=-32.0, min_dur=0.8)]

    loud = subprocess.run(["ffmpeg", "-i", mp4, "-af", "ebur128", "-f", "null", "-"],
                          capture_output=True, text=True).stderr
    m = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", loud)
    rep["integrated_lufs"] = float(m[-1]) if m else None

    # visueel: contactsheet + eerste/laatste frame
    n_tiles = min(max(int(dur), 4), 30)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp4,
                    "-vf", f"fps={n_tiles}/{dur:.2f},scale=270:-1,tile=6x{-(-n_tiles // 6)}",
                    "-frames:v", "1", os.path.join(out_dir, "contactsheet.jpg")], check=False)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.1", "-i", mp4, "-frames:v", "1",
                    os.path.join(out_dir, "first.png")], check=False)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(dur - 0.2, 0):.2f}", "-i", mp4,
                    "-frames:v", "1", os.path.join(out_dir, "last.png")], check=False)

    flags = []
    if not rep["within_60s"]:
        flags.append(f"duur {dur:.1f}s > 60s")
    if gaps:
        flags.append(f"{len(gaps)} gat(en) op video-track 1: {gaps}")
    if rep["sub_sync"]["ok"] is False:
        flags.append(f"sub-sync tot {rep['sub_sync']['worst_offset_s']}s ernaast")
    if clipped:
        flags.append(f"{len(clipped)} mogelijk afgekapte zin(nen): "
                     + "; ".join(c["text"] for c in clipped))
    rep["flags"] = flags
    rep["ok"] = not flags

    lines = [f"# QA — {name}", "",
             f"- Duur: {rep['duration_s']}s ({'OK' if rep['within_60s'] else 'TE LANG'})",
             f"- Gaten video-track 1: {gaps or 'geen'}",
             f"- Subs: {rep['sub_count']}  |  sync: {rep['sub_sync']['worst_offset_s']}s worst"
             f" ({rep['sub_sync']['ok']})",
             f"- Audio-tracks met inhoud: {audio_tracks}  |  muziekbed: {rep['has_music_bed']}",
             f"- Loudness: {rep['integrated_lufs']} LUFS",
             f"- Dode lucht (>0.8s): {rep['dead_air'] or 'geen'}",
             f"- Mogelijk afgekapt: {clipped or 'niets'}", "",
             "## Transcript", *[f"- {t}" for t in rep["transcript"]], "",
             f"**Oordeel: {'SCHOON' if rep['ok'] else 'FLAGS: ' + ' | '.join(flags)}**"]
    with open(os.path.join(out_dir, "report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(out_dir, "report.json"), "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    return rep


# ---------------------------------------------------------------------------
# FX-bibliotheek, SRT naar schijf, edit-log (2026-07-08)
# ---------------------------------------------------------------------------

AUDIO_EXT = (".wav", ".mp3", ".m4a", ".aif", ".aiff", ".ogg")


def fx_index(fx_dir, refresh=False):
    """Indexeer de muziek/SFX-bibliotheek van een project (<project>/fx): per bestand
    duur, categorie (music/sfx uit het pad) en bpm (alleen voor tracks > 8s).
    Cache in <fx_dir>/.fx_index.json (mtime-based); refresh=True dwingt herindexering.
    Hiermee is 'zoek een record-scratch' of 'welke track is ~120bpm' één lookup
    i.p.v. blind find-en door Envato-mappen."""
    fx_dir = os.path.abspath(fx_dir)
    cache_path = os.path.join(fx_dir, ".fx_index.json")
    cache = {}
    if os.path.isfile(cache_path) and not refresh:
        with open(cache_path) as f:
            cache = {e["path"]: e for e in json.load(f)}
    out = []
    for root, _dirs, files in os.walk(fx_dir):
        for fn in files:
            if not fn.lower().endswith(AUDIO_EXT) or fn.startswith("._"):
                continue
            p = os.path.join(root, fn)
            mtime = os.stat(p).st_mtime_ns
            hit = cache.get(p)
            if hit and hit.get("mtime") == mtime:
                out.append(hit)
                continue
            try:
                dur = _media_duration(p)
            except Exception:  # noqa: BLE001 — kapot bestand: overslaan, niet crashen
                continue
            rel = os.path.relpath(p, fx_dir)
            entry = {"path": p, "rel": rel, "name": fn, "mtime": mtime,
                     "category": rel.split(os.sep)[0].lower(),
                     "duration_s": round(dur, 2), "bpm": None}
            if dur > 8.0:
                try:
                    entry["bpm"] = beats_in(p)["bpm"]
                except Exception:  # noqa: BLE001
                    pass
            out.append(entry)
    with open(cache_path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def save_srt(name, out_dir):
    """Schrijf de subs van het subtitle-spoor van een timeline als nette SRT naar
    out_dir/<naam>.srt (Roelofs vaste plek: <video-map>/subtitles/). Timeline-start-
    offset (01:00:00:00) wordt naar 0 teruggerekend."""
    info = srv.get_timeline_items(name)
    subs = next((t["items"] for t in info["tracks"] if t["type"] == "subtitle"), [])
    if not subs:
        raise RuntimeError(f"save_srt: geen subtitle-spoor op '{name}'")
    vids = next((t["items"] for t in info["tracks"] if t["type"] == "video"), [])
    t0 = vids[0]["start_sec"] if vids else subs[0]["start_sec"]
    entries = [(s["start_sec"] - t0, s["end_sec"] - t0, s["name"]) for s in subs]
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.srt")
    with open(path, "w") as f:
        f.write(_srt(entries))
    return {"status": "success", "path": path, "subs": len(entries)}


def edit_log(project_dir, video, text):
    """Sessie-geheugen per project: append een blok aan <project>/script/edit-log.md.
    Volgende sessies lezen dit eerst (strategie, take-keuzes, muziek/SFX, open punten)."""
    d = os.path.join(project_dir, "script")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "edit-log.md")
    stamp = time.strftime("%Y-%m-%d %H:%M")
    block = f"\n## {video} — {stamp}\n\n{text.strip()}\n"
    with open(path, "a") as f:
        f.write(block)
    return {"status": "success", "path": path}


# ---------------------------------------------------------------------------
# Batch-export van finals (alleen op Roelofs expliciete vraag)
# ---------------------------------------------------------------------------

EXPORT_NAME_FIX = {"V1": "V1.1", "V 1 Extra": "V1.EX"}


def plan_export(final_dir, only=None):
    """Exportplan: [(timeline, submap, bestandsnaam)] — eerst laten zien, dan draaien."""
    _, project = ra.get_project()
    names = [project.GetTimelineByIndex(i).GetName()
             for i in range(1, int(project.GetTimelineCount()) + 1)]
    if only:
        names = [n for n in names if n in only]
    plan = []
    for n in names:
        m = re.search(r"\d", n)
        block = f"V{m.group(0)}" if m else "overig"
        fname = EXPORT_NAME_FIX.get(n, n)
        plan.append((n, block, f"{fname}.mp4"))
    return plan


def export_finals(final_dir, only=None, plan=None):
    """Alle (of `only`) timelines renderen naar final_dir/<blok>/<naam>.mp4 met
    post-checks. VOORWAARDE: Roelof heeft er expliciet om gevraagd, en de Deliver-
    dropdown 'Burn into video' staat aan als de subs ingebrand moeten (API erft dit).
    Bewezen run 2026-07-07: 29/29 geslaagd."""
    plan = plan or plan_export(final_dir, only=only)
    results = {"ok": [], "failed": []}
    for tl_name, block, fname in plan:
        outdir = os.path.join(final_dir, block)
        os.makedirs(outdir, exist_ok=True)
        try:
            path = render(tl_name, fname, outdir)
            good = os.path.getsize(path) > 0 and _media_duration(path) > 0.5
            (results["ok"] if good else results["failed"]).append((tl_name, path))
        except Exception as e:  # noqa: BLE001 — batch mag niet stoppen op één video
            results["failed"].append((tl_name, str(e)))
    return results
