#!/usr/bin/env python3
"""Spanish voice helper: search YouTube, then play a pick on the Roku via Roukito."""

import json
import re
import socket
import ssl
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
ROUKITO_URL = "http://127.0.0.1:8787/mcp"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:1.5b"
YT_DLP = ROOT / ".venv" / "bin" / "yt-dlp"
if not YT_DLP.exists():
    YT_DLP = Path("yt-dlp")
HOST = "0.0.0.0"
PORT = 8790

sessions: dict[str, dict] = {}

ONES = {
    "1": 1,
    "uno": 1,
    "una": 1,
    "primero": 1,
    "primera": 1,
    "2": 2,
    "dos": 2,
    "segundo": 2,
    "segunda": 2,
    "3": 3,
    "tres": 3,
    "tercero": 3,
    "tercera": 3,
    "4": 4,
    "cuatro": 4,
    "cuarto": 4,
    "cuarta": 4,
    "5": 5,
    "cinco": 5,
    "quinto": 5,
    "quinta": 5,
}
NUMBER_WORDS = ("", "uno", "dos", "tres", "cuatro", "cinco")
NUMBER_LABELS = ("Uno", "Dos", "Tres", "Cuatro", "Cinco")
MAX_RESULTS = 5
CANCEL = {"cancela", "cancelar", "nada", "para", "parar", "salir"}
YES = {"si", "sí", "yes", "claro", "bueno", "ok", "okay", "dale", "busca", "buscar", "correcto", "exacto", "bien", "adelante"}
NO = {"no", "nel", "nop", "cambiar", "cambia", "otro", "otra"}
REPEAT = {"repite", "repetir", "otra", "otravez", "denuevo"}


def speakable_title(title: str) -> str:
    text = re.sub(r"\s+", " ", title or "").strip()
    text = re.sub(r"[|#*_]+", " ", text)
    if len(text) > 42:
        text = text[:42].rsplit(" ", 1)[0]
    return text


