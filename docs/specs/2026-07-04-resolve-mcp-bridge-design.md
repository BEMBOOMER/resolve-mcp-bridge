# resolve-mcp-bridge — design

2026-07-04 · Roelof (BEMBOOMER) · goedgekeurd in sessie

## Doel

Claude laat editen in DaVinci Resolve: clips zien, weten wat er gezegd wordt,
en batch-edits maken — puur cuts, zoom-ins/close-ups en ondertitels.

## Beslissingen

- **Vorm:** Python MCP-server (FastMCP, stdio) die direct op de officiële
  `DaVinciResolveScript`-API praat. Geen paneel/file-bridge zoals bij
  ae-mcp-bridge nodig: Resolve's API is al een out-of-process Python-module.
- **Gratis + Studio:** alles draait op de gratis versie; Studio-only features
  (ingebouwde captions) zijn guarded. Validatie draaide op Resolve 21 Studio.
- **Transcriptie:** lokaal via whisper.cpp (`whisper-cli`, brew) met
  `ggml-large-v3-turbo-q5_0`. Token-offsets worden samengevoegd tot
  woord-timestamps. Cache in `~/.cache/resolve-mcp-bridge/` op mtime+model.
  Resolve's eigen `CreateSubtitlesFromAudio` bleek via de API onbetrouwbaar
  (returnt `False`, ook met settings) — whisper is de primaire route.
- **Cuts:** de API kan bestaande clips niet razor-splitsen. Patroon: Claude
  kiest segmenten (op transcript + stiltes), server bouwt een **nieuwe
  timeline** via `AppendToTimeline` met in/out-frames. Origineel blijft staan.
- **Zooms:** `SetProperty("ZoomX"/"ZoomY"/"Pan"/"Tilt")` per timeline-item,
  batch over een lijst. 1.2 = subtiele punch-in, 1.5 = close-up.
- **Ondertitels:** primair native subtitle-track via SRT-import
  (media pool `ImportMedia` + `AppendToTimeline` — gevalideerd werkend);
  styling één keer in de Inspector per track. Secundair Text+ via
  `InsertFusionTitleIntoTimeline` voor gestylde titels/callouts —
  let op: API kan clipduur niet zetten, dus niet voor dichte subs.
- **Zelfcontrole:** `render_still` (GrabStill + gallery-export naar PNG),
  zelfde workflow-upgrade als `preview_frame` bij ae-mcp-bridge.

## Componenten

- `src/resolve_bridge/resolve_api.py` — verbinding, project/timeline/clip
  helpers, frame/timecode-wiskunde
- `src/resolve_bridge/transcribe.py` — ffmpeg-extractie, whisper-cli JSON,
  woord-merge, stiltedetectie, SRT-generatie (max 42 tekens/regel)
- `src/resolve_bridge/server.py` — 13 MCP-tools, fouten als leesbare
  `{status: error}` payloads i.p.v. exceptions
- `scripts/validate.py` — end-to-end validatie met zelf-gegenereerde
  spraak-testclip (`say -v Xander` + ffmpeg testsrc2)

## Validatie (2026-07-04, Resolve 21.0.0.47 Studio)

13/13 checks geslaagd: verbinding, project, media-import, project-info,
list_media, transcriptie (NL, 29 woorden), stiltes, build_edit (3 segmenten),
timeline-items, zooms, SRT-generatie, SRT op subtitle-track, Text+, still-export.
Enige uitval: `CreateSubtitlesFromAudio` (zie boven, bewust gedegradeerd).
