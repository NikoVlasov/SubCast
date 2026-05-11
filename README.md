# SubCast 🎙️

**Real-time subtitle translation for live streams and podcasts**

SubCast brings live AI-powered subtitles to any stream or podcast — in your language, on any screen. Watch Twitch, YouTube Live, and Kick streams with real-time translated subtitles, even if you don't speak the streamer's language.

---

## ✨ Features

- **Real-time subtitles** — AI-powered speech recognition and translation with minimal delay
- **Auto language detection** — no need to select the source language; SubCast detects it automatically
- **Your language** — choose your subtitle language from 12+ supported languages
- **Three platforms** — Twitch, YouTube Live, and Kick
- **Android TV app** — full-screen stream with subtitles and Twitch chat side by side
- **Twitch account integration** — log in to see your followed channels and use chat
- **Desktop widget** — floating subtitle overlay for PC
- **Mobile companion** — view subtitles on your phone while watching on TV
- **Chat toggle** — hide or show Twitch chat with a single button
- **Quality selector** — switch between 480p, 720p, and Auto quality with your TV remote

---

## 📱 Device Support

| Device | Method | Experience |
|--------|--------|-----------|
| Android TV | Install APK | ⭐ Best — full screen stream + subtitles + chat |
| Android Phone/Tablet | Install APK | Great — stream + subtitles |
| iPhone / iPad | Browser → `server-ip:5000/mobile` | Good — subtitles companion |
| Windows PC | Desktop widget (PyQt6) | Great — floating subtitle overlay |
| Any browser | `server-ip:5000/mobile` | Good — subtitles on any device |

### Quick Start by Device

**Android TV / Android Phone:**
Download `SubCast.apk` from [Releases](https://github.com/NikoVlasov/SubCast/releases) and sideload it.

**iPhone / iPad:**
Make sure SubCast server is running on your PC.
Open Safari and go to `http://your-server-ip:5000/mobile`

**Windows Desktop:**
Run `run_server.bat` then launch `widget.py` for a floating subtitle overlay.

---

## 🚀 Getting Started

### Requirements

- Windows PC (runs the translation server)
- Android TV (for the best experience)
- Python 3.10+
- Groq API key (free) — for speech recognition
- OpenAI API key (optional) — for higher quality translation

### Installation

**1. Clone the repository**
```bash
git clone https://github.com/NikoVlasov/SubCast.git
cd subcast
```

**2. Install dependencies**
```bash
pip install -r requirements.txt
```

**3. Add your API keys to `.env`**
```
GROQ_API_KEY=your_groq_key_here
OPENAI_API_KEY=your_openai_key_here
```

**4. Start the server**

Double-click `run_server.bat` or run:
```bash
python app.py
```

**5. Install the Android TV app**

Download `SubCast.apk` from [Releases](https://github.com/NikoVlasov/SubCast/releases) and sideload it onto your Android TV.

---

## 🌍 Supported Languages

English, Russian, Spanish, French, German, Portuguese, Italian, Turkish, Japanese, Korean, Chinese, Arabic

---

## 🏗️ Architecture

```
Android TV App (SubCast.apk)
        ↓ WiFi
Flask Server (your PC)
        ↓
Groq Whisper API → speech recognition (free)
        ↓
GPT-4o-mini → natural translation
        ↓
Subtitles appear on screen
```

---

## 💡 How It Works

1. The Android TV app connects to the Flask server running on your PC
2. The server captures audio from the live stream using streamlink or yt-dlp
3. Audio is sent to Groq Whisper for speech recognition (language auto-detected)
4. Recognized text is translated using GPT-4o-mini
5. Subtitles appear on your TV in real time

---

## 🔄 Translation Quality

SubCast has two modes:

- **Premium** ⭐ — Groq Whisper + GPT-4o-mini (high quality, requires API keys)
- **Basic** 🔄 — fallback mode when API limits are reached (free, lower quality)

---

## 🛠️ Tech Stack

- **Backend**: Python, Flask, streamlink, yt-dlp, ffmpeg
- **AI**: Groq Whisper large-v3, OpenAI GPT-4o-mini
- **Android TV**: Kotlin, ExoPlayer (media3), WebView
- **Desktop widget**: PyQt6

---

## 📄 License

MIT License — free to use, modify, and distribute.

---

## ☕ Support

If you find SubCast useful, consider supporting development:

[Buy me a coffee on Ko-fi](https://ko-fi.com/subcast)

---

*SubCast is not affiliated with Twitch, YouTube, or Kick.*
