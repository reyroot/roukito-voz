# Roukito voz

Voice remote for a blind Spanish speaker. One button on an Android phone.

He hears “¿Qué quieres hacer?” and then:

1. Buscar un video. The phone confirms the search term, reads up to five YouTube results, and he picks one by number. The Roku plays that video.
2. Detener el video.
3. Reproducir el video.
4. Elegir de la última búsqueda. The phone reads the saved results again, or plays the one he names.

The phone beeps when the microphone is ready. With TalkBack on, the prompts go through TalkBack. With TalkBack off, the app speaks itself.

## Run the server

On the same machine as Roukito. The container uses the host network so it can reach Roukito on `127.0.0.1`.

```bash
docker compose up -d --build
```

The last search is saved in `data/` next to the compose file.

Roukito must be listening at `http://127.0.0.1:8787/mcp`, and Ollama at `http://127.0.0.1:11434` with `qwen2.5:1.5b` (`ollama pull qwen2.5:1.5b`).

## How it understands him

Short, clear answers (“tres”, “sí”, “no”, “para”, “repite”, or words from one title in the list) are matched by word rules in `server.py`, instantly. Anything else goes to the Ollama model in `understand.py`, which says what he meant at that point of the dialogue: search for what, stop, resume, pick which video, and so on. The model can only answer with the actions that make sense for the question he was asked. If Ollama is down, slow or unsure, the word rules decide, so the remote keeps working without it.

Set `OLLAMA_MODEL` in `docker-compose.yml` to try another model. On this CPU-only server `qwen2.5:1.5b` answers in about 1.5 seconds; `llama3.2:3b` took twice as long and picked the wrong video more often.

The phone calls `http://<server>:8790/api/turn`. Set that address in `MainActivity` before building the app. The phone and the server must be on the same Wi-Fi.

## Android

Open `android/` in Android Studio, or build the debug APK with Gradle. The app needs the microphone permission.
