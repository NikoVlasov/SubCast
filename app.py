import re
import subprocess
import time
import os
import tempfile
import queue as queue_module
import threading
import json
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
from groq import Groq, RateLimitError as GroqRateLimitError
from deep_translator import GoogleTranslator
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from pydub import AudioSegment

load_dotenv()

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
client      = OpenAI()                              # GPT-4o-mini for translation
groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))  # whisper-large-v3 for transcription

# === SETTINGS ===
record_seconds        = 3    # 5 → 3 for lower subtitle latency
VOLUME_BOOST_DB       = 10
SILENCE_RMS_THRESHOLD = 50   # skip audio below this RMS; saves API calls + kills hallucinations

# Full language names sent in the GPT translation prompt
LANG_NAMES = {
    "EN": "English",  "RU": "Russian",   "ES": "Spanish",  "FR": "French",
    "DE": "German",   "PT": "Portuguese", "IT": "Italian",  "TR": "Turkish",
    "JA": "Japanese", "KO": "Korean",     "ZH": "Chinese",  "AR": "Arabic",
}

# Streamlink quality tiers (tried left-to-right until one succeeds)
QUALITY_PROFILES = {
    "480p": ["480p30", "480p", "360p30", "360p", "audio_only"],
    "720p": ["720p60", "720p", "720p30", "best"],
    "auto": ["best", "audio_only", "720p60", "720p", "480p", "360p"],
}

# Queue holds paths to boosted WAV files ready for transcription.
# maxsize=2 so the recorder never runs more than one block ahead.
audio_queue: queue_module.Queue = queue_module.Queue(maxsize=2)

# Per-session stop event — replaced on each /start so old threads exit cleanly.
_stop_event = threading.Event()
_stop_event.set()  # initially stopped; no threads running

# State
_block_lock            = threading.Lock()
_blocks_processed      = 0
_groq_unavailable_until = 0.0   # epoch; Groq skipped while time.time() < this
is_translating         = False
last_translation = {"recognized_text": "", "translation": "", "timestamp": 0.0, "quality": "premium", "message": ""}
stream_url       = ""
dest_lang        = "RU"
quality_pref     = "auto"

# Pre-compiled pattern for repeating-syllable music ("la la la", "na na na", …)
_MUSIC_RE = re.compile(r'\b(la|na|mm|da)\b(\s+\1){2,}', re.IGNORECASE)


# ── helpers ───────────────────────────────────────────────────────────────────

def get_platform(url: str) -> str:
    if "twitch.tv" in url:
        return "twitch"
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "kick.com" in url:
        return "kick"
    return "unknown"


def _cleanup(*paths: str) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


# ── audio capture ─────────────────────────────────────────────────────────────

def _streamlink_capture(url: str, quality: str, raw_path: str) -> bool:
    try:
        sl = subprocess.Popen(
            ["streamlink", url, quality, "--stdout"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        ff = subprocess.Popen(
            ["ffmpeg", "-y", "-i", "pipe:0", "-t", str(record_seconds),
             "-ac", "1", "-ar", "16000", raw_path],
            stdin=sl.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ff.communicate()
        sl.terminate()
        size = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
        print(f"    streamlink quality={quality!r}  size={size} bytes")
        return size >= 1000
    except Exception as e:
        print(f"    streamlink {quality!r} failed: {e}")
        return False


def _ytdlp_pipe_capture(url: str, raw_path: str) -> bool:
    try:
        print("  ▶️  yt-dlp pipe capture…")
        yt = subprocess.Popen(
            ["yt-dlp", "-f", "91", "--no-playlist", "-o", "-", url],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        ff = subprocess.Popen(
            ["ffmpeg", "-y", "-i", "pipe:0", "-t", str(record_seconds),
             "-ac", "1", "-ar", "16000", raw_path],
            stdin=yt.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ff.communicate()
        yt.terminate()
        size = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
        print(f"    yt-dlp size={size} bytes")
        return size >= 1000
    except Exception as e:
        print(f"    yt-dlp failed: {e}")
        return False


def record_audio(url: str, raw_path: str, boosted_path: str) -> bool:
    try:
        print(f"🎙️ Recording from {url}")
        platform = get_platform(url)

        captured = False
        for attempt in range(1, 4):
            if attempt > 1:
                print(f"  ↩ Retry {attempt}/3…")
                time.sleep(1)

            if platform == "youtube":
                captured = _ytdlp_pipe_capture(url, raw_path)
            elif platform == "twitch":
                captured = any(_streamlink_capture(url, q, raw_path)
                               for q in ["audio_only", "best"])
            else:
                qualities = QUALITY_PROFILES.get(quality_pref, QUALITY_PROFILES["auto"])
                captured = any(_streamlink_capture(url, q, raw_path) for q in qualities)

            if captured:
                break

        if not captured:
            print("⚠️ Audio too short or missing after 3 attempts")
            return False

        audio = AudioSegment.from_file(raw_path)

        # Volume gate — skip silent segments to save API calls and reduce hallucinations
        if audio.rms < SILENCE_RMS_THRESHOLD:
            print(f"  🔇 Silent (rms={audio.rms} < {SILENCE_RMS_THRESHOLD}), skipping")
            return False

        (audio + VOLUME_BOOST_DB).export(boosted_path, format="wav")
        return True

    except Exception as e:
        print(f"❌ Recording error: {e}")
        return False


# ── translation ───────────────────────────────────────────────────────────────

def translate_text(text: str, dest: str = "RU") -> str:
    lang = LANG_NAMES.get(dest.upper(), dest)
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f"Translate the following text to {lang}. "
                    f"Return only the translation, nothing else:\n{text}"
                ),
            }],
            max_tokens=500,
            temperature=0,
            timeout=10.0,
        )
        translated = response.choices[0].message.content.strip()

        # Summarize if too long to display comfortably on screen
        if len(translated) > 150:
            summary = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        f"Summarize this in max 100 characters in {lang}, "
                        f"keeping the key meaning: {translated}"
                    ),
                }],
                max_tokens=80,
                temperature=0,
                timeout=10.0,
            )
            translated = summary.choices[0].message.content.strip()

        return translated
    except Exception as e:
        print(f"❌ Translation error: {e}")
        return ""


