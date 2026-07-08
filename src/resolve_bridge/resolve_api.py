"""Connection layer to the DaVinci Resolve scripting API.

Resolve must be running. On the free version, external scripting must be
allowed (Preferences > System > General > External scripting using: Local).
Works on both Free and Studio; Studio-only features are guarded at call time.
"""

import os
import sys

RESOLVE_SCRIPT_API = os.environ.get(
    "RESOLVE_SCRIPT_API",
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
)
RESOLVE_SCRIPT_LIB = os.environ.get(
    "RESOLVE_SCRIPT_LIB",
    "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so",
)

os.environ["RESOLVE_SCRIPT_API"] = RESOLVE_SCRIPT_API
os.environ["RESOLVE_SCRIPT_LIB"] = RESOLVE_SCRIPT_LIB
_modules = os.path.join(RESOLVE_SCRIPT_API, "Modules")
if _modules not in sys.path:
    sys.path.append(_modules)


class ResolveError(RuntimeError):
    pass


def get_resolve():
    """Fresh connection to the running Resolve instance."""
    try:
        import DaVinciResolveScript as dvr
    except ImportError as e:
        raise ResolveError(
            f"Kan DaVinciResolveScript niet importeren ({e}). "
            f"Controleer RESOLVE_SCRIPT_API: {RESOLVE_SCRIPT_API}"
        )
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise ResolveError(
            "Geen verbinding met DaVinci Resolve. Draait de app? "
            "Zo ja: zet Preferences > System > General > "
            "'External scripting using' op 'Local' en herstart Resolve."
        )
    return resolve


def get_project(resolve=None):
    resolve = resolve or get_resolve()
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    if project is None:
        raise ResolveError("Geen project geopend in Resolve.")
    return resolve, project


def get_timeline(project, name=None):
    """Current timeline, or look one up by name."""
    if name:
        for i in range(1, int(project.GetTimelineCount()) + 1):
            tl = project.GetTimelineByIndex(i)
            if tl and tl.GetName() == name:
                return tl
        raise ResolveError(f"Timeline '{name}' niet gevonden.")
    tl = project.GetCurrentTimeline()
    if tl is None:
        raise ResolveError("Geen timeline actief in het project.")
    return tl


def is_studio(resolve):
    """Best-effort Studio detection; None if unknown."""
    try:
        name = resolve.GetProductName()
        return "studio" in str(name).lower()
    except Exception:
        return None


def iter_media_pool_clips(media_pool, folder=None, path=""):
    """Yield (folder_path, mediaPoolItem) recursively through the media pool."""
    folder = folder or media_pool.GetRootFolder()
    here = f"{path}/{folder.GetName()}" if path else folder.GetName()
    for clip in folder.GetClipList() or []:
        yield here, clip
    for sub in folder.GetSubFolderList() or []:
        yield from iter_media_pool_clips(media_pool, sub, here)


def find_clip(media_pool, name_or_path):
    """Find a media pool clip by exact name or by source file path/basename."""
    target = os.path.basename(name_or_path)
    matches = []
    for _, clip in iter_media_pool_clips(media_pool):
        clip_name = clip.GetName()
        file_path = clip.GetClipProperty("File Path") or ""
        if name_or_path in (clip_name, file_path) or target in (
            clip_name,
            os.path.basename(file_path),
        ):
            matches.append(clip)
    if not matches:
        raise ResolveError(f"Clip '{name_or_path}' niet gevonden in de media pool.")
    return matches[0]


def clip_fps(clip):
    val = clip.GetClipProperty("FPS")
    try:
        return float(val)
    except (TypeError, ValueError):
        raise ResolveError(f"Geen FPS-property op clip {clip.GetName()!r} (kreeg {val!r}).")


def clip_frame_count(clip):
    """Length of a media pool clip in frames, or None if unknown."""
    frames = clip.GetClipProperty("Frames")
    try:
        return int(frames)
    except (TypeError, ValueError):
        pass
    try:
        return tc_to_frames(clip.GetClipProperty("Duration"), clip_fps(clip))
    except Exception:
        return None


def timeline_fps(timeline):
    return float(timeline.GetSetting("timelineFrameRate"))


def timeline_start_frame(timeline, fps):
    """Absolute frame number of the timeline's first frame (start timecode offset)."""
    if hasattr(timeline, "GetStartTimecode"):
        try:
            return tc_to_frames(timeline.GetStartTimecode(), fps)
        except Exception:
            pass
    return int(timeline.GetStartFrame())


def sec_to_frames(sec, fps):
    """Seconds -> frames with half-up rounding (round() is banker's: 12.5 -> 12)."""
    return int(float(sec) * fps + 0.5)


def tc_to_frames(tc, fps):
    """'HH:MM:SS:FF' -> absolute frame count (nominal rate, mirrors frames_to_tc)."""
    h, m, s, f = (int(x) for x in tc.replace(";", ":").split(":"))
    fps_i = int(round(fps))
    return (h * 3600 + m * 60 + s) * fps_i + f


def frames_to_tc(frames, fps):
    fps_i = int(round(fps))
    f = int(frames) % fps_i
    total_s = int(frames) // fps_i
    return f"{total_s // 3600:02d}:{(total_s % 3600) // 60:02d}:{total_s % 60:02d}:{f:02d}"
