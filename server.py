#!/usr/bin/env python3
"""Spanish voice helper for the Android app: search YouTube, then play a pick on the Roku via Roukito."""

import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ROUKITO_VOZ_DATA", ROOT))
LAST_SEARCH = DATA / "last_search.json"
ROUKITO_URL = "http://127.0.0.1:8787/mcp"
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
FILLER = {
    "quiero", "queria", "quería", "ver", "video", "videos", "por", "favor",
    "porfavor", "ponme", "pon", "pone", "busca", "buscar", "el", "la", "los", "las",
    "un", "una", "me", "mi", "mis", "porfa", "porfis", "anda", "reproducir",
    "poner", "de", "del", "al", "a", "y", "en", "que", "qué", "lo", "le", "les",
    "te", "se", "con", "para", "oye", "oiga", "hola", "gracias", "quieres",
    "puedes", "puede", "please",
}
LIST_WORDS = {"lista", "ultima", "último", "última", "anterior", "guardada", "guardado"}
LIST_COMMAND = FILLER | LIST_WORDS | {
    "elegir", "elige", "busqueda", "búsqueda", "numero", "número",
}


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


MENU = "¿Qué quieres hacer? Uno, buscar un video. Dos, detener el video. Tres, reproducir el video. Cuatro, elegir de la última búsqueda."
STOP_WORDS = {"detener", "deten", "pausa", "pausar", "stop"}
PLAY_WORDS = {"reproducir", "reproduce", "play", "continuar", "continua", "sigue", "seguir"}
SEARCH_WORDS = {"buscar", "busca", "ver"}


def menu_choice(words: list[str], tokens: set[str]) -> int | None:
    if mentions_saved_list(words, tokens):
        return 4
    number = choice_number(" ".join(words))
    if number in (1, 2, 3):
        return number
    if words and words[0] == "para":
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


def tokens_of(text: str) -> list[str]:
    return re.sub(r"[^a-záéíóúüñ0-9 ]", " ", text.lower()).split()


def fold_word(word: str) -> str:
    return word.translate(str.maketrans("áéíóúüñ", "aeiouun"))


def near(left: str, right: str) -> bool:
    left, right = fold_word(left), fold_word(right)
    if left == right:
        return True
    if len(left) >= 4 and len(right) >= 4 and (left in right or right in left):
        return True
    if min(len(left), len(right)) < 4 or abs(len(left) - len(right)) > 2:
        return False
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, 1):
        current = [index]
        for other_index, other in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[other_index] + 1,
                previous[other_index - 1] + (char != other),
            ))
        previous = current
    return previous[-1] <= 2


def content_words(text: str) -> list[str]:
    return [word for word in tokens_of(text) if word not in FILLER and (len(word) > 2 or word.isdigit())]


def search_term(heard: str) -> str:
    """Keep the topic words and drop Spanish filler."""
    raw = re.sub(r"[^0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]", " ", heard)
    raw = re.sub(r"\s+", " ", raw).strip()
    kept = []
    for word in raw.split():
        token = word.lower()
        if token in FILLER or (len(token) <= 2 and not token.isdigit()):
            continue
        kept.append(word)
    if not kept:
        return re.sub(r"\s+", " ", heard).strip()
    return " ".join(kept)


def pick_by_title(heard: str, videos: list[dict]) -> int:
    ignored = folded_tokens(LIST_COMMAND) | folded_tokens(ONES)
    wanted = [word for word in content_words(heard) if fold_word(word) not in ignored]
    if not wanted:
        return 0
    best_index = 0
    best_score = 0
    for index, video in enumerate(videos, start=1):
        haystack = tokens_of(f"{video.get('title') or ''} {video.get('channel') or ''}")
        score = sum(1 for word in wanted if any(near(word, other) for other in haystack))
        if score > best_score:
            best_score = score
            best_index = index
    return best_index if best_score else 0


def resolve_choice(heard: str, videos: list[dict]) -> int:
    number = choice_number(heard)
    if number is not None:
        return number if 1 <= number <= len(videos) else 0
    return pick_by_title(heard, videos)


