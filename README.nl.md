# resolve-mcp-bridge — korte uitleg (NL)

Deze tool laat Claude Code rechtstreeks editen in DaVinci Resolve: transcriberen
(lokaal, gratis), knippen, ondertitels plaatsen, muziek en SFX op de timeline
zetten en zichzelf controleren met een QA-rapport. Jij houdt de regie: Claude
bouwt base layers, jij doet de creatieve afwerking en de export.

## Installatie (eenmalig, ~10 minuten)

1. Zorg dat je hebt: DaVinci Resolve (Free of Studio), [Claude Code](https://claude.com/claude-code)
   met abonnement, en Homebrew.
2. Terminal:

```sh
git clone <repo-url> && cd resolve-mcp-bridge
brew install python@3.11 ffmpeg whisper-cpp
python3.11 -m venv .venv && ./.venv/bin/pip install "mcp[cli]"
curl -L -o models/ggml-large-v3-turbo-q5_0.bin \
  "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
./.venv/bin/python scripts/test_helpers.py   # moet "ALLES GROEN" geven
```

3. Resolve: **Preferences > System > General > External scripting using: Local**
   (daarna Resolve herstarten).
4. Bridge registreren bij Claude Code (vervang `<repo>` door jouw absolute pad):

```sh
claude mcp add resolve-bridge --scope user \
  --env "PYTHONPATH=<repo>/src" \
  -- "<repo>/.venv/bin/python" "<repo>/src/resolve_bridge/server.py"
```

5. De edit-skill installeren (het draaiboek dat Claude volgt):

```sh
mkdir -p ~/.claude/skills && cp -R skills/davinci-edit ~/.claude/skills/
```

## Gebruik

Open je Resolve-project, start Claude Code en typ bijvoorbeeld:

- `/davinci-edit` — alle nieuwe raw-clip-mappen tot base layers verwerken
- `/davinci-edit V3 V4` — alleen die mappen
- "maak een QA-rapport van timeline V6.1"
- "leg een muziekbed onder V2.1 met de Comedy Funny track"

Claude leest eerst de editbrief (`script/`-map van het project), knipt genre-bewust
(sketches houden hun stiltes, pratende video's worden strak), zet subs exact op het
gesproken woord en controleert zichzelf met `qa_report()` vóór oplevering.

## Regiekamer (meekijken + sturen)

Start `./.venv/bin/python scripts/monitor.py` en open http://127.0.0.1:8765 —
een dashboard in DaVinci-stijl met live voortgang (fases, timeline, QA, feed)
terwijl Claude edit. Ook een afstandsbediening: instructies sturen, op de
timeline aanwijzen, notities op een clip of sub, clips verslepen om te
verplaatsen, pauzeren/goedkeuren/opnieuw.

## Spelregels

- Claude **exporteert geen finals** zonder expliciete vraag (check-renders naar de
  werkmap mogen) en verwijdert **nooit** bestaande timelines; het bouwt altijd
  nieuwe. Burn-in van subs is een Deliver-instelling die je zelf zet.
- Werkt het niet? Draai `scripts/validate.py` met Resolve open en check dat de
  External-scripting-instelling op Local staat.

Volledige (Engelse) documentatie: [README.md](README.md).
