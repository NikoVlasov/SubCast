import re
import subprocess
import time
import os
import tempfile
import queue as queue_module
import threading
from collections import deque

from openai import OpenAI
from groq import Groq, RateLimitError as GroqRateLimitError
from deep_translator import GoogleTranslator
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from pydub import AudioSegment
import streamlink as _sl_module
import yt_dlp as _ytdlp_module

load_dotenv()

app     = Flask(__name__)


BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ── Lazy API clients ──────────────────────────────────────────────────────────
_client      = None
_groq_client = None
_sl_session  = None




def _get_client():
    global _client
    if _client is None:
        _client = OpenAI()
    return _client

def _get_groq():
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _groq_client

def _get_sl():
    global _sl_session
    if _sl_session is None:
        _sl_session = _sl_module.Streamlink()
        for opt, val in [("player", ""), ("player-args", ""), ("player-no-close", False)]:
            try:
                _sl_session.options.set(opt, val)
            except Exception:
                pass
    return _sl_session

# ── Settings ──────────────────────────────────────────────────────────────────
RECORD_SECONDS        = 2
VOLUME_BOOST_DB       = 10
SILENCE_RMS_THRESHOLD = 50

FRAGMENT_WORD_LIMIT = 2    # words; blocks shorter than this get buffered
FRAGMENT_MAX_AGE    = 5.0  # seconds before a buffered fragment is flushed as-is

LANG_NAMES = {
    "EN": "English",  "RU": "Russian",   "ES": "Spanish",  "FR": "French",
    "DE": "German",   "PT": "Portuguese", "IT": "Italian",  "TR": "Turkish",
    "JA": "Japanese", "KO": "Korean",     "ZH": "Chinese",  "AR": "Arabic",
}

QUALITY_PROFILES = {
    "480p": ["480p30", "480p", "360p30", "360p", "audio_only"],
    "720p": ["720p60", "720p", "720p30", "best"],
    "auto": ["best", "audio_only", "720p60", "720p", "480p", "360p"],
}

# ── Global state ──────────────────────────────────────────────────────────────
# Simple boolean — both threads check this; no Events, no Locks needed.
is_translating = False

stream_url   = ""
dest_lang    = "RU"
quality_pref = "auto"

last_translation = {
    "recognized_text": "", "translation": "",
    "timestamp": 0.0, "quality": "premium", "message": "",
}

_groq_unavailable_until = 0.0
_context_history: deque = deque(maxlen=2)  # last 2 recognized phrases; deque is safe for 2 writers

# Pipeline: recorder → [audio_queue] → transcriber_loop × 2 (each thread: transcribe + translate)
# maxsize=1: transcription (~3.5-4s via Groq) is slower than capture (2s chunks), so any
# backlog means subtitles drift further behind over time. Only the newest chunk is kept —
# better to skip audio than show speech from 10-20s ago.
audio_queue = queue_module.Queue(maxsize=1)   # (audio_path, rec_elapsed, recorded_at)

# HLS URL cache — avoids a 2-5 s streamlink/yt-dlp lookup every block.
# Kept short: a stale cached URL means ffmpeg keeps reading from further and
# further behind the live edge as the broadcast continues, so subtitles drift.
_hls_cache     = {}
_HLS_CACHE_TTL = 25.0   # seconds

_ffmpeg_fail_count = 0   # consecutive ffmpeg failures; reset on success

# ── Regex ─────────────────────────────────────────────────────────────────────
# Matches 3+ consecutive repetitions of common filler/lyric syllables
_MUSIC_RE = re.compile(
    r'\b(oh|ah|uh|ooh|woo|la|na|da|mm|hmm|yeah|hey|whoa|baby)\b(\s+\1){2,}',
    re.IGNORECASE,
)
_CJK_RE   = re.compile(r'[一-鿿぀-ゟ゠-ヿ가-힯]')
_LATIN_CYRILLIC_LANGS = {"EN", "RU", "ES", "FR", "DE", "PT", "IT", "TR", "AR"}


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_platform(url):
    if "twitch.tv"  in url: return "twitch"
    if "youtube.com" in url or "youtu.be" in url: return "youtube"
    if "kick.com"   in url: return "kick"
    return "unknown"

