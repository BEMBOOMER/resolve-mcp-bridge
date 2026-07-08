# resolve-mcp-bridge

> Nederlands? Zie [README.nl.md](README.nl.md) voor de korte uitleg en installatie.

MCP server that lets Claude edit in DaVinci Resolve: it reads your timeline and
media pool, transcribes source audio locally (whisper.cpp, word-level timestamps),
and batch-applies edits — cuts, punch-in zooms/close-ups, subtitles, music beds,
SFX placement, beat-grid detection, and a self-check QA report.

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

## Regiekamer (live monitor + controller)

Start `./.venv/bin/python scripts/monitor.py` and open http://127.0.0.1:8765 —
a DaVinci-styled dashboard showing live pipeline progress (phases, timeline
tracks, QA, event feed) while Claude edits. It is also a controller: send
instructions, point at a timecode, comment on a clip/subtitle, drag video clips
to reorder, pause/approve/redo. Commands land in Claude's queue and are picked
up between pipeline phases.

## Team setup (collega's met Claude Code)

Volledige setup vanaf nul, per persoon (macOS):

```sh
git clone <repo-url> && cd resolve-mcp-bridge
brew install python@3.11 ffmpeg whisper-cpp
python3.11 -m venv .venv && ./.venv/bin/pip install "mcp[cli]"
curl -L -o models/ggml-large-v3-turbo-q5_0.bin \
  "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
./.venv/bin/python scripts/test_helpers.py     # offline sanity check (geen Resolve nodig)
```

Dan Resolve openen (Preferences > System > General > External scripting: **Local**),
de `claude mcp add`-regel hierboven draaien met jouw absolute repo-pad, en de
edit-skill installeren zodat Claude het volledige draaiboek volgt:

```sh
mkdir -p ~/.claude/skills && cp -R skills/davinci-edit ~/.claude/skills/
```

Integratietest met Resolve open: `./.venv/bin/python scripts/validate.py`.

De skill (`skills/davinci-edit/SKILL.md`) bevat de volledige werkwijze: editbrief
lezen, genre-bewust knippen, subs exact op het gesproken woord, muziekbed + SFX,
en een verplicht `qa_report()` vóór oplevering. Belangrijk voor de batch-pipeline:
`scripts/base_layer.py` (helpers) — env-overrides `RESOLVE_BRIDGE_WHISPER_MODEL`
en `RESOLVE_BRIDGE_WHISPER_BIN` als je model of binary ergens anders staat.

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
| `make_srt_from_transcript` | Transcribe → ready-to-import SRT; pass the `build_edit` segments to get it remapped to the re-cut timeline |
| `add_text_plus` | Styled Text+ titles/callouts on any track, with position + duration, non-destructive (nested title timelines) |
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
4. `make_srt_from_transcript` with the same segments (auto-remapped to the new cut) → `add_subtitles_srt`
5. `render_still` to verify frames visually; iterate
