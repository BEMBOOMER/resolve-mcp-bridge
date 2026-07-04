#!/usr/bin/env python3
"""Phase 0: validate every Resolve API assumption live, end to end.

Creates a throwaway project 'resolve-bridge-validation', generates a test clip
with Dutch speech (say + ffmpeg), then exercises each bridge tool against it.
Run with Resolve open:  .venv/bin/python scripts/validate.py
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from resolve_bridge import server as srv  # noqa: E402
from resolve_bridge import resolve_api as ra  # noqa: E402

WORK = os.path.expanduser("~/.cache/resolve-mcp-bridge/validation")
RESULTS = []


def check(name, result, ok=None):
    if ok is None:
        ok = isinstance(result, dict) and result.get("status") in ("success", "partial")
    RESULTS.append((name, ok, result))
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      -> {result}")
    return ok


def make_test_clip():
    os.makedirs(WORK, exist_ok=True)
    clip = os.path.join(WORK, "bridge-test-clip.mp4")
    if os.path.exists(clip):
        return clip
    text = ("Hallo, dit is een testclip voor de resolve bridge. "
            "Nu volgt een korte stilte. [[slnc 1500]] "
            "En daarna praten we gewoon weer verder over de edit, "
            "met cuts, zoom ins en ondertitels.")
    speech = os.path.join(WORK, "speech.aiff")
    subprocess.run(["say", "-v", "Xander", "-o", speech, text], check=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25",
        "-i", speech, "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", clip], check=True, capture_output=True)
    return clip


def main():
    print("== resolve-bridge validation ==\n")

    # 1. connection
    try:
        resolve = ra.get_resolve()
        print(f"PASS  verbinding — {resolve.GetVersionString()}, studio={ra.is_studio(resolve)}")
        RESULTS.append(("verbinding", True, None))
    except ra.ResolveError as e:
        print(f"FAIL  verbinding: {e}")
        sys.exit(1)

    # 2. throwaway project
    pm = resolve.GetProjectManager()
    name = "resolve-bridge-validation"
    project = pm.LoadProject(name) or pm.CreateProject(name)
    if not project:
        print("FAIL  kon validatieproject niet aanmaken")
        sys.exit(1)
    print(f"PASS  project '{name}'")

    # 3. test media + import
    clip_path = make_test_clip()
    media_pool = project.GetMediaPool()
    existing = [c for _, c in ra.iter_media_pool_clips(media_pool)
                if (c.GetClipProperty("File Path") or "") == clip_path]
    items = existing or resolve.GetMediaStorage().AddItemListToMediaPool([clip_path])
    check("media-import", {"status": "success" if items else "error"}, bool(items))

    check("get_project_info", srv.get_project_info())
    check("list_media", srv.list_media())

    # 4. transcriptie + stiltes (los van Resolve)
    t = srv.transcribe_clip(clip_path, language="nl")
    ok = t.get("status") == "success" and t.get("segments")
    check("transcribe_clip", t if not ok else {"status": "success"}, bool(ok))
    if ok:
        print(f"      -> \"{t['text'][:80]}...\" ({len(t['segments'])} segmenten, "
              f"{sum(len(s['words']) for s in t['segments'])} woorden)")
    check("detect_silences", srv.detect_silences(clip_path))

    # 5. cuts: nieuwe timeline uit segmenten
    tl_name = f"validation-cut-{int(time.time())}"
    edit = srv.build_edit(tl_name, [
        {"clip": clip_path, "in_sec": 0.5, "out_sec": 3.0},
        {"clip": clip_path, "in_sec": 6.0, "out_sec": 9.0},
        {"clip": clip_path, "in_sec": 10.0, "out_sec": 12.0},
    ])
    check("build_edit (cuts)", edit)

    check("get_timeline_items", srv.get_timeline_items())

    # 6. punch-in zooms
    check("set_transforms (zoom)", srv.set_transforms(
        [{"index": 2, "zoom": 1.25}, {"index": 3, "zoom": 1.5, "tilt": -40}]))

    # 7. native subtitles via SRT
    srt = srv.make_srt_from_transcript(clip_path, language="nl")
    check("make_srt_from_transcript", srt)
    if srt.get("status") == "success":
        check("add_subtitles_srt", srv.add_subtitles_srt(srt["srt"]))

    # 8. Text+ titel
    check("add_text_plus", srv.add_text_plus(
        [{"text": "VALIDATIE", "start_sec": 0.5, "style": {"Size": 0.12}}]))

    # 9. still renderen
    check("render_still", srv.render_still(timecode="01:00:01:00"))

    # 10. Studio-only captions (mag falen op free)
    r = srv.create_subtitles_from_audio()
    print(f"INFO  create_subtitles_from_audio (Studio): {r.get('status')} — {r.get('message', '')}")

    print("\n== samenvatting ==")
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks geslaagd" +
          (f" — gefaald: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