# GoogleTranslator uses lowercase ISO codes; Chinese needs an explicit region tag
_DEEP_LANG_MAP = {"ZH": "zh-CN"}

def _basic_translate(text: str, dest: str) -> str:
    """Translate with GoogleTranslator (free, no rate limits)."""
    lang = _DEEP_LANG_MAP.get(dest.upper(), dest.lower())
    try:
        result = GoogleTranslator(source="auto", target=lang).translate(text)
        return result or text
    except Exception as e:
        print(f"  ⚠️ GoogleTranslator failed: {e}")
        return text


def is_garbage(text: str) -> bool:
    """Return True if the transcription looks like silence, noise, or hallucination."""
    t = text.strip()
    if not t:   # completely empty
        return True

    lower = t.lower()
    for marker in (
        # Whisper noise/silence markers
        '[music]', '[applause]', '[laughter]', '[silence]',
        # Attribution hallucinations
        'subtitles by', 'subtitle by', 'transcribed by', 'www.', '.com', '.org',
        # English engagement-bait phrases Whisper hallucinates at stream end
        'thanks for watching', 'thank you for watching',
        "don't forget to", 'like and subscribe',
        # Russian equivalents
        'спасибо за просмотр', 'подписывайтесь',
    ):
        if marker in lower:
            return True

    # Musical note symbols
    if '♪' in t or '♫' in t:
        return True

    # Repeating-syllable music: "la la la", "na na na", "mm mm mm", "da da da"
    if _MUSIC_RE.search(t):
        return True

    # No word characters at all (pure symbols / punctuation)
    if not re.search(r'\w', t, re.UNICODE):
        return True

    return False


_BASIC_MSG = "Базовый перевод. Для премиум качества оформите подписку"

