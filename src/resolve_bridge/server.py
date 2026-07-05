"""resolve-mcp-bridge — MCP server that lets Claude edit in DaVinci Resolve.

Read the timeline and media pool, transcribe source audio locally (whisper.cpp),
then batch-build edits: cuts (new timeline from segments), punch-in zooms,
and subtitles (native SRT track and/or Text+). Works on Free and Studio.
"""

import functools
import glob
import json
import os
import time

from mcp.server.fastmcp import FastMCP

from resolve_bridge import resolve_api as ra
from resolve_bridge import transcribe as tr

mcp = FastMCP("resolve-bridge")

WORK_DIR = os.path.expanduser("~/.cache/resolve-mcp-bridge/work")


def _tool(fn):
    """Run a tool body, converting exceptions into readable error payloads.

    functools.wraps is essential: FastMCP derives the tool's parameter schema
    from the function signature, and wraps lets inspect.signature() see through
    the wrapper (a bare *args/**kwargs wrapper breaks every schema).
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ra.ResolveError, tr.TranscribeError) as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}
    return wrapper


def _clip_path(clip):
    return clip.GetClipProperty("File Path") or ""


def _resolve_media_path(name_or_path):
    """Accept a filesystem path or a media pool clip name; return the file path."""
    if os.path.exists(os.path.expanduser(name_or_path)):
        return os.path.abspath(os.path.expanduser(name_or_path))
    _, project = ra.get_project()
    clip = ra.find_clip(project.GetMediaPool(), name_or_path)
    path = _clip_path(clip)
    if not path or not os.path.exists(path):
        raise ra.ResolveError(f"Clip '{name_or_path}' heeft geen bereikbaar bronbestand ({path!r}).")
    return path


@mcp.tool()
@_tool
def get_project_info() -> dict:
    """Current Resolve project: name, fps, timelines, current timeline, Studio or Free."""
    resolve, project = ra.get_project()
    current = project.GetCurrentTimeline()
    return {
        "status": "success",
        "product": resolve.GetProductName() if hasattr(resolve, "GetProductName") else "unknown",
        "is_studio": ra.is_studio(resolve),
        "version": resolve.GetVersionString(),
        "project": project.GetName(),
        "fps": project.GetSetting("timelineFrameRate"),
        "resolution": f'{project.GetSetting("timelineResolutionWidth")}x{project.GetSetting("timelineResolutionHeight")}',
        "timeline_count": project.GetTimelineCount(),
        "current_timeline": current.GetName() if current else None,
    }


@mcp.tool()
@_tool
def list_media() -> dict:
    """All clips in the media pool with duration, fps, resolution and source path."""
    _, project = ra.get_project()
    clips = []
    for folder, clip in ra.iter_media_pool_clips(project.GetMediaPool()):
        props = {k: clip.GetClipProperty(k) for k in
                 ("Duration", "FPS", "Resolution", "File Path", "Type", "Audio Ch")}
        clips.append({"folder": folder, "name": clip.GetName(), **{k.lower().replace(" ", "_"): v for k, v in props.items()}})
    return {"status": "success", "count": len(clips), "clips": clips}


@mcp.tool()
@_tool
def get_timeline_items(timeline_name: str = "") -> dict:
    """All items on a timeline (current one by default): per track, with start/end/source
    timings in both frames and seconds, plus current zoom/pan values."""
    _, project = ra.get_project()
    tl = ra.get_timeline(project, timeline_name or None)
    fps = ra.timeline_fps(tl)
    tracks = []
    for kind in ("video", "audio", "subtitle"):
        for idx in range(1, int(tl.GetTrackCount(kind)) + 1):
            items = []
            for n, item in enumerate(tl.GetItemListInTrack(kind, idx) or [], 1):
                entry = {
                    "index": n,
                    "name": item.GetName(),
                    "start_frame": item.GetStart(),
                    "end_frame": item.GetEnd(),
                    "start_sec": round(item.GetStart() / fps, 3),
                    "end_sec": round(item.GetEnd() / fps, 3),
                    "duration_sec": round(item.GetDuration() / fps, 3),
                }
                if kind == "video":
                    try:
                        entry["source_start_frame"] = item.GetLeftOffset()
                        entry["zoom"] = item.GetProperty("ZoomX")
                        entry["pan"] = item.GetProperty("Pan")
                        entry["tilt"] = item.GetProperty("Tilt")
                        mpi = item.GetMediaPoolItem()
                        entry["source_file"] = _clip_path(mpi) if mpi else None
                    except Exception:
                        pass
                items.append(entry)
            if items:
                tracks.append({"type": kind, "track": idx, "items": items})
    return {"status": "success", "timeline": tl.GetName(), "fps": fps,
            "start_timecode": tl.GetStartTimecode() if hasattr(tl, "GetStartTimecode") else None,
            "tracks": tracks}


@mcp.tool()
@_tool
def transcribe_clip(clip: str, language: str = "auto") -> dict:
    """Transcribe a clip (media pool name or file path) locally with whisper.cpp.
    Returns sentence segments with word-level timestamps in source-clip seconds.
    Use this to know what is said and where to cut. Results are cached."""
    path = _resolve_media_path(clip)
    result = tr.transcribe(path, language=language)
    return {"status": "success", "source": path, **result}


@mcp.tool()
@_tool
def detect_silences(clip: str, noise_db: float = -32.0, min_duration: float = 0.45) -> dict:
    """Detect silences in a clip (media pool name or file path) via ffmpeg.
    Returns silence windows in source-clip seconds — candidates for cutting."""
    path = _resolve_media_path(clip)
    silences = tr.detect_silences(path, noise_db=noise_db, min_duration=min_duration)
    return {"status": "success", "source": path, "count": len(silences), "silences": silences}


@mcp.tool()
@_tool
def build_edit(timeline_name: str, segments: list, set_current: bool = True) -> dict:
    """Build a NEW timeline from kept segments (the batch-cut workflow — the Resolve API
    cannot razor-split existing clips, so cuts are expressed as a segment list).
    Each segment: {"clip": <media pool name or path>, "in_sec": float, "out_sec": float}.
    Segments are appended in list order. The source timeline stays untouched."""
    _, project = ra.get_project()
    media_pool = project.GetMediaPool()
    if any(tl.GetName() == timeline_name for tl in
           (project.GetTimelineByIndex(i) for i in range(1, int(project.GetTimelineCount()) + 1)) if tl):
        raise ra.ResolveError(f"Timeline '{timeline_name}' bestaat al — kies een andere naam.")
    timeline = media_pool.CreateEmptyTimeline(timeline_name)
    if not timeline:
        raise ra.ResolveError(f"Kon timeline '{timeline_name}' niet aanmaken.")

    appended, failed = 0, []
    for i, seg in enumerate(segments):
        clip = ra.find_clip(media_pool, seg["clip"])
        fps = ra.clip_fps(clip)
        info = {
            "mediaPoolItem": clip,
            "startFrame": int(round(float(seg["in_sec"]) * fps)),
            "endFrame": int(round(float(seg["out_sec"]) * fps)) - 1,
        }
        result = media_pool.AppendToTimeline([info])
        if result and result[0]:
            appended += 1
        else:
            failed.append({"segment": i, **{k: seg[k] for k in ("clip", "in_sec", "out_sec")}})
    if set_current:
        project.SetCurrentTimeline(timeline)
    return {"status": "success" if not failed else "partial",
            "timeline": timeline_name, "appended": appended, "failed": failed}


@mcp.tool()
@_tool
def set_transforms(items: list, track: int = 1, timeline_name: str = "") -> dict:
    """Batch punch-in zooms / reframes on timeline video items.
    Each item: {"index": 1-based position on the track, "zoom": float (1.0 = none,
    1.2 = subtle punch-in, 1.5 = close-up), "pan": float?, "tilt": float?}.
    Pan/tilt are in pixels; positive tilt moves the image down (use to keep faces framed)."""
    _, project = ra.get_project()
    tl = ra.get_timeline(project, timeline_name or None)
    track_items = tl.GetItemListInTrack("video", track) or []
    applied, errors = 0, []
    for spec in items:
        idx = int(spec["index"])
        if idx < 1 or idx > len(track_items):
            errors.append(f"index {idx} buiten bereik (track heeft {len(track_items)} items)")
            continue
        item = track_items[idx - 1]
        ok = True
        if "zoom" in spec:
            z = float(spec["zoom"])
            ok &= bool(item.SetProperty("ZoomX", z)) and bool(item.SetProperty("ZoomY", z))
        if "pan" in spec:
            ok &= bool(item.SetProperty("Pan", float(spec["pan"])))
        if "tilt" in spec:
            ok &= bool(item.SetProperty("Tilt", float(spec["tilt"])))
        if ok:
            applied += 1
        else:
            errors.append(f"index {idx}: SetProperty gaf false terug")
    return {"status": "success" if not errors else "partial", "applied": applied, "errors": errors}


@mcp.tool()
@_tool
def add_subtitles_srt(srt_content: str, timeline_name: str = "") -> dict:
    """Put subtitles on the native subtitle track of a timeline by importing SRT content.
    Pass full SRT text (use transcribe_clip output; keep lines <= 42 chars).
    Styling is then controlled in Resolve's subtitle track inspector (once, for the track)."""
    _, project = ra.get_project()
    tl = ra.get_timeline(project, timeline_name or None)
    project.SetCurrentTimeline(tl)
    media_pool = project.GetMediaPool()

    os.makedirs(WORK_DIR, exist_ok=True)
    srt_path = os.path.join(WORK_DIR, f"subs-{int(time.time())}.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(srt_content)

    imported = media_pool.ImportMedia([srt_path])
    if not imported:
        raise ra.ResolveError("SRT-import in de media pool faalde.")
    # A subtitle track must exist before the SRT can be placed on the timeline.
    if int(tl.GetTrackCount("subtitle")) == 0:
        tl.AddTrack("subtitle")
    media_pool.AppendToTimeline(imported)
    track = int(tl.GetTrackCount("subtitle"))
    items = tl.GetItemListInTrack("subtitle", track) if track else []
    count = len(items or [])
    if count == 0:
        return {"status": "error", "srt_file": srt_path,
                "message": "SRT staat in de media pool maar landde niet op het subtitle-spoor. "
                           "Sleep hem handmatig op het spoor, of gebruik add_text_plus."}
    return {"status": "success", "timeline": tl.GetName(), "srt_file": srt_path,
            "subtitle_tracks": track, "subtitles_placed": count}


@mcp.tool()
@_tool
def add_text_plus(items: list, video_track: int = 2, timeline_name: str = "") -> dict:
    """Add styled Text+ title clips (subtitles/titles with full font control).
    Each item: {"text": str, "start_sec": float, "style": {optional Text+ inputs, e.g.
    "Size": 0.08, "Font": "Avenir Next", "Style": "Bold"}}. Items are inserted at their
    start position on the given video track. NOTE: the API cannot set clip duration,
    so Text+ clips get the default duration — best for titles/callouts; for dense
    subtitles prefer add_subtitles_srt."""
    _, project = ra.get_project()
    tl = ra.get_timeline(project, timeline_name or None)
    project.SetCurrentTimeline(tl)
    fps = ra.timeline_fps(tl)
    start_tc = ra.tc_to_frames(tl.GetStartTimecode(), fps) if hasattr(tl, "GetStartTimecode") else 0

    created, errors = 0, []
    for spec in items:
        frames = start_tc + int(round(float(spec["start_sec"]) * fps))
        tl.SetCurrentTimecode(ra.frames_to_tc(frames, fps))
        item = tl.InsertFusionTitleIntoTimeline("Text+")
        if not item:
            errors.append(f"insert faalde op {spec['start_sec']}s")
            continue
        try:
            comp = item.GetFusionCompByIndex(1)
            tool = comp.FindToolByID("TextPlus")
            tool.SetInput("StyledText", spec["text"])
            for key, val in (spec.get("style") or {}).items():
                tool.SetInput(key, val)
            created += 1
        except Exception as e:
            errors.append(f"{spec['start_sec']}s: tekst gezet? nee — {e}")
    return {"status": "success" if not errors else "partial", "created": created,
            "errors": errors, "note": "Duur per clip handmatig of via trim aanpassen; API kan dat niet."}


@mcp.tool()
@_tool
def create_subtitles_from_audio(timeline_name: str = "") -> dict:
    """Studio only: let Resolve itself transcribe the timeline audio and create
    a subtitle track (its built-in AI captions). Fails gracefully on Free."""
    resolve, project = ra.get_project()
    tl = ra.get_timeline(project, timeline_name or None)
    project.SetCurrentTimeline(tl)
    if not hasattr(tl, "CreateSubtitlesFromAudio"):
        raise ra.ResolveError("CreateSubtitlesFromAudio niet beschikbaar (gratis versie of oude Resolve). Gebruik transcribe_clip + add_subtitles_srt.")
    ok = tl.CreateSubtitlesFromAudio()
    return {"status": "success" if ok else "error",
            "message": "Resolve maakt ondertitels aan (kan even duren)." if ok else
            "Resolve weigerde — deze API-call is in de praktijk onbetrouwbaar. "
            "Gebruik transcribe_clip + add_subtitles_srt als betrouwbare route."}


@mcp.tool()
@_tool
def render_still(timecode: str = "", out_dir: str = "") -> dict:
    """Grab the current timeline frame (optionally at a given 'HH:MM:SS:FF' timecode)
    and export it as PNG so you can visually check your own edit. Returns the file path."""
    _, project = ra.get_project()
    tl = ra.get_timeline(project)
    if timecode:
        tl.SetCurrentTimecode(timecode)
    out_dir = out_dir or os.path.join(WORK_DIR, "stills")
    os.makedirs(out_dir, exist_ok=True)
    still = tl.GrabStill()
    if not still:
        raise ra.ResolveError("GrabStill faalde — staat er een timeline open met de playhead op beeld?")
    gallery = project.GetGallery()
    album = gallery.GetCurrentStillAlbum()
    prefix = f"still_{int(time.time())}"
    album.ExportStills([still], out_dir, prefix, "png")
    album.DeleteStills([still])
    pngs = sorted(glob.glob(os.path.join(out_dir, f"{prefix}*.png")), key=os.path.getmtime)
    if not pngs:
        raise ra.ResolveError("Export van still leverde geen PNG op.")
    return {"status": "success", "file": pngs[-1], "timecode": tl.GetCurrentTimecode()}


@mcp.tool()
@_tool
def add_markers(markers: list, timeline_name: str = "") -> dict:
    """Batch-add markers to a timeline to document edit decisions.
    Each marker: {"sec": float (timeline seconds, 0 = timeline start), "color": str
    (Blue/Cyan/Green/Yellow/Red/Pink/Purple/Fuchsia/Rose/Lavender/Sky/Mint/Lemon/
    Sand/Cocoa/Cream), "name": str, "note": str?, "duration_sec": float?}.
    Convention: Red = removed retake/flub here, Yellow = removed silence/filler,
    Blue = info. Markers sit on the timeline ruler, visible in Edit page."""
    _, project = ra.get_project()
    tl = ra.get_timeline(project, timeline_name or None)
    fps = ra.timeline_fps(tl)
    added, errors = 0, []
    for m in markers:
        frame = int(round(float(m["sec"]) * fps))
        dur = max(1, int(round(float(m.get("duration_sec", 0)) * fps)))
        ok = tl.AddMarker(frame, m.get("color", "Blue"), m.get("name", ""),
                          m.get("note", ""), dur, "")
        if ok:
            added += 1
        else:
            errors.append(f"marker op {m['sec']}s geweigerd (frame {frame})")
    return {"status": "success" if not errors else "partial", "added": added, "errors": errors}


@mcp.tool()
@_tool
def delete_timeline(timeline_name: str) -> dict:
    """Delete a timeline by exact name (e.g. an obsolete edit iteration).
    The media pool and source clips are untouched. Cannot be undone via API."""
    _, project = ra.get_project()
    tl = ra.get_timeline(project, timeline_name)
    mp = project.GetMediaPool()
    ok = mp.DeleteTimelines([tl])
    return {"status": "success" if ok else "error",
            "message": f"Timeline '{timeline_name}' verwijderd." if ok else "DeleteTimelines gaf false terug."}


@mcp.tool()
@_tool
def set_playhead(timecode: str) -> dict:
    """Move the playhead of the current timeline to 'HH:MM:SS:FF'."""
    _, project = ra.get_project()
    tl = ra.get_timeline(project)
    ok = tl.SetCurrentTimecode(timecode)
    return {"status": "success" if ok else "error", "timecode": tl.GetCurrentTimecode()}


@mcp.tool()
@_tool
def make_srt_from_transcript(clip: str, language: str = "auto", max_chars: int = 42) -> dict:
    """Convenience: transcribe a clip and return ready-to-use SRT text (source-clip time).
    If the timeline was re-cut with build_edit, remap times per segment before importing."""
    path = _resolve_media_path(clip)
    result = tr.transcribe(path, language=language)
    return {"status": "success", "source": path,
            "srt": tr.to_srt(result["segments"], max_chars=max_chars)}


if __name__ == "__main__":
    mcp.run()