def _cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

# ── Audio capture ─────────────────────────────────────────────────────────────
def _ffmpeg_record(source_url, raw_path):
    global _ffmpeg_fail_count
    try:
        subprocess.run(
            ["ffmpeg", "-y",
             "-fflags", "nobuffer",
             "-flags", "low_delay",
             "-strict", "experimental",
             "-live_start_index", "-1",   # newest segment, not a few behind live edge
             "-http_persistent", "0",     # fresh connection/request per block, no stale keep-alive reuse
             "-i", source_url,
             "-t", str(RECORD_SECONDS), "-ac", "1", "-ar", "16000", raw_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=_NO_WINDOW,
        )
        size = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
        print(f"    ffmpeg: {size} bytes")
        if size >= 1000:
            _ffmpeg_fail_count = 0
            return True
        _ffmpeg_fail_count += 1
        return False
    except Exception as e:
        print(f"    ffmpeg failed: {e}")
        _ffmpeg_fail_count += 1
        return False

def _sl_get_url(url, quality, force=False):
    key = f"{url}|{quality}"
    now = time.time()
    if not force:
        entry = _hls_cache.get(key)
        if entry and now < entry[1]:
            age = int(now - (entry[1] - _HLS_CACHE_TTL))
            print(f"    HLS cache hit ({age}s old): {quality!r}")
            return entry[0]
    try:
        streams = _get_sl().streams(url)
        stream  = streams.get(quality) or streams.get("best")
        if stream and hasattr(stream, "url"):
            hls = stream.url
            _hls_cache[key] = (hls, now + _HLS_CACHE_TTL)
            print(f"    HLS URL refreshed: {quality!r}")
            return hls
    except Exception as e:
        print(f"    streamlink {quality!r}: {e}")
    return ""

def _yt_get_url(url, force=False):
    now = time.time()
    if not force:
        entry = _hls_cache.get(url)
        if entry and now < entry[1]:
            age = int(now - (entry[1] - _HLS_CACHE_TTL))
            print(f"    HLS cache hit ({age}s old): youtube")
            return entry[0]
    try:
        for fmt in ["91", "bestaudio/best"]:
            with _ytdlp_module.YoutubeDL({"format": fmt, "quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(url, download=False)
                hls  = (info or {}).get("url", "")
            if hls:
                _hls_cache[url] = (hls, now + _HLS_CACHE_TTL)
                print(f"    HLS URL refreshed: youtube")
                return hls
    except Exception as e:
        print(f"    yt-dlp failed: {e}")
    return ""

def record_audio(url, raw_path, boosted_path):
    try:
        platform = get_platform(url)
        captured = False

        force_refresh = _ffmpeg_fail_count >= 3
        if force_refresh:
            print(f"  ⚠ {_ffmpeg_fail_count} consecutive ffmpeg failures — forcing URL refresh")

        for attempt in range(1, 4):
            if attempt > 1:
                print(f"  ↩ Retry {attempt}/3…")
                time.sleep(1)
            force = force_refresh or attempt > 1

            if platform == "youtube":
                hls = _yt_get_url(url, force=force)
                captured = bool(hls) and _ffmpeg_record(hls, raw_path)
            elif platform == "twitch":
                for q in ["audio_only", "best"]:
                    hls = _sl_get_url(url, q, force=force)
                    if hls and _ffmpeg_record(hls, raw_path):
                        captured = True
                        break
            else:
                for q in QUALITY_PROFILES.get(quality_pref, QUALITY_PROFILES["auto"]):
                    hls = _sl_get_url(url, q, force=force)
                    if hls and _ffmpeg_record(hls, raw_path):
                        captured = True
                        break

            if captured:
                break

        if not captured:
            return False

        audio = AudioSegment.from_file(raw_path)
        if len(audio) < 1000:
            print(f"  🔇 Too short ({len(audio)}ms), skipping")
            return False
        if audio.rms < SILENCE_RMS_THRESHOLD:
            print(f"  🔇 Silent (rms={audio.rms}), skipping")
            return False

        (audio + VOLUME_BOOST_DB).export(boosted_path, format="wav")
        return True

    except Exception as e:
        print(f"❌ Recording error: {e}")
        return False

# ── Translation ───────────────────────────────────────────────────────────────
def translate_text(text, dest="RU", src="EN", previous_context=None):
    target_lang = LANG_NAMES.get(dest.upper(), dest)
    source_lang = LANG_NAMES.get(src.upper(), src)
    try:
        system_prompt = (
            "You are a professional live subtitle translator "
            "for video streams (gaming, chatting, music, etc).\n\n"
            "Rules:\n"
            "- Translate naturally and conversationally, not literally\n"
            "- Match the speaker's tone (casual, excited, calm)\n"
            "- Preserve slang, jokes, and gaming terms appropriately\n"
            "- Keep cultural references understandable\n"
            "- Use context from previous lines for consistency\n"
            "- Output ONLY the translation, no explanations or quotes\n"
            "- If the text is incomplete (cut off mid-sentence), translate "
            "what's there naturally\n"
            "- If the input has obvious transcription errors (misheard words), "
            "infer the correct meaning from context and translate that"
        )
        ctx_block = (
            f"Previous context (last 2 lines):\n{previous_context}\n\n"
            if previous_context else ""
        )
        user_prompt = (
            f"{ctx_block}"
            f"Current line to translate from {source_lang} to {target_lang}:\n"
            f"{text}\n\n"
            f"Translate naturally:"
        )
        response = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=60,
            temperature=0.3,
            timeout=5.0,
        )
        translated = response.choices[0].message.content.strip()

        if len(translated) > 80:
            condensed = _get_client().chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        f"Condense to max 60 chars in {target_lang}, "
                        f"keep meaning natural and clear: {translated}"
                    ),
                }],
                max_tokens=40, temperature=0, timeout=10.0,
            )
            translated = condensed.choices[0].message.content.strip()

        return translated
    except Exception as e:
        print(f"❌ GPT translation error: {e}")
        return ""

