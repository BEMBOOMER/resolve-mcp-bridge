"""Reusable base-layer builder: segments in -> vertical timeline + subs + burn render.

Workflow per video (Roelofs shortform-regels: max 60s, alle stiltes eruit):
  1. build(name, segments)      — nieuwe verticale timeline, fill-zoom per clip
  2. check_render + whisper     — ground-truth spraakkaart van de échte timeline
  3. captions uit die kaart     — per definitie gesynct; korte regels (<=18 tekens)
  4. final render met burn-in   — Deliver-instelling 'Burn into video' moet aan staan
"""

import json
import os
import subprocess
import time

from resolve_bridge import resolve_api as ra
from resolve_bridge import server as srv

MODEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "models", "ggml-large-v3-turbo-q5_0.bin")
SCRATCH = os.path.expanduser("~/.cache/resolve-mcp-bridge/work")


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
    resolve, project = ra.get_project()
    tl = ra.get_timeline(project, name)
    project.SetCurrentTimeline(tl)
    resolve.OpenPage("deliver")
    project.DeleteAllRenderJobs()
    project.SetCurrentRenderFormatAndCodec("mp4", "H264")
    project.SetRenderSettings({"TargetDir": target_dir, "CustomName": custom_name,
                               "SelectAllFrames": True, "ExportVideo": True, "ExportAudio": True})
    job = project.AddRenderJob()
    project.StartRendering(job)
    while project.IsRenderingInProgress():
        time.sleep(1)
    resolve.OpenPage("edit")
    status = project.GetRenderJobStatus(job).get("JobStatus")
    if status != "Complete":
        raise RuntimeError(f"render {custom_name}: {status}")
    return os.path.join(target_dir, custom_name + ".mp4")


def listen(mp4):
    """Whisper op een render: [(start, end, text)] — ground truth voor captions/QA."""
    wav = os.path.join(SCRATCH, "listen.wav")
    base = os.path.join(SCRATCH, "listen")
    subprocess.run(["ffmpeg", "-y", "-i", mp4, "-vn", "-ac", "1", "-ar", "16000", wav],
                   capture_output=True, check=True)
    subprocess.run(["/opt/homebrew/bin/whisper-cli", "-m", MODEL, "-f", wav,
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


def tighten(segments, silences, end=None, residual=0.07):
    """Knip render-tijd-stiltes terug het segmentenlijstje in (shortform: alles eruit).

    segments: [(path, in_sec, out_sec)] zoals gebouwd; silences in render-tijd van
    diezelfde volgorde. Geeft een nieuwe segmentenlijst terug.
    """
    # render-tijd -> (path, bron-tijd) stukken
    bounds = []
    t = 0.0
    for path, a, b in segments:
        bounds.append((t, t + (b - a), path, a))
        t += b - a
    total = end if end is not None else t

    spans, cur = [], 0.0
    for a, b in silences:
        ca, cb = a + residual, b - residual
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
            "Foutuil": "fauteuil", "Hoogbaaag": "Hoekbaaaank", "Scandy": "Scandi", "Nederland-Sweden": "Nederland-Zweden"}


def captions_from_listen(name, spans, width=18):
    """spans: [(start, end, text)] -> SRT op de timeline, korte regels, lange spans gesplitst."""
    entries = []
    for a, b, text in spans:
        for wrong, right in BRANDFIX.items():
            text = text.replace(wrong, right)
        lines = wrap(text, width)
        if len(lines) <= 2:
            entries.append((a, b, "\n".join(lines)))
        else:
            chunks = ["\n".join(lines[i:i + 2]) for i in range(0, len(lines), 2)]
            step = (b - a) / len(chunks)
            for i, ch in enumerate(chunks):
                entries.append((a + i * step, a + (i + 1) * step, ch))

    def ts(s):
        ms = int(round(s * 1000))
        return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"

    srt = "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(entries, 1))
    return srv.add_subtitles_srt(srt, timeline_name=name)
