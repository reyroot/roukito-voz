"""Ask the local Ollama model what a spoken sentence means at this point of the dialogue."""

import json
import os
import socket
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "15"))

# What each phase lets the person do. The model may only answer with these, so
# it cannot, say, start playback while the phone is asking "¿Busco gatos?".
ACTIONS = {
    "menu": ["buscar", "detener", "reproducir", "lista_guardada", "cancelar", "no_entendi"],
    "topic": ["buscar", "cancelar", "no_entendi"],
    "confirm": ["si", "no", "buscar", "cancelar", "no_entendi"],
    "choice": ["elegir", "repetir", "buscar", "cancelar", "no_entendi"],
}

GUIDE = """Eres el oído de un control remoto por voz para una tele Roku. Lo usa un hombre ciego que habla español. \
Él oye una pregunta del teléfono y contesta hablando. Tú recibes la pregunta y lo que él dijo, tal como lo \
transcribió el reconocimiento de voz, que a veces se equivoca en alguna palabra. Decide qué quiso decir y \
contesta solo con el JSON.

Acciones:
- buscar: quiere ver algo en YouTube. En "tema" pon lo que quiere ver, corto, en español, sin "quiero ver", \
"ponme", "un video de", "por favor" ni "en YouTube". Si no dijo qué quiere ver, "tema" vacío.
- detener: quiere parar o pausar la tele.
- reproducir: quiere que el video pausado siga.
- lista_guardada: quiere elegir de la última búsqueda, la lista de antes.
- si: acepta la búsqueda propuesta.
- no: rechaza la búsqueda propuesta sin decir otra.
- elegir: escoge un video de la lista. En "numero" pon su posición, de 1 a 5. Puede decir la posición o \
describir el video por su título, su canal o su tema.
- repetir: quiere oír la lista otra vez.
- cancelar: quiere dejarlo, salir o no hacer nada.
- no_entendi: no se entiende.
"numero" es 0 salvo en elegir.

Ejemplos:
Menú. Dijo "ponme algo para dormir por favor" -> {"accion": "buscar", "tema": "música para dormir", "numero": 0}
Menú. Dijo "quiero ver la novela la rosa de guadalupe" -> {"accion": "buscar", "tema": "la rosa de guadalupe", "numero": 0}
Menú. Dijo "los tres cerditos" -> {"accion": "buscar", "tema": "los tres cerditos", "numero": 0}
Menú. Dijo "quita eso de la tele" -> {"accion": "detener", "tema": "", "numero": 0}
Menú. Dijo "déjalo así" -> {"accion": "cancelar", "tema": "", "numero": 0}
Menú. Dijo "lo que busqué antes" -> {"accion": "lista_guardada", "tema": "", "numero": 0}
Qué quieres ver. Dijo "algo de pelota cubana" -> {"accion": "buscar", "tema": "pelota cubana", "numero": 0}
Busco gatos. Dijo "así mismo" -> {"accion": "si", "tema": "", "numero": 0}
Busco gatos. Dijo "no, mejor de perros" -> {"accion": "buscar", "tema": "perros", "numero": 0}
Busco gatos. Dijo "no quiero eso" -> {"accion": "no", "tema": "", "numero": 0}
Lista: 1. Receta de arroz congrí (canal Cocina Cubana) 2. Noticias de Miami (canal NTN24) 3. Final de la Serie del Caribe (canal Deportes TV). Dijo "el del juego de pelota" -> {"accion": "elegir", "tema": "", "numero": 3}
Misma lista. Dijo "el de las noticias" -> {"accion": "elegir", "tema": "", "numero": 2}
Misma lista. Dijo "el de cocina" -> {"accion": "elegir", "tema": "", "numero": 1}
Misma lista. Dijo "ninguno, busca de pesca" -> {"accion": "buscar", "tema": "pesca", "numero": 0}"""


def schema(phase: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "accion": {"type": "string", "enum": ACTIONS[phase]},
            "tema": {"type": "string"},
            "numero": {"type": "integer", "minimum": 0, "maximum": 5},
        },
        "required": ["accion", "tema", "numero"],
    }


def context(phase: str, question: str, videos: list[dict], query: str) -> str:
    where = {"menu": "Menú", "topic": "Qué quieres ver", "confirm": "Busco", "choice": "Lista"}[phase]
    lines = [f"Momento: {where}.", f"Pregunta del teléfono: {question}"]
    if phase == "confirm" and query:
        lines.append(f"Búsqueda propuesta: {query}")
    if phase == "choice" and videos:
        lines.append("Videos de la lista:")
        for index, video in enumerate(videos, start=1):
            channel = f" (canal {video['channel']})" if video.get("channel") else ""
            lines.append(f"{index}. {video['title']}{channel}")
    return "\n".join(lines)


def understand(phase: str, heard: str, question: str, videos: list[dict] | None = None, query: str = "") -> dict | None:
    """Return {"accion", "tema", "numero"}, or None when the model is unavailable or answers nonsense."""
    phase = phase if phase in ACTIONS else "topic"
    body = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": GUIDE},
                {
                    "role": "user",
                    "content": context(phase, question, videos or [], query) + f"\nÉl dijo: {heard}",
                },
            ],
            "format": schema(phase),
            "stream": False,
            "keep_alive": "24h",
            "options": {"temperature": 0, "num_predict": 60},
        }
    ).encode()
    request = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT) as response:
            payload = json.loads(response.read().decode())
        answer = json.loads(payload["message"]["content"])
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"ollama unavailable: {error}", flush=True)
        return None
    action = answer.get("accion")
    if action not in ACTIONS[phase]:
        print(f"ollama answered {answer!r}", flush=True)
        return None
    try:
        number = int(answer.get("numero") or 0)
    except (TypeError, ValueError):
        number = 0
    return {"accion": action, "tema": str(answer.get("tema") or "").strip(" .¿?¡!\"'"), "numero": number}