_DEEP_LANG_MAP = {"ZH": "zh-CN"}

def _google_translate(text, dest):
    lang = _DEEP_LANG_MAP.get(dest.upper(), dest.lower())
    try:
        result = GoogleTranslator(source="auto", target=lang).translate(text)
        return result or text
    except Exception as e:
        print(f"  ⚠️ GoogleTranslator failed: {e}")
        return text

def is_garbage(text, dest=""):
    t = text.strip()
    if not t:
        return True

    # CJK in translated output going to a Latin/Cyrillic language → garbage
    if dest.upper() in _LATIN_CYRILLIC_LANGS and _CJK_RE.search(t):
        return True

    # CJK in transcribed source text (dest="") → hallucination on silent/noisy audio
    if not dest and _CJK_RE.search(t):
        return True

    lower = t.lower()
    for marker in (
        # Whisper metadata / credit hallucinations
        '[music]', '[applause]', '[laughter]', '[silence]',
        'subtitles by', 'subtitle by', 'subtitled by', 'transcribed by',
        'субтитры создавал', 'субтитры сделал', 'dimatorzok',
        'ご視聴ありがとう',
        # Former initial_prompt echo
        'common words:',
        # Generic spam
        'www.', '.com', '.org',
        'thanks for watching', 'thank you for watching',
        "don't forget to", 'like and subscribe',
        'спасибо за просмотр', 'подписывайтесь',
    ):
        if marker in lower:
            return True

    # Prompt-echo: ≥2 of our former rare prompt words appearing together
    # signals Whisper is regurgitating the prompt instead of real speech
    _PROMPT_ECHO = {'ribeye', 'gameplay', 'donate', 'viewers'}
    if sum(1 for w in _PROMPT_ECHO if w in lower) >= 2:
        return True

    if '♪' in t or '♫' in t:
        return True
    if _MUSIC_RE.search(t):
        return True
    # High word repetition → likely song lyrics (e.g. "yeah yeah yeah yeah")
    words = re.findall(r'\b\w+\b', lower)
    if len(words) >= 4 and len(set(words)) / len(words) < 0.40:
        return True
    if not re.search(r'\w', t, re.UNICODE):
        return True
    return False

