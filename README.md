# resolve-mcp-bridge

MCP server that lets Claude edit in DaVinci Resolve: it reads your timeline and
media pool, transcribes source audio locally (whisper.cpp, word-level timestamps),
and batch-applies edits — cuts, punch-in zooms/close-ups, and subtitles.

Works with DaVinci Resolve **Free and Studio** (validated against Resolve 21 Studio).
No panel or plugin needed: it talks to the official Python scripting API of the
running Resolve app.

## Requirements

- DaVinci Resolve running, with **Preferences > System > General > External
  scripting using: Local** (Studio default)
- Python 3.10+ (`brew install python@3.11`)
- `brew install ffmpeg whisper-cpp`
- A whisper model in `models/` (default: `ggml-large-v3-turbo-q5_0.bin`):

```sh
curl -L -o models/ggml-large-v3-turbo-q5_0.bin \
  "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
```

## Install

```sh
python3.11 -m venv .venv
./.venv/bin/pip install "mcp[cli]"
./.venv/bin/python scripts/validate.py   # end-to-end check against live Resolve
```

Register with Claude Code (adjust the absolute path):

```sh
claude mcp add resolve-bridge --scope user \
  --env "PYTHONPATH=<repo>/src" \
  -- "<repo>/.venv/bin/python" "<repo>/src/resolve_bridge/server.py"
```

## Tools

| Tool | What it does |
|---|---|
| `get_project_info` | Project, fps, resolution, Studio/Free, current timeline |
| `list_media` | All media pool clips with duration/fps/path |
| `get_timeline_items` | Every item per track with timings, zoom/pan state |
| `transcribe_clip` | Local whisper transcription, sentence + word timestamps (cached) |
| `detect_silences` | ffmpeg silence windows — cut candidates |
| `build_edit` | **Cuts**: build a new timeline from kept segments (source stays untouched) |
| `set_transforms` | **Zooms**: batch punch-ins/close-ups via ZoomX/Y + Pan/Tilt |
| `add_subtitles_srt` | **Subtitles** on the native subtitle track (style once in the Inspector) |
| `make_srt_from_transcript` | Transcribe → ready-to-import SRT text |
| `add_text_plus` | Styled Text+ titles/callouts (API can't set duration — titles, not dense subs) |
| `create_subtitles_from_audio` | Resolve's built-in AI captions (Studio; unreliable via API) |
| `render_still` | Export the current frame as PNG so Claude can check its own work |
| `set_playhead` | Move the playhead |

## Why cuts build a new timeline

The Resolve API cannot razor-split an existing clip. The reliable batch pattern
is: decide which segments to keep (transcript + silences), then append those
segments to a fresh timeline. One call = the whole rough cut, fully reversible.

## Typical workflow

1. `list_media` + `transcribe_clip` + `detect_silences` — Claude sees and hears the footage
2. Claude picks segments (drop silences, stumbles, retakes) → `build_edit`
3. `set_transforms` — alternate punch-ins per segment for emphasis/close-ups
4. `make_srt_from_transcript` → remap times to the new cut → `add_subtitles_srt`
5. `render_still` to verify frames visually; iterate