def save_search(query: str, videos: list[dict]) -> None:
    # Saving is a convenience for option four; a failure must not lose the results.
    try:
        LAST_SEARCH.write_text(
            json.dumps({"query": query, "videos": videos}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as error:
        print(f"could not save {LAST_SEARCH}: {error}", flush=True)


def load_search() -> dict | None:
    try:
        data = json.loads(LAST_SEARCH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    videos = data.get("videos") or []
    if not videos:
        return None
    return {"query": data.get("query") or "", "videos": videos}


def folded_tokens(words: list[str] | set[str]) -> set[str]:
    return {fold_word(word) for word in words}


def mentions_saved_list(words: list[str], tokens: set[str]) -> bool:
    if choice_number(" ".join(words)) == 4:
        return True
    if folded_tokens(tokens) & folded_tokens(LIST_WORDS):
        return True
    compact = "".join(fold_word(word) for word in words)
    return "delalista" in compact or "ultimabusqueda" in compact


def utterance_picks_saved(heard: str) -> bool:
    words = tokens_of(heard)
    if not words:
        return False
    number = choice_number(heard)
    command = folded_tokens(LIST_COMMAND) | folded_tokens(ONES)
    content = [word for word in words if fold_word(word) not in command]
    if content:
        return True
    if number is None:
        return False
    if number == 4 and not (folded_tokens(words) & folded_tokens(LIST_WORDS)):
        return False
    return 1 <= number <= MAX_RESULTS


def answer_kind(words: list[str]) -> str:
    if words and words[0] in NO:
        return "no"
    if set(words) & YES:
        return "yes"
    return ""


def play_picked(session: dict, video: dict) -> dict:
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


def choose_video(session: dict, heard: str, videos: list[dict]) -> dict:
    number = resolve_choice(heard, videos)
    if number < 1 or number > len(videos):
        return {
            "say": "No entendí. " + ask_phrase(len(videos) or 1),
            "phase": "choice",
            "videos": videos,
        }
    return play_picked(session, videos[number - 1])


def use_saved_list(session: dict, heard: str) -> dict:
    saved = load_search()
    if not saved:
        return {
            "say": "No hay una búsqueda guardada. " + MENU,
            "phase": "menu",
        }
    videos = saved["videos"]
    session["phase"] = "choice"
    session["videos"] = videos
    if utterance_picks_saved(heard):
        return choose_video(session, heard, videos)
    return {"say": list_phrase(videos), "phase": "choice", "videos": videos}


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
    # cancels when the request is just that command. In the menu a bare "para"
    # means option two, stop the video.
    cancel = bool(words) and (
        words[0] in (CANCEL - {"para"})
        or (words[0] == "para" and len(words) <= 2 and session["phase"] != "menu")
    )
    if cancel:
        session["phase"] = "topic"
        session["videos"] = []
        session["query"] = ""
        return {"say": "Cancelado. Pulsa el botón cuando quieras.", "phase": "topic"}

    if session["phase"] == "menu":
        picked = menu_choice(words, tokens)
        if picked == 1:
            topic = menu_topic(heard)
            if topic:
                return ask_to_search(session, heard, topic)
            session["phase"] = "topic"
            return {"say": "¿Qué quieres ver?", "phase": "ask"}
        if picked == 4:
            return use_saved_list(session, heard)
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
            save_search(query, videos)
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
        videos = session.get("videos") or []
        if not videos:
            session["phase"] = "topic"
            return {"say": "Pulsa el botón y dime qué quieres ver.", "phase": "topic"}
        return choose_video(session, heard, videos)

    if not heard:
        return {"say": "No te escuché. Pulsa el botón e inténtalo otra vez.", "phase": "topic"}

    return ask_to_search(session, heard, search_term(heard))


def menu_topic(heard: str) -> str:
    """The topic in "buscar videos de gatos", so the menu need not ask again."""
    words = tokens_of(heard)
    if words and words[0] in ONES:
        heard = re.sub(r"^\W*\w+", "", heard, count=1)
    if not [word for word in content_words(heard) if word not in SEARCH_WORDS]:
        return ""
    return search_term(heard)


def ask_to_search(session: dict, heard: str, query: str) -> dict:
    print(f"search {heard!r} -> {query!r}", flush=True)
    session["phase"] = "confirm"
    session["query"] = query
    return {"say": f"¿Busco {query}?", "phase": "confirm"}


class Handler(BaseHTTPRequestHandler):
    # A phone that drops off the Wi-Fi mid-request must not hold a thread forever.
    timeout = 30

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
        if self.path.split("?", 1)[0] == "/health":
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


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"roukito-voz http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