_BASIC_MSG = "Базовый перевод. Для премиум качества оформите подписку"

# Common Whisper Russian transcription errors — only unambiguous replacements
_TRANSCRIPTION_FIXES = [
    (re.compile(r'\bБэтман\b'),  'Бэтмен'),
    (re.compile(r'\bБетмен\b'),  'Бэтмен'),
    (re.compile(r'\bСтрем\b',  re.IGNORECASE), 'Стрим'),
]

def _fix_transcription(text: str) -> str:
    for pattern, replacement in _TRANSCRIPTION_FIXES:
        text = pattern.sub(replacement, text)
    return text

# ── Worker threads ────────────────────────────────────────────────────────────
def recorder_loop(url):
    print("▶️  Recorder started")
    while is_translating:
        fd_raw, raw_path     = tempfile.mkstemp(suffix="_raw.wav")
        fd_b,   boosted_path = tempfile.mkstemp(suffix="_boosted.wav")
        os.close(fd_raw)
        os.close(fd_b)

        t0 = time.time()
        if record_audio(url, raw_path, boosted_path):
            rec_elapsed = time.time() - t0
            _cleanup(raw_path)
            chunk = (boosted_path, rec_elapsed, time.time())
            try:
                audio_queue.put_nowait(chunk)
            except queue_module.Full:
                # Transcriber hasn't kept up — drop the queued (stale) chunk and
                # keep only the freshest one, so we never build a backlog.
                try:
                    stale_path, _, _ = audio_queue.get_nowait()
                    _cleanup(stale_path)
                    print("  ⏭ Dropping stale audio chunk (queue full)")
                except queue_module.Empty:
                    pass
                try:
                    audio_queue.put_nowait(chunk)
                except queue_module.Full:
                    _cleanup(boosted_path)
        else:
            _cleanup(raw_path, boosted_path)

    print("⏹  Recorder stopped")


def _translate_and_save(text: str, groq_ok: bool, tid: int) -> None:
    """Translate one recognized line and write result to last_translation. Called from transcriber threads."""
    global last_translation, _context_history
    ctx        = " | ".join(_context_history) if _context_history else None
    translated = ""
    quality    = "basic"
    message    = ""

    # Always try GPT-4o-mini first regardless of which transcriber was used.
    # Google is only a last resort when the OpenAI call itself throws.
    try:
        translated = translate_text(text, dest=dest_lang, src="EN", previous_context=ctx)
        if translated and not is_garbage(translated, dest=dest_lang):
            quality = "premium"
            src_tag = "Groq" if groq_ok else "Whisper-1"
            print(f"  📘 [T{tid}] Premium ({src_tag}→GPT): {translated}")
        else:
            translated = ""   # garbage result — fall through to Google
    except Exception as e:
        print(f"  ❌ [T{tid}] GPT failed: {e}")
        translated = ""

    if not translated:
        translated = _google_translate(text, dest_lang)
        message    = _BASIC_MSG if translated else ""
        if translated:
            print(f"  📗 [T{tid}] Basic (Google fallback): {translated}")

    if translated and not is_garbage(translated, dest=dest_lang):
        _context_history.append(text)   # deque(maxlen=2) auto-discards oldest
        last_translation = {            # simple assignment — atomic under GIL
            "recognized_text": text,
            "translation":     translated,
            "timestamp":       time.time(),
            "quality":         quality,
            "message":         message,
        }
        print(f"  💾 [T{tid}] Saved: {translated!r}")


