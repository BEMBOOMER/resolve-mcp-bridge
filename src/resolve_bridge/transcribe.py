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


def _split_balanced(words, max_chars):
    """Split words into chunks of <= max_chars with roughly equal length, so the
    last chunk never ends up as a one-word orphan card."""
    import math
    total = len(" ".join(w["word"] for w in words))
    n = max(1, math.ceil(total / max_chars))
    target = total / n
    chunks, chunk, cur_len = [], [], 0
    for w in words:
        add = len(w["word"]) + (1 if chunk else 0)
        if chunk and (cur_len + add > max_chars or (cur_len >= target and len(chunks) < n - 1)):
            chunks.append(chunk)
            chunk, cur_len = [], 0
            add = len(w["word"])
        chunk.append(w)
        cur_len += add
    if chunk:
        chunks.append(chunk)
    return chunks


def remap_segments(segments, windows):
    """Remap source-time transcript segments into timeline time after a re-cut.

    windows: [(in_sec, out_sec, timeline_offset_sec)] — the kept pieces of this
    source clip in timeline order. Words outside every window are dropped;
    segments spanning a cut are split per window. Word times shift with them.
    A small tolerance keeps words whose whisper timestamp clumps just outside a
    cut point (common at segment starts) without resurrecting removed middles.
    """
    TOL = 0.35
    out = []
    for seg in segments:
        words = seg.get("words") or []
        for (w_in, w_out, off) in windows:
            if words:
                kept = [w for w in words
                        if w_in - TOL <= (w["start"] + w["end"]) / 2 < w_out + TOL]
                if not kept:
                    continue
                shifted = [{"word": w["word"],
                            "start": off + min(max(w["start"] - w_in, 0.0), w_out - w_in),
                            "end": off + min(max(w["end"] - w_in, 0.0), w_out - w_in)} for w in kept]
                out.append({"start": shifted[0]["start"], "end": shifted[-1]["end"],
                            "text": " ".join(w["word"] for w in kept), "words": shifted})
            else:
                s, e = max(seg["start"], w_in), min(seg["end"], w_out)
                if e - s > 0.05:
                    out.append({"start": off + (s - w_in), "end": off + (e - w_in),
                                "text": seg["text"], "words": []})
    out.sort(key=lambda s: s["start"])
    return out


def to_srt(segments, max_chars=42):
    """Transcript segments -> SRT text. Long segments are split on word timings
    into balanced chunks (no orphan one-word cards)."""
    def fmt(t):
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"

    entries = []
    for seg in segments:
        words = seg.get("words") or []
        if len(seg["text"]) <= max_chars or len(words) < 2:
            entries.append((seg["start"], seg["end"], seg["text"]))
            continue
        for chunk in _split_balanced(words, max_chars):
            entries.append((chunk[0]["start"], chunk[-1]["end"], " ".join(x["word"] for x in chunk)))

    # readability pass: stretch too-short cards (< 1s) toward the next card
    MIN_DUR, GAP = 1.0, 0.04
    stretched = []
    for i, (start, end, text) in enumerate(entries):
        if end - start < MIN_DUR:
            limit = entries[i + 1][0] - GAP if i + 1 < len(entries) else start + MIN_DUR
            end = max(end, min(start + MIN_DUR, limit))
        stretched.append((start, end, text))

    lines = []
    for i, (start, end, text) in enumerate(stretched, 1):
        lines += [str(i), f"{fmt(start)} --> {fmt(end)}", text, ""]
    return "\n".join(lines)
