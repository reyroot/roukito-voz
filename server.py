#!/usr/bin/env python3
"""Spanish voice helper for the Android app: search YouTube, then play a pick on the Roku via Roukito."""

import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from understand import understand

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
    # How the recognizer sometimes writes "cinco" and "tres".
    "sinco": 5,
    "zinco": 5,
    "trece": 3,
    "quinto": 5,
    "quinta": 5,
}
NUMBER_WORDS = ("", "uno", "dos", "tres", "cuatro", "cinco")
NUMBER_LABELS = ("Uno", "Dos", "Tres", "Cuatro", "Cinco")
MAX_RESULTS = 5
CANCEL = {"cancela", "cancelar", "nada", "para", "parar", "salir"}
YES = {"si", "sí", "yes", "claro", "bueno", "ok", "okay", "dale", "busca", "buscar", "correcto", "exacto", "bien", "adelante"}
NO = {"no", "nel", "nop", "cambiar", "cambia", "otro", "otra"}
REPEAT = {"repite", "repitelo", "repítelo", "repetir", "otra", "otravez", "denuevo"}
FILLER = {
    "quiero", "queria", "quería", "ver", "video", "videos", "por", "favor",
    "porfavor", "ponme", "pon", "pone", "busca", "buscar", "el", "la", "los", "las",
    "un", "una", "me", "mi", "mis", "porfa", "porfis", "anda", "reproducir",
    "poner", "de", "del", "al", "a", "y", "en", "que", "qué", "lo", "le", "les",
    "te", "se", "con", "para", "oye", "oiga", "hola", "gracias", "quieres",
    "puedes", "puede", "please", "youtube", "gusta", "gustan", "ninguno", "ninguna",
    "mejor", "algo",
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
        elif score == best_score:
            best_index = 0  # two videos match as well, so it is not clear which one
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


# Words that carry no topic of their own. An answer made only of these is
# understood by the rules below without asking the model.
COMMAND_WORDS = (
    FILLER | LIST_COMMAND | set(ONES) | STOP_WORDS | PLAY_WORDS | SEARCH_WORDS
    | YES | NO | REPEAT | CANCEL
    | {"tele", "television", "televisión", "tv", "eso", "ese", "esa", "este", "esta", "mismo", "ya", "ahora"}
)
MENU_ACTIONS = (None, "buscar", "detener", "reproducir", "lista_guardada")
LAST_WORDS = {"ultimo", "ultima"}
# In the list, these mean "none of these, look for something else".
OTHER_SEARCH = {"busca", "buscar", "ninguno", "ninguna", "otro", "otra", "otros", "otras"}


def only_commands(words: list[str]) -> bool:
    known = folded_tokens(COMMAND_WORDS)
    return all(fold_word(word) in known for word in words)


def wants_repeat(words: list[str]) -> bool:
    compact = "".join(words)
    return bool(set(words) & REPEAT) or "otravez" in compact or "denuevo" in compact


def intent(action: str, topic: str = "", number: int = 0) -> dict:
    return {"accion": action, "tema": topic, "numero": number}


def rule_intent(phase: str, words: list[str], heard: str, videos: list[dict]) -> dict | None:
    """Short, unambiguous answers: a number, sí, no, para, repite. None means ask the model."""
    if phase == "menu" and only_commands(words):
        picked = menu_choice(words, set(words))
        if picked:
            return intent(MENU_ACTIONS[picked])
    if phase == "confirm":
        kind = answer_kind(words)
        if kind == "yes" and "no" not in words:
            return intent("si")
        if kind == "no" and only_commands(words):
            return intent("no")
    if phase == "choice":
        if wants_repeat(words):
            return intent("repetir")
        number = choice_number(heard)
        if number and only_commands(words):
            return intent("elegir", number=number)
        if folded_tokens(words) & LAST_WORDS and only_commands(words):
            return intent("elegir", number=len(videos))
        if not set(words) & OTHER_SEARCH:
            # "el de Luisito Comunica": words from one title or channel.
            picked = pick_by_title(heard, videos)
            if picked:
                return intent("elegir", number=picked)
    return None


def fallback_intent(phase: str, words: list[str], heard: str, videos: list[dict]) -> dict:
    """What the word rules make of the answer when the model is unavailable or unsure."""
    if phase == "menu":
        picked = menu_choice(words, set(words))
        if picked == 1:
            return intent("buscar", menu_topic(heard))
        return intent(MENU_ACTIONS[picked]) if picked else intent("no_entendi")
    if phase == "confirm":
        kind = answer_kind(words)
        if kind == "no":
            rest = re.sub(r"^\W*\w+", "", heard, count=1)
            return intent("buscar", search_term(rest)) if content_words(rest) else intent("no")
        return intent("si") if kind == "yes" else intent("no_entendi")
    if phase == "choice":
        if set(words) & OTHER_SEARCH:
            topic = " ".join(word for word in content_words(heard) if word not in OTHER_SEARCH)
            return intent("buscar", topic) if topic else intent("no_entendi")
        number = choice_number(heard)
        return intent("elegir", number=number) if number and number <= len(videos) else intent("no_entendi")
    return intent("buscar", search_term(heard))


def clean_topic(topic: str) -> str:
    topic = re.sub(r"\s+", " ", topic).strip(" .,¿?¡!\"'")
    topic = re.sub(r"\s*\b(en|de) youtube\b.*$", "", topic, flags=re.IGNORECASE)
    topic = re.sub(r"^(un |unos )?videos? (de |del |sobre )", "", topic, flags=re.IGNORECASE)
    return topic[:80].strip()


def understand_turn(session: dict, words: list[str], heard: str) -> dict:
    phase = session["phase"]
    videos = session.get("videos") or []
    ruled = rule_intent(phase, words, heard, videos)
    if ruled:
        print(f"rules {heard!r} -> {ruled}", flush=True)
        return ruled
    started = time.monotonic()
    guessed = understand(phase, heard, session.get("asked") or "", videos, session.get("query") or "")
    took = time.monotonic() - started
    if guessed and guessed["accion"] == "elegir" and not 1 <= guessed["numero"] <= len(videos):
        guessed = None
    if guessed and guessed["accion"] == "buscar":
        guessed["tema"] = clean_topic(guessed["tema"])
        # An empty topic in the list would throw the list away.
        if phase in ("topic", "confirm", "choice") and not guessed["tema"]:
            guessed = None
    if guessed and guessed["accion"] == "no" and not only_commands(words):
        # "no, mejor de Benny Moré": the small model hears the no and drops the topic.
        guessed = None
    if guessed and guessed["accion"] != "no_entendi":
        print(f"ollama {heard!r} -> {guessed} in {took:.1f}s", flush=True)
        return guessed
    fallback = fallback_intent(phase, words, heard, videos)
    print(f"fallback {heard!r} -> {fallback} (ollama said {guessed}, {took:.1f}s)", flush=True)
    return fallback


def reset(session: dict, phase: str = "topic") -> None:
    session["phase"] = phase
    session["videos"] = []
    session["query"] = ""


def turn(session_id: str, text: str, client_phase: str = "") -> dict:
    session = sessions.setdefault(session_id, {"phase": "menu", "videos": [], "query": "", "asked": MENU})
    if client_phase == "menu":
        reset(session, "menu")
        session["asked"] = MENU
    result = answer(session, (text or "").strip())
    session["asked"] = result["say"]
    return result


def answer(session: dict, heard: str) -> dict:
    words = tokens_of(heard)
    if not words:
        if session["phase"] == "choice":
            return {"say": "No te escuché. " + ask_phrase(len(session["videos"])), "phase": "choice", "videos": session["videos"]}
        return {"say": "No te escuché. Pulsa el botón e inténtalo otra vez.", "phase": "topic"}

    # "para" is also a normal Spanish word ("algo para escuchar"), so it only
    # cancels when the request is just that command. In the menu a bare "para"
    # means option two, stop the video.
    if words[0] in (CANCEL - {"para"}) or (words[0] == "para" and len(words) <= 2 and session["phase"] != "menu"):
        reset(session)
        return {"say": "Cancelado. Pulsa el botón cuando quieras.", "phase": "topic"}

    phase = session["phase"]
    if phase == "choice" and not session.get("videos"):
        reset(session)
        return {"say": "Pulsa el botón y dime qué quieres ver.", "phase": "topic"}

    meant = understand_turn(session, words, heard)
    action, topic, number = meant["accion"], meant["tema"], meant["numero"]

    if action == "cancelar":
        reset(session)
        return {"say": "Cancelado. Pulsa el botón cuando quieras.", "phase": "topic"}
    if action == "buscar":
        if topic:
            return ask_to_search(session, heard, topic)
        reset(session)
        return {"say": "¿Qué quieres ver?", "phase": "ask"}
    if action in ("detener", "reproducir"):
        return playback(session, action)
    if action == "lista_guardada":
        return use_saved_list(session, heard)

    if phase == "confirm":
        query = session.get("query") or ""
        if action == "si" and query:
            return run_search(session, query)
        if action == "no":
            reset(session)
            return {"say": "¿Qué quieres ver?", "phase": "ask"}
        return {"say": f"¿Busco {query}? Di sí o no." if query else "Di sí o no.", "phase": "confirm"}

    if phase == "choice":
        videos = session["videos"]
        if action == "repetir":
            return {"say": list_phrase(videos), "phase": "choice", "videos": videos}
        if action == "elegir" and 1 <= number <= len(videos):
            return play_picked(session, videos[number - 1])
        return {"say": "No entendí. " + ask_phrase(len(videos)), "phase": "choice", "videos": videos}

    if phase == "menu":
        return {"say": "No entendí. " + MENU, "phase": "menu"}
    return {"say": "No entendí. ¿Qué quieres ver?", "phase": "ask"}


def playback(session: dict, action: str) -> dict:
    try:
        control_playback("pause" if action == "detener" else "play")
    except TvOff:
        return {"say": "No encuentro la tele. Enciéndela y pulsa el botón otra vez.", "phase": "topic"}
    reset(session)
    said = "Detuve el video." if action == "detener" else "Sigue el video."
    return {"say": f"Listo. {said} Pulsa el botón cuando quieras.", "phase": "done"}


def run_search(session: dict, query: str) -> dict:
    print(f"confirmed {query!r}", flush=True)
    videos = search_youtube(query)
    if not videos:
        reset(session)
        return {"say": "No encontré videos. Pulsa el botón para intentar otro tema.", "phase": "topic"}
    session["phase"] = "choice"
    session["videos"] = videos
    session["query"] = ""
    save_search(query, videos)
    return {"say": list_phrase(videos), "phase": "choice", "videos": videos}


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
    session["videos"] = []
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


def warm_up() -> None:
    """Load the model now, so the first question does not wait for it."""
    understand("menu", "hola", MENU)


def main() -> None:
    threading.Thread(target=warm_up, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"roukito-voz http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