def transcriber_loop(tid: int):
    """Full pipeline per thread: audio → transcription → translation → last_translation."""
    global _groq_unavailable_until
    print(f"▶️  Transcriber T{tid} started")

    buf_text    = ""   # short-fragment hold buffer — local to this thread
    buf_time    = 0.0
    buf_groq_ok = False

    def _flush_buf():
        nonlocal buf_text, buf_time, buf_groq_ok
        if buf_text:
            print(f"  ⏱ [T{tid}] Flushing buffered fragment: {buf_text!r}")
            _translate_and_save(buf_text, buf_groq_ok, tid)
            buf_text = ""

    while is_translating:
        try:
            audio_path, rec_time, recorded_at = audio_queue.get(timeout=1)
        except queue_module.Empty:
            if buf_text and time.time() - buf_time > FRAGMENT_MAX_AGE:
                _flush_buf()
            continue

        chunk_age = time.time() - recorded_at
        if chunk_age > 3.0:
            print(f"  ⏭ [T{tid}] Skipping stale audio chunk ({chunk_age:.1f}s old)")
            _cleanup(audio_path)
            continue

        text           = None
        groq_ok        = False
        no_speech_skip = False
        tr_start       = time.time()

        if time.time() < _groq_unavailable_until:
            secs = int(_groq_unavailable_until - time.time())
            print(f"  ⏭ [T{tid}] Groq cooldown ({secs}s left)")
        else:
            try:
                print(f"  ⏳ [T{tid}] Groq transcription…")
                with open(audio_path, "rb") as f:
                    transcript = _get_groq().audio.transcriptions.create(
                        model="whisper-large-v3", file=f,
                        response_format="verbose_json",
                        temperature=0,
                        timeout=15.0,
                    )
                no_speech_prob = max(
                    (getattr(s, 'no_speech_prob', 0.0)
                     for s in (getattr(transcript, 'segments', None) or [])),
                    default=0.0,
                )
                if no_speech_prob > 0.5:
                    print(f"  ✗ [T{tid}] No speech detected (prob={no_speech_prob:.2f})")
                    no_speech_skip = True
                else:
                    text    = transcript.text.strip()
                    groq_ok = True
                    print(f"  ✅ Groq [T{tid}]: {text[:60]!r}")
            except GroqRateLimitError as e:
                _groq_unavailable_until = time.time() + 60
                print(f"  ⚡ [T{tid}] Groq 429 — paused 60s: {e}")
            except Exception as e:
                print(f"  ❌ [T{tid}] Groq failed: {type(e).__name__}: {e}")

        if no_speech_skip:
            _cleanup(audio_path)
            continue

        if text is None:
            try:
                print(f"  ⏳ [T{tid}] OpenAI Whisper fallback…")
                with open(audio_path, "rb") as f:
                    transcript = _get_client().audio.transcriptions.create(
                        model="whisper-1", file=f,
                        temperature=0,
                        timeout=8.0,
                    )
                text = transcript.text.strip()
                print(f"  ✅ [T{tid}] OpenAI Whisper: {text[:60]!r}")
            except Exception as e:
                kind = "timeout" if "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower() else "error"
                print(f"  ⏭ [T{tid}] OpenAI Whisper {kind} — skipping block: {type(e).__name__}")
                _cleanup(audio_path)
                continue

        tr_time = time.time() - tr_start
        _cleanup(audio_path)

        text = _fix_transcription(text)

        if is_garbage(text):
            print(f"  ✗ [T{tid}] Filtered: {text!r}")
            continue

        if text == last_translation.get("recognized_text", ""):
            print(f"  ✗ [T{tid}] Duplicate: {text!r}")
            continue

        now = time.time()

        if buf_text:
            if now - buf_time <= FRAGMENT_MAX_AGE:
                text = buf_text + " " + text
                print(f"  🔗 [T{tid}] Merged fragment → {text!r}")
            else:
                _flush_buf()
            buf_text = ""

        if len(text.split()) <= FRAGMENT_WORD_LIMIT:
            print(f"  ⏳ [T{tid}] Buffering short fragment: {text!r}")
            buf_text    = text
            buf_time    = now
            buf_groq_ok = groq_ok
            continue

        print(f"🗣 [T{tid}] Recognized: {text}")
        gpt_start = time.time()
        _translate_and_save(text, groq_ok, tid)
        gpt_time = time.time() - gpt_start
        print(f"  ⏱ [T{tid}] Record: {rec_time:.1f}s, Transcribe: {tr_time:.1f}s, Translate: {gpt_time:.1f}s")

    # Flush any remaining fragment when stopping
    _flush_buf()
    print(f"⏹  Transcriber T{tid} stopped")

