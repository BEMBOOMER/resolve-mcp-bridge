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
        entry = {"folder": folder, "name": clip.GetName(), **{k.lower().replace(" ", "_"): v for k, v in props.items()}}
        try:
            entry["duration_sec"] = round(ra.tc_to_frames(props["Duration"], float(props["FPS"])) / float(props["FPS"]), 3)
        except Exception:
            pass
        clips.append(entry)
    return {"status": "success", "count": len(clips), "clips": clips}


@mcp.tool()
@_tool
def get_timeline_items(timeline_name: str = "") -> dict:
    """All items on a timeline (current one by default): per track, with start/end/source
    timings in frames (absolute) and seconds (relative to timeline start, so 0 = first
    frame — the same time base add_markers and add_text_plus use), plus zoom/pan values."""
    _, project = ra.get_project()
    tl = ra.get_timeline(project, timeline_name or None)
    fps = ra.timeline_fps(tl)
    tl_start = ra.timeline_start_frame(tl, fps)
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
                    "start_sec": round((item.GetStart() - tl_start) / fps, 3),
                    "end_sec": round((item.GetEnd() - tl_start) / fps, 3),
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
    for i, seg in enumerate(segments):
        if float(seg["out_sec"]) <= float(seg["in_sec"]):
            raise ra.ResolveError(f"Segment {i}: out_sec ({seg['out_sec']}) moet groter zijn dan in_sec ({seg['in_sec']}).")
    previous = project.GetCurrentTimeline()
    timeline = media_pool.CreateEmptyTimeline(timeline_name)
    if not timeline:
        raise ra.ResolveError(f"Kon timeline '{timeline_name}' niet aanmaken.")

    appended, failed, clamped = 0, [], []
    for i, seg in enumerate(segments):
        clip = ra.find_clip(media_pool, seg["clip"])
        fps = ra.clip_fps(clip)
        start_f = ra.sec_to_frames(seg["in_sec"], fps)
        end_f = ra.sec_to_frames(seg["out_sec"], fps)
        # clamp to the source length — Resolve would otherwise silently place
        # freeze frames beyond the media end (out_sec 999 -> a 993s item).
        clip_frames = ra.clip_frame_count(clip)
        if clip_frames:
            if start_f >= clip_frames:
                failed.append({"segment": i, "reason": f"in_sec voorbij clipeinde ({round(clip_frames / fps, 2)}s)",
                               **{k: seg[k] for k in ("clip", "in_sec", "out_sec")}})
                continue
            if end_f > clip_frames:
                clamped.append({"segment": i, "out_sec": round(clip_frames / fps, 3)})
                end_f = clip_frames
        # endFrame is exclusive in AppendToTimeline: 0..25 places exactly 25 frames.
        info = {"mediaPoolItem": clip, "startFrame": start_f, "endFrame": end_f}
        result = media_pool.AppendToTimeline([info])
        if result and result[0]:
            appended += 1
        else:
            failed.append({"segment": i, **{k: seg[k] for k in ("clip", "in_sec", "out_sec")}})
    if set_current:
        project.SetCurrentTimeline(timeline)
    elif previous:
        project.SetCurrentTimeline(previous)
    out = {"status": "success" if not failed else "partial",
           "timeline": timeline_name, "appended": appended, "failed": failed}
    if clamped:
        out["clamped_to_clip_end"] = clamped
    return out


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
    srt_path = os.path.join(WORK_DIR, f"subs-{time.time_ns()}.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(srt_content)

    imported = media_pool.ImportMedia([srt_path])
    if not imported:
        raise ra.ResolveError("SRT-import in de media pool faalde.")
    # A subtitle track must exist before the SRT can be placed on the timeline.
    if int(tl.GetTrackCount("subtitle")) == 0:
        tl.AddTrack("subtitle")
    track = int(tl.GetTrackCount("subtitle"))
    before = len(tl.GetItemListInTrack("subtitle", track) or [])
    media_pool.AppendToTimeline(imported)
    items = tl.GetItemListInTrack("subtitle", track) if track else []
    count = len(items or []) - before
    if count <= 0:
        return {"status": "error", "srt_file": srt_path,
                "message": "SRT staat in de media pool maar landde niet op het subtitle-spoor. "
                           "Sleep hem handmatig op het spoor, of gebruik add_text_plus."}
    return {"status": "success", "timeline": tl.GetName(), "srt_file": srt_path,
            "subtitle_tracks": track, "subtitles_placed": count}


def _titles_folder(media_pool):
    root = media_pool.GetRootFolder()
    for sub in root.GetSubFolderList() or []:
        if sub.GetName() == "MCP Titles":
            return sub
    folder = media_pool.AddSubFolder(root, "MCP Titles")
    if not folder:
        raise ra.ResolveError("Kon media pool-map 'MCP Titles' niet aanmaken.")
    return folder


def _make_title_timeline(project, media_pool, folder, spec, fps, name):
    """Build a helper timeline holding only the styled Text+, long enough for the
    requested duration (Text+ inserts are 5s each, so repeat until covered)."""
    media_pool.SetCurrentFolder(folder)
    try:
        title_tl = media_pool.CreateEmptyTimeline(name)
        if not title_tl:
            raise ra.ResolveError(f"Kon titel-timeline '{name}' niet aanmaken.")
        project.SetCurrentTimeline(title_tl)
        need = int(round(float(spec.get("duration_sec", 4.0)) * fps))
        tl_start = ra.timeline_start_frame(title_tl, fps)
        covered = 0
        while covered < need:
            title_tl.SetCurrentTimecode(ra.frames_to_tc(tl_start + covered, fps))
            item = title_tl.InsertFusionTitleIntoTimeline("Text+")
            if not item:
                raise ra.ResolveError("InsertFusionTitleIntoTimeline faalde in de titel-timeline.")
            comp = item.GetFusionCompByIndex(1)
            tool = comp.FindToolByID("TextPlus")
            tool.SetInput("StyledText", spec["text"])
            for key, val in (spec.get("style") or {}).items():
                tool.SetInput(key, val)
            covered += int(item.GetDuration())
    finally:
        media_pool.SetCurrentFolder(media_pool.GetRootFolder())
    for _, clip in ra.iter_media_pool_clips(media_pool, folder, "MCP Titles"):
        if clip.GetName() == name:
            return clip
    raise ra.ResolveError(f"Titel-timeline '{name}' niet teruggevonden in de media pool.")


@mcp.tool()
@_tool
def add_text_plus(items: list, video_track: int = 2, timeline_name: str = "") -> dict:
    """Add styled Text+ title clips on the requested video track, non-destructively.
    Each item: {"text": str, "start_sec": float (timeline seconds, 0 = timeline start),
    "duration_sec": float? (default 4.0), "style": {optional Text+ inputs, e.g.
    "Size": 0.08, "Font": "Avenir Next", "Style": "Bold"}}.
    Each title lives in its own helper timeline (media pool folder 'MCP Titles') and is
    nested onto the target timeline at the exact position and duration — no ripple, no
    splits, existing clips stay untouched. Restyle later by opening the helper timeline."""
    _, project = ra.get_project()
    tl = ra.get_timeline(project, timeline_name or None)
    if video_track < 1:
        raise ra.ResolveError(f"video_track moet >= 1 zijn (kreeg {video_track}).")
    fps = ra.timeline_fps(tl)
    tl_start = ra.timeline_start_frame(tl, fps)
    media_pool = project.GetMediaPool()
    folder = _titles_folder(media_pool)
    project.SetCurrentTimeline(tl)
    while int(tl.GetTrackCount("video")) < video_track:
        if not tl.AddTrack("video"):
            raise ra.ResolveError(f"Kon videotrack {video_track} niet aanmaken.")

    created, errors = 0, []
    for spec in items:
        dur = int(round(float(spec.get("duration_sec", 4.0)) * fps))
        if dur < 1:
            errors.append(f"{spec.get('start_sec')}s: duration_sec te kort ({spec.get('duration_sec')}).")
            continue
        rec = tl_start + ra.sec_to_frames(spec["start_sec"], fps)
        # Resolve would silently shift/trim an overlapping placement into the free
        # gap — check up front and refuse instead.
        existing = tl.GetItemListInTrack("video", video_track) or []
        clash = next((e for e in existing if e.GetStart() < rec + dur and e.GetEnd() > rec), None)
        if clash:
            errors.append(f"{spec['start_sec']}s: overlapt '{clash.GetName()}' op V{video_track} "
                          f"— kies een andere start_sec/duration_sec of track.")
            continue
        name = f"_title-{time.time_ns()}"
        try:
            title_clip = _make_title_timeline(project, media_pool, folder, spec, fps, name)
        except ra.ResolveError as e:
            errors.append(f"{spec.get('start_sec')}s: {e}")
            continue
        project.SetCurrentTimeline(tl)
        result = media_pool.AppendToTimeline([{
            "mediaPoolItem": title_clip, "startFrame": 0, "endFrame": dur,
            "trackIndex": video_track, "recordFrame": rec, "mediaType": 1}])
        if result and result[0]:
            created += 1
        else:
            errors.append(f"{spec['start_sec']}s: plaatsing op V{video_track} faalde "
                          f"(overlapt daar al een clip?)")
    project.SetCurrentTimeline(tl)
    return {"status": "success" if not errors else "partial", "created": created,
            "track": video_track, "errors": errors,
            "note": "Titels zijn geneste timelines uit de map 'MCP Titles'; open die om te herstylen."}


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
    try:
        # Resolve 19+ wants a settings dict; the bare call returns False there.
        ok = tl.CreateSubtitlesFromAudio({})
    except TypeError:
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
    length = tl.GetEndFrame() - tl.GetStartFrame()
    added, errors = 0, []
    for m in markers:
        frame = int(round(float(m["sec"]) * fps))
        if frame < 0 or frame >= length:
            errors.append(f"marker op {m['sec']}s valt buiten de timeline (duur {round(length / fps, 2)}s)")
            continue
        dur = max(1, int(round(float(m.get("duration_sec", 0)) * fps)))
        ok = tl.AddMarker(frame, m.get("color", "Blue"), m.get("name", ""),
                          m.get("note", ""), dur, "")
        if ok:
            added += 1
        else:
            errors.append(f"marker op {m['sec']}s geweigerd (frame {frame}; staat er al een marker op dat frame?)")
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
def make_srt_from_transcript(clip: str, language: str = "auto", max_chars: int = 42,
                             segments: list = None) -> dict:
    """Convenience: transcribe a clip and return ready-to-use SRT text.
    Without `segments` the times are in source-clip time. Pass the SAME segment list
    you gave build_edit ([{"clip", "in_sec", "out_sec"}, ...]) to get the SRT remapped
    into timeline time of the re-cut edit: words in removed ranges are dropped and the
    rest shifts to match the new timeline, ready for add_subtitles_srt."""
    path = _resolve_media_path(clip)
    result = tr.transcribe(path, language=language)
    trans_segments = result["segments"]
    if segments:
        windows, timeline_pos = [], 0.0
        for seg in segments:
            dur = float(seg["out_sec"]) - float(seg["in_sec"])
            if dur <= 0:
                raise ra.ResolveError(f"Segment {seg}: out_sec moet groter zijn dan in_sec.")
            try:
                seg_path = _resolve_media_path(seg["clip"])
            except ra.ResolveError:
                seg_path = None
            if seg_path == path:
                windows.append((float(seg["in_sec"]), float(seg["out_sec"]), timeline_pos))
            timeline_pos += dur
        trans_segments = tr.remap_segments(trans_segments, windows)
    return {"status": "success", "source": path,
            "remapped": bool(segments),
            "srt": tr.to_srt(trans_segments, max_chars=max_chars)}


if __name__ == "__main__":
    mcp.run()