def search_youtube(query: str) -> list[dict]:
    proc = subprocess.run(
        [
            str(YT_DLP),
            "--skip-download",
            "--ignore-errors",
            "--no-warnings",
            "--print",
            "%(id)s\t%(language)s\t%(title)s\t%(channel)s\t%(duration)s",
            "--playlist-end",
            "20",
            f"ytsearch20:{query}",
        ],
        capture_output=True,
        text=True,
        timeout=50,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(proc.stderr.strip() or "yt-dlp failed")
    videos = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        video_id, language, title, channel = parts[0], parts[1], parts[2], parts[3]
        duration = parts[4] if len(parts) > 4 else ""
        if not video_id or not title or title == "NA":
            continue
        if not language.lower().startswith("es"):
            continue
        videos.append(
            {
                "id": video_id,
                "title": speakable_title(title),
                "channel": speakable_title("" if channel == "NA" else channel),
                "duration": duration_phrase(duration),
            }
        )
        if len(videos) == MAX_RESULTS:
            break
    return videos


def duration_phrase(raw: str) -> str:
    if not raw or raw == "NA":
        return ""
    try:
        total = int(float(raw))
    except ValueError:
        return ""
    if total < 45:
        return "el video dura menos de un minuto"
    minutes = max(1, round(total / 60))
    if minutes == 1:
        return "el video dura un minuto"
    return f"el video dura {minutes} minutos"


def ask_phrase(count: int) -> str:
    count = max(1, min(MAX_RESULTS, count))
    if count == 1:
        return "Di uno."
    names = NUMBER_WORDS[1 : count + 1]
    return "Di " + ", ".join(names[:-1]) + " o " + names[-1] + "."


def list_phrase(videos: list[dict]) -> str:
    parts = []
    for i, video in enumerate(videos[:MAX_RESULTS]):
        title = video["title"].rstrip(".!? ")
        duration = video.get("duration") or ""
        if duration:
            parts.append(f"{NUMBER_LABELS[i]}. {title}, {duration}.")
        else:
            parts.append(f"{NUMBER_LABELS[i]}. {title}.")
    count = NUMBER_WORDS[len(parts)]
    return f"Encontré {count}. " + " ".join(parts) + " " + ask_phrase(len(parts))


def choice_number(text: str) -> int | None:
    words = re.sub(r"[^a-záéíóúüñ0-9 ]", " ", text.lower())
    words = words.replace("vez", " vez ")
    compact = words.replace(" ", "")
    if "otravez" in compact or "denuevo" in compact:
        return None
    for token in words.split():
        if token in ONES:
            return ONES[token]
    return None


MENU = "¿Qué quieres hacer? Uno, buscar un video. Dos, detener el video. Tres, reproducir el video."
STOP_WORDS = {"detener", "deten", "pausa", "pausar", "stop"}
PLAY_WORDS = {"reproducir", "reproduce", "play", "continuar", "continua", "sigue", "seguir"}
SEARCH_WORDS = {"buscar", "busca", "ver"}


def menu_choice(words: list[str], tokens: set[str]) -> int | None:
    number = choice_number(" ".join(words))
    if number in (1, 2, 3):
        return number
    if words and words[0] == "para" and len(words) > 2:
        return 2
    if tokens & STOP_WORDS:
        return 2
    if tokens & PLAY_WORDS:
        return 3
    if tokens & SEARCH_WORDS:
        return 1
    return None


def play_on_roku(video_id: str) -> None:
    roku_tool("roku_play_youtube", {"video": video_id})


def control_playback(action: str) -> None:
    roku_tool("roku_playback", {"action": action})


def roku_tool(name: str, arguments: dict) -> None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        }
    ).encode()
    request = urllib.request.Request(
        ROUKITO_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read().decode()
    except (urllib.error.URLError, TimeoutError, socket.timeout) as error:
        raise TvOff(str(error)) from error
    match = re.search(r"data: (\{.*\})", raw)
    if not match:
        raise TvOff("Roukito did not answer")
    payload = json.loads(match.group(1))
    if payload.get("error") or (payload.get("result") or {}).get("isError"):
        message = json.dumps(payload, ensure_ascii=False).lower()
        if any(word in message for word in ("timeout", "unreachable", "refused", "connect", "power", "offline", "network")):
            raise TvOff(message)
        raise RuntimeError(message)


class TvOff(Exception):
    pass


def search_term(heard: str) -> str:
    """Turn a spoken sentence into a short YouTube query. Keep the sentence if the model fails."""
    prompt = (
        "Convierte lo que dice una persona en una búsqueda corta de YouTube, en español. "
        "Responde solo con la búsqueda, en una línea, sin comillas ni explicación.\n"
        "Persona: quiero ver cómo hacer chilaquiles por favor\n"
        "Búsqueda: cómo hacer chilaquiles\n"
        "Persona: ponme un video de Juan Gabriel\n"
        "Búsqueda: Juan Gabriel\n"
        f"Persona: {heard}\n"
        "Búsqueda:"
    )
    body = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 24},
        }
    ).encode()
    request = urllib.request.Request(
        OLLAMA_URL,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError, OSError) as error:
        print(f"search term fallback: {error}", flush=True)
        return heard
    line = str(payload.get("response") or "").strip().splitlines()
    term = line[0].strip() if line else ""
    term = re.sub(r"^[\"'`]+|[\"'`]+$", "", term)
    term = re.sub(r"^(búsqueda|busqueda)\s*:\s*", "", term, flags=re.IGNORECASE)
    term = re.sub(r"\s+", " ", term).strip(" .")
    if not term or len(term) > 80 or term.lower().startswith(("claro", "por supuesto", "aquí", "aqui")):
        print(f"search term rejected: {term!r}", flush=True)
        return heard
    return term


def answer_kind(words: list[str]) -> str:
    if words and words[0] in NO:
        return "no"
    if set(words) & YES:
        return "yes"
    return ""