def transcribe_and_translate(audio_path: str) -> tuple:
    """Returns (recognized_text, translated_text, quality, message).

    Every step is isolated in its own try/except so a failure in one step
    never silently kills the whole pipeline.

    Premium path : Groq whisper-large-v3  +  GPT-4o-mini   (10 s timeout each)
    Basic fallback: OpenAI whisper-1       +  GoogleTranslator (free)
    """

    # ── Step 1: Groq Whisper (15 s hard timeout; >10 s → basic mode) ─────────
    global _groq_unavailable_until
    groq_ok = False
    text     = None

    now = time.time()
    if now < _groq_unavailable_until:
        remaining = int(_groq_unavailable_until - now)
        print(f"  ⏭ Groq on cooldown ({remaining}s left) — using basic mode")
    else:
        t0 = time.monotonic()
        try:
            print("  ⏳ Groq transcription…")
            with open(audio_path, "rb") as f:
                transcript = groq_client.audio.transcriptions.create(
                    model="whisper-large-v3",
                    file=f,
                    timeout=15.0,
                )
            groq_elapsed = time.monotonic() - t0
            text = transcript.text.strip()
            if groq_elapsed > 10.0:
                print(f"  ⚠️ Groq slow ({groq_elapsed:.1f}s), switching to basic mode for this block")
                # text is captured; skip GPT and use GoogleTranslator below
            else:
                groq_ok = True
                print(f"  ✅ Groq OK ({groq_elapsed:.1f}s): {text[:60]!r}")
        except GroqRateLimitError as e:
            _groq_unavailable_until = time.time() + 3600
            resume = time.strftime("%H:%M", time.localtime(_groq_unavailable_until))
            print(f"  ⚡ Groq 429 — disabling Groq for 60 min (resumes ~{resume}): {e}")
        except Exception as e:
            print(f"  ❌ Groq failed ({time.monotonic()-t0:.1f}s): {type(e).__name__}: {e}")

    # ── Step 2: OpenAI Whisper fallback if Groq failed ─────────────────────────
    if text is None:
        print("  🔄 Switched to basic mode (Groq unavailable) — trying OpenAI Whisper…")
        try:
            with open(audio_path, "rb") as f:
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    timeout=10.0,
                )
            text = transcript.text.strip()
            print(f"  ✅ OpenAI Whisper OK: {text[:60]!r}")
        except Exception as e:
            print(f"  ❌ OpenAI Whisper fallback failed: {type(e).__name__}: {e}")
            return "", "", "premium", ""

    # ── Step 3: Validate transcription ─────────────────────────────────────────
    if is_garbage(text):
        print(f"  ✗ Filtered: {text!r}")
        return "", "", ("premium" if groq_ok else "basic"), ""

    if text == last_translation.get("recognized_text", ""):
        print(f"  ✗ Duplicate: {text!r}")
        return "", "", ("premium" if groq_ok else "basic"), ""

    print(f"🗣 Recognized: {text}")

    # ── Step 4: GPT-4o-mini translation (premium path only) ───────────────────
    if groq_ok:
        try:
            print("  ⏳ GPT-4o-mini translation…")
            translated = translate_text(text, dest=dest_lang)
            if translated:
                print(f"  📘 Translation (premium): {translated}")
                return text, translated, "premium", ""
            print("  ⚠️ GPT returned empty string — falling back to GoogleTranslator")
        except Exception as e:
            print(f"  ❌ GPT translation failed: {type(e).__name__}: {e}")

    # ── Step 5: GoogleTranslator (free fallback) ────────────────────────────────
    print("  ⏳ GoogleTranslator…")
    try:
        translated = _basic_translate(text, dest_lang)
        print(f"  📗 Translation (basic): {translated}")
        return text, translated, "basic", _BASIC_MSG
    except Exception as e:
        print(f"  ❌ GoogleTranslator failed: {type(e).__name__}: {e}")
        # Last resort: return raw transcription so something is shown
        return text, text, "basic", _BASIC_MSG


# ── worker threads ────────────────────────────────────────────────────────────

def recorder_loop(url: str, stop: threading.Event) -> None:
    """Continuously records audio and pushes non-silent blocks onto the queue."""
    while not stop.is_set():
        fd_raw, raw_path = tempfile.mkstemp(suffix="_raw.wav")
        os.close(fd_raw)
        fd_b, boosted_path = tempfile.mkstemp(suffix="_boosted.wav")
        os.close(fd_b)

        if record_audio(url, raw_path, boosted_path):
            _cleanup(raw_path)
            try:
                audio_queue.put(boosted_path, timeout=record_seconds)
            except queue_module.Full:
                _cleanup(boosted_path)
        else:
            _cleanup(raw_path, boosted_path)


def _process_block(audio_path: str, block_time: float) -> None:
    """Worker: transcribe+translate one block, then update global state."""
    global last_translation, _blocks_processed

    recognized, translated, quality, message = transcribe_and_translate(audio_path)
    _cleanup(audio_path)

    with _block_lock:
        _blocks_processed += 1
        count = _blocks_processed
    if count % 10 == 0:
        print(f"💓 Transcriber alive, processed {count} blocks")

    # Guard against out-of-order completions: only update if this block is newer
    if recognized and block_time > last_translation.get("timestamp", 0):
        last_translation = {
            "recognized_text": recognized,
            "translation":     translated,
            "timestamp":       block_time,   # capture time, not completion time
            "quality":         quality,
            "message":         message,
        }


