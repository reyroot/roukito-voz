# Roukito voz

Voice remote for a blind Spanish speaker. One button on an Android phone.

He hears “¿Qué quieres hacer?” and then:

1. Buscar un video. The phone confirms the search term, reads up to five YouTube results, and he picks one by number. The Roku plays that video.
2. Detener el video.
3. Reproducir el video.

The phone beeps when the microphone is ready. With TalkBack on, the prompts go through TalkBack. With TalkBack off, the app speaks itself.

## Run the server

On the same machine as Roukito and Ollama:

```bash
python3 -m venv .venv
.venv/bin/pip install yt-dlp
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 825 -nodes -subj "/CN=roukito-voz"
.venv/bin/python server.py
```

Roukito must be listening at `http://127.0.0.1:8787/mcp`. Ollama must be listening at `http://127.0.0.1:11434` with `qwen2.5:1.5b`, which turns a spoken sentence into a short YouTube search.

The phone calls `http://<server>:8790/api/turn`. Set that address in `MainActivity` before building the app. The phone and the server must be on the same Wi-Fi.

## Android

Open `android/` in Android Studio, or build the debug APK with Gradle. The app needs the microphone permission.