# ── Flask routes ──────────────────────────────────────────────────────────────
def _quality_rank(q):
    order = ['audio_only', '160p', '360p', '480p', '480p30',
             '720p', '720p30', '720p60', '1080p', '1080p60', 'best']
    try:    return order.index(q)
    except: return -1

@app.route('/')
def index():
    for name in ('index.html', 'mobile.html'):
        if os.path.exists(os.path.join(BASE_DIR, name)):
            return send_from_directory(BASE_DIR, name)
    return "SubCast running", 200

@app.route('/mobile')
def mobile():
    return send_from_directory(BASE_DIR, 'mobile.html')

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

@app.route('/stream_url', methods=['POST'])
def get_stream_url():
    data    = request.get_json(force=True) or {}
    surl    = data.get('stream_url', '').strip()
    quality = data.get('quality', 'auto').strip()

    if not surl:
        return jsonify({'error': 'stream_url required', 'url': '', 'qualities': []}), 400

    platform    = get_platform(surl)
    hls_url     = ''
    qualities   = []
    quality_map = {'480p': '480p30', '720p': '720p60', 'auto': 'best'}

    try:
        if platform == 'youtube':
            hls_url   = _yt_get_url(surl)
            qualities = ['1080p', '720p', '480p', '360p']
        else:
            streams   = _get_sl().streams(surl)
            qualities = sorted(streams.keys(), key=_quality_rank, reverse=True)
            target    = quality_map.get(quality, 'best')
            for tier in [target, 'best']:
                stream = streams.get(tier)
                if stream and hasattr(stream, 'url'):
                    hls_url = stream.url
                    _hls_cache[f"{surl}|{tier}"] = (hls_url, time.time() + _HLS_CACHE_TTL)
                    break
    except Exception as e:
        print(f'  /stream_url error: {e}')
        return jsonify({'error': str(e), 'url': '', 'qualities': qualities}), 500

    print(f'🎬 /stream_url quality={quality}  {hls_url[:60]}…')
    return jsonify({'url': hls_url, 'qualities': qualities}), 200

@app.route('/start', methods=['POST'])
def start_translation():
    global is_translating, stream_url, dest_lang, quality_pref, last_translation, _context_history

    data    = request.get_json() or {}
    new_url = data.get("stream_url", "").strip()
    if not new_url:
        return jsonify({"error": "stream_url is required"}), 400

    dest_lang    = data.get("dest_lang", "RU").upper()
    q            = data.get("quality", "auto").lower()
    quality_pref = q if q in QUALITY_PROFILES else "auto"

    # Signal old threads to stop and wait for them to finish naturally
    is_translating = False
    time.sleep(3)

    # Drain audio queue (holds (path, rec_time, recorded_at) tuples)
    while not audio_queue.empty():
        try:
            path, _, _ = audio_queue.get_nowait()
            _cleanup(path)
        except queue_module.Empty:
            break

    # Clear HLS cache on stream change
    if new_url != stream_url:
        _hls_cache.clear()

    stream_url       = new_url
    _context_history = deque(maxlen=2)
    last_translation = {
        "recognized_text": "🎙️ Listening...",
        "translation":     "🎙️ Listening...",
        "timestamp":       time.time(),
        "quality":         "premium",
        "message":         "",
    }

    is_translating = True
    threading.Thread(target=recorder_loop,    args=(stream_url,), daemon=True).start()
    threading.Thread(target=transcriber_loop, args=(1,),          daemon=True).start()
    # threading.Thread(target=transcriber_loop, args=(2,),          daemon=True).start()  # disabled: Groq daily limit hit

    print(f"▶️  Started: {stream_url}  dest={dest_lang}  quality={quality_pref}")
    return jsonify({"status": "started", "dest_lang": dest_lang}), 200

@app.route('/stop', methods=['POST'])
def stop_translation():
    global is_translating
    is_translating = False
    return jsonify({"status": "stopped"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
