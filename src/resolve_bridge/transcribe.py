"""Local transcription via whisper.cpp (whisper-cli) with word timestamps.

Pipeline: ffmpeg extracts mono 16kHz WAV -> whisper-cli writes full JSON
(segments + tokens) -> tokens are merged back into words with timings.
Results are cached per source file + model, keyed by mtime.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile

MODEL_PATH = os.environ.get(
    "RESOLVE_BRIDGE_WHISPER_MODEL",
    os.path.join(os.path.dirname(__file__), "..", "..", "models", "ggml-large-v3-turbo-q5_0.bin"),
)
CACHE_DIR = os.path.expanduser("~/.cache/resolve-mcp-bridge")


class TranscribeError(RuntimeError):
    pass


def _require(binary):
    path = shutil.which(binary)
    if not path:
        raise TranscribeError(f"'{binary}' niet gevonden op PATH. Installeer via: brew install {binary}")
    return path


def _cache_key(media_path, language):
    stat = os.stat(media_path)
    raw = f"{media_path}:{stat.st_mtime_ns}:{stat.st_size}:{os.path.basename(MODEL_PATH)}:{language}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _extract_wav(media_path, wav_path):
    ffmpeg = _require("ffmpeg")
    cmd = [ffmpeg, "-y", "-i", media_path, "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", wav_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise TranscribeError(f"ffmpeg audio-extractie faalde: {proc.stderr[-800:]}")


def _tokens_to_words(segment):
    """Merge whisper.cpp subword tokens into words with start/end seconds."""
    words = []
    current = None
    for tok in segment.get("tokens", []):
        text = tok.get("text", "")
        if text.startswith("[_") or not text.strip():
            continue
        start = tok["offsets"]["from"] / 1000.0
        end = tok["offsets"]["to"] / 1000.0
        if text.startswith(" ") or current is None:
            if current:
                words.append(current)
            current = {"word": text.strip(), "start": start, "end": end}
        else:
            current["word"] += text
            current["end"] = end
    if current:
        words.append(current)
    return words


def transcribe(media_path, language="auto"):
    """Transcribe a media file. Returns {language, text, segments:[{start,end,text,words}]}."""
    media_path = os.path.abspath(os.path.expanduser(media_path))
    if not os.path.exists(media_path):
        raise TranscribeError(f"Bestand niet gevonden: {media_path}")
    model = os.path.abspath(MODEL_PATH)
    if not os.path.exists(model):
        raise TranscribeError(f"Whisper-model niet gevonden: {model}")

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, _cache_key(media_path, language) + ".json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    whisper = _require("whisper-cli")
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "audio.wav")
        _extract_wav(media_path, wav)
        out_base = os.path.join(tmp, "result")
        cmd = [whisper, "-m", model, "-f", wav, "-l", language,
               "-ojf", "-of", out_base, "-np"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        json_path = out_base + ".json"
        if proc.returncode != 0 or not os.path.exists(json_path):
            raise TranscribeError(f"whisper-cli faalde: {proc.stderr[-800:]}")
        with open(json_path) as f:
            raw = json.load(f)

    segments = []
    for seg in raw.get("transcription", []):
        text = seg.get("text", "").strip()
        if not text:
            continue
        segments.append({
            "start": seg["offsets"]["from"] / 1000.0,
            "end": seg["offsets"]["to"] / 1000.0,
            "text": text,
            "words": _tokens_to_words(seg),
        })

    result = {
        "language": raw.get("result", {}).get("language", language),
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
    }
    with open(cache_file, "w") as f:
        json.dump(result, f, ensure_ascii=False)
    return result


def detect_silences(media_path, noise_db=-32.0, min_duration=0.45):
    """ffmpeg silencedetect -> [{start, end, duration}] in seconds."""
    media_path = os.path.abspath(os.path.expanduser(media_path))
    if not os.path.exists(media_path):
        raise TranscribeError(f"Bestand niet gevonden: {media_path}")
    ffmpeg = _require("ffmpeg")
    cmd = [ffmpeg, "-i", media_path, "-af",
           f"silencedetect=noise={noise_db}dB:d={min_duration}", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    silences, start = [], None
    for line in proc.stderr.splitlines():
        line = line.strip()
        if "silence_start:" in line:
            start = float(line.split("silence_start:")[1].strip())
        elif "silence_end:" in line and start is not None:
            parts = line.split("silence_end:")[1].split("|")
            end = float(parts[0].strip())
            silences.append({"start": start, "end": end, "duration": round(end - start, 3)})
            start = None
    return silences


def to_srt(segments, max_chars=42):
    """Transcript segments -> SRT text. Splits long segments on word timings."""
    def fmt(t):
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"

    entries = []
    for seg in segments:
        words = seg.get("words") or []
        if len(seg["text"]) <= max_chars or len(words) < 2:
            entries.append((seg["start"], seg["end"], seg["text"]))
            continue
        # split on word boundaries into chunks of <= max_chars
        chunk, chunk_start = [], words[0]["start"]
        for w in words:
            candidate = " ".join(x["word"] for x in chunk + [w])
            if chunk and len(candidate) > max_chars:
                entries.append((chunk_start, chunk[-1]["end"], " ".join(x["word"] for x in chunk)))
                chunk, chunk_start = [w], w["start"]
            else:
                chunk.append(w)
        if chunk:
            entries.append((chunk_start, chunk[-1]["end"], " ".join(x["word"] for x in chunk)))

    lines = []
    for i, (start, end, text) in enumerate(entries, 1):
        lines += [str(i), f"{fmt(start)} --> {fmt(end)}", text, ""]
    return "\n".join(lines)