def turn(session_id: str, text: str, client_phase: str = "") -> dict:
    session = sessions.setdefault(session_id, {"phase": "menu", "videos": [], "query": ""})
    if client_phase == "menu":
        session["phase"] = "menu"
        session["videos"] = []
        session["query"] = ""
    heard = (text or "").strip()
    lowered = re.sub(r"[^a-záéíóúüñ0-9 ]", "", heard.lower())
    tokens = set(lowered.split())

    words = lowered.split()
    # "para" is also a normal Spanish word ("algo para escuchar"), so it only
    # cancels when the request is just that command.
    cancel = bool(words) and (
        words[0] in (CANCEL - {"para"}) or (words[0] == "para" and len(words) <= 2)
    )
    if cancel:
        session["phase"] = "topic"
        session["videos"] = []
        session["query"] = ""
        return {"say": "Cancelado. Pulsa el botón cuando quieras.", "phase": "topic"}

    if session["phase"] == "menu":
        picked = menu_choice(words, tokens)
        if picked == 1:
            session["phase"] = "topic"
            return {"say": "¿Qué quieres ver?", "phase": "ask"}
        if picked in (2, 3):
            action = "pause" if picked == 2 else "play"
            try:
                control_playback(action)
            except TvOff:
                return {
                    "say": "No encuentro la tele. Enciéndela y pulsa el botón otra vez.",
                    "phase": "topic",
                }
            session["phase"] = "topic"
            said = "Detuve el video." if picked == 2 else "Sigue el video."
            return {"say": f"Listo. {said} Pulsa el botón cuando quieras.", "phase": "done"}
        return {"say": MENU, "phase": "menu"}

    if session["phase"] == "confirm":
        kind = answer_kind(words)
        query = session.get("query") or ""
        if kind == "yes" and query:
            print(f"confirmed {query!r}", flush=True)
            videos = search_youtube(query)
            if not videos:
                session["phase"] = "topic"
                session["query"] = ""
                return {"say": "No encontré videos. Pulsa el botón para intentar otro tema.", "phase": "topic"}
            session["phase"] = "choice"
            session["videos"] = videos
            session["query"] = ""
            return {"say": list_phrase(videos), "phase": "choice", "videos": videos}
        if kind == "no":
            session["phase"] = "topic"
            session["query"] = ""
            return {"say": "¿Qué quieres ver?", "phase": "ask"}
        return {
            "say": f"¿Busco {query}? Di sí o no." if query else "Di sí o no.",
            "phase": "confirm",
        }

    if session["phase"] == "choice" and (tokens & REPEAT or "otravez" in lowered.replace(" ", "") or "denuevo" in lowered.replace(" ", "")):
        videos = session.get("videos") or []
        if not videos:
            session["phase"] = "topic"
            return {"say": "Pulsa el botón y dime qué quieres ver.", "phase": "topic"}
        return {"say": list_phrase(videos), "phase": "choice", "videos": videos}

    if session["phase"] == "choice":
        number = choice_number(heard)
        videos = session.get("videos") or []
        if number is None or number > len(videos):
            return {
                "say": "No entendí. " + ask_phrase(len(videos) or 1),
                "phase": "choice",
                "videos": videos,
            }
        video = videos[number - 1]
        try:
            play_on_roku(video["id"])
        except TvOff:
            return {
                "say": "No encuentro la tele. Enciéndela y pulsa el botón otra vez.",
                "phase": "topic",
            }
        session["phase"] = "topic"
        session["videos"] = []
        return {
            "say": "Listo. Ya está en la tele. Pulsa el botón para otro video.",
            "phase": "done",
            "video": video,
        }

    if not heard:
        return {"say": "No te escuché. Pulsa el botón e inténtalo otra vez.", "phase": "topic"}

    query = search_term(heard)
    print(f"search {heard!r} -> {query!r}", flush=True)
    session["phase"] = "confirm"
    session["query"] = query
    return {"say": f"¿Busco {query}?", "phase": "confirm"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not isinstance(self.connection, ssl.SSLSocket):
            host = self.headers.get("Host", f"192.168.68.100:{PORT}").split(":")[0]
            target = f"https://{host}:{PORT}{self.path}"
            body = f"Abre {target}\n".encode()
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            page = (STATIC / "index.html").read_bytes()
            self._send(200, page, "text/html; charset=utf-8")
            return
        if path == "/health":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        self._send(404, b"no\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/turn":
            self._send(404, b"no\n", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            result = turn(
                str(payload.get("session") or "phone"),
                str(payload.get("text") or ""),
                str(payload.get("phase") or ""),
            )
            body = json.dumps(result, ensure_ascii=False).encode()
            self._send(200, body, "application/json; charset=utf-8")
        except Exception as error:
            body = json.dumps(
                {"say": "Hubo un problema. Pulsa el botón e inténtalo otra vez.", "phase": "topic", "error": str(error)},
                ensure_ascii=False,
            ).encode()
            self._send(500, body, "application/json; charset=utf-8")


class VoiceServer(ThreadingHTTPServer):
    def __init__(self, address, handler, context: ssl.SSLContext) -> None:
        self.ssl_context = context
        super().__init__(address, handler)

    def process_request(self, request, client_address) -> None:
        thread = threading.Thread(
            target=self._handle_client,
            args=(request, client_address),
            daemon=True,
        )
        thread.start()

    def _handle_client(self, request, client_address) -> None:
        try:
            request.settimeout(12)
            first = request.recv(1, socket.MSG_PEEK)
            if first[:1] == b"\x16":
                request = self.ssl_context.wrap_socket(request, server_side=True)
            self.finish_request(request, client_address)
        except Exception as error:
            print(f"{client_address[0]} closed: {error}")
        finally:
            self.shutdown_request(request)


def main() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ROOT / "cert.pem", ROOT / "key.pem")
    server = VoiceServer((HOST, PORT), Handler, context)
    print(f"roukito-voz http://{HOST}:{PORT} and https://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