def transcriber_loop(stop: threading.Event) -> None:
    """Submits audio blocks to a 2-worker pool so Groq calls run in parallel."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        while not stop.is_set() or not audio_queue.empty():
            try:
                audio_path = audio_queue.get(timeout=1)
            except queue_module.Empty:
                continue

            block_time = time.time()
            pool.submit(_process_block, audio_path, block_time)
            audio_queue.task_done()
        # Exiting the with-block waits for any in-flight workers to finish


# ── Flask routes ──────────────────────────────────────────────────────────────

def _quality_rank(q: str) -> int:
    order = ['audio_only', '160p', '360p', '480p', '480p30', '720p', '720p30',
             '720p60', '1080p', '1080p60', 'best']
    try:
        return order.index(q)
    except ValueError:
        return -1


@app.route('/stream_url', methods=['POST'])
def get_stream_url():
    data    = request.get_json(force=True) or {}
    surl    = data.get('stream_url', '').strip()
    quality = data.get('quality', 'auto').strip()

    if not surl:
        return jsonify({'error': 'stream_url required', 'url': '', 'qualities': []}), 400

    platform    = get_platform(surl)
    quality_map = {'480p': '480p', '720p': '720p60', 'auto': 'best', 'best': 'best'}
    sl_quality  = quality_map.get(quality, 'best')
    qualities: list = []
    hls_url: str    = ''

    try:
        if platform == 'youtube':
            result = subprocess.run(
                ['yt-dlp', '-f', '91', '-g', '--no-playlist', surl],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                hls_url   = result.stdout.strip().splitlines()[0]
                qualities = ['1080p', '720p', '480p', '360p']
            else:
                print(f'⚠️  yt-dlp failed: {result.stderr[:200]}')
        else:
            try:
                info = subprocess.run(
                    ['streamlink', '--json', surl],
                    capture_output=True, text=True, timeout=20
                )
                if info.returncode == 0:
                    streams   = json.loads(info.stdout).get('streams', {})
                    qualities = sorted(streams.keys(), key=_quality_rank, reverse=True)
            except Exception as exc:
                print(f'⚠️  streamlink --json failed: {exc}')

            for tier in [sl_quality, 'best']:
                r = subprocess.run(
                    ['streamlink', '--stream-url', surl, tier],
                    capture_output=True, text=True, timeout=30
                )
                if r.returncode == 0 and r.stdout.strip():
                    hls_url = r.stdout.strip()
                    break

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'timeout', 'url': '', 'qualities': qualities}), 504
    except Exception as exc:
        return jsonify({'error': str(exc), 'url': '', 'qualities': qualities}), 500

    preview = (hls_url[:80] + '…') if len(hls_url) > 80 else hls_url
    print(f'🎬 /stream_url  quality={quality}/{sl_quality}  url={preview or "(none)"}')
    return jsonify({'url': hls_url, 'qualities': qualities}), 200


@app.route('/start', methods=['POST'])
def start_translation():
    global is_translating, stream_url, dest_lang, quality_pref, last_translation, _stop_event

    data    = request.get_json()
    new_url = data.get("stream_url", "").strip()
    if not new_url:
        return jsonify({"error": "stream_url is required"}), 400

    dest_lang    = data.get("dest_lang", "RU").upper()
    q            = data.get("quality", "auto").lower()
    quality_pref = q if q in QUALITY_PROFILES else "auto"

    # Stop any running session so old threads exit and don't pollute the new one
    if is_translating:
        print(f"🔄 Restarting session → {new_url}")
        _stop_event.set()
        is_translating = False

    # Clear subtitles immediately — client sees blank on streamer switch
    stream_url       = new_url
    last_translation = {"recognized_text": "", "translation": "", "timestamp": time.time(), "quality": "premium", "message": ""}

    # Drain and delete any stale audio files from the previous session
    while not audio_queue.empty():
        try:
            _cleanup(audio_queue.get_nowait())
        except queue_module.Empty:
            break

    # Fresh stop event for the new session; threads run while it is not set
    _stop_event    = threading.Event()
    is_translating = True

    threading.Thread(target=recorder_loop,    args=(stream_url, _stop_event), daemon=True).start()
    threading.Thread(target=transcriber_loop, args=(_stop_event,),            daemon=True).start()

    print(f"▶️  Started: {stream_url}  dest={dest_lang}  quality={quality_pref}")
    return jsonify({"status": "started", "dest_lang": dest_lang}), 200


@app.route('/stop', methods=['POST'])
def stop_translation():
    global is_translating
    _stop_event.set()
    is_translating = False
    return jsonify({"status": "stopped"}), 200


@app.route('/latest', methods=['GET'])
def get_latest():
    return jsonify(last_translation), 200


@app.route('/status', methods=['GET'])
def get_status():
    return jsonify({
        "is_translating": is_translating,
        "stream_url":     stream_url,
        "dest_lang":      dest_lang,
        "platform":       get_platform(stream_url),
        "quality":        quality_pref,
    }), 200


@app.route('/mobile')
def mobile():
    return send_from_directory(BASE_DIR, 'mobile.html')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
