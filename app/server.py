"""Ida-Telegram MCP Server.

Stellt genau einer, in TELEGRAM_CHAT_ID fest konfigurierten Person eine
Telegram-Nachricht als MCP-Tool fuer Claude bereit -- ueber Streamable HTTP,
damit es per Remote-MCP-Verbindung (z.B. ueber einen Cloudflare Tunnel)
genutzt werden kann. Der Endpunkt ist per Shared-Secret-Token abgesichert
(siehe app/auth.py). Absichtlich kein Parameter fuer eine andere chat_id --
dieser Server kann strukturell nur an die eine konfigurierte Person schreiben.

Wenn AUTOREPLY_ENABLED=true erkennt ein Hintergrund-Loop (app/telegram_poller.py)
neue Telegram-Nachrichten und triggert dafuer eine claude.ai Routine ueber
deren eigenen API-Token (ROUTINE_ID/ROUTINE_API_KEY). Der Container ruft
dabei selbst keine Claude-API auf -- die Routine liest die Nachricht(en)
ueber das Tool neue_nachrichten_abrufen und antwortet ueber nachricht_senden.

Persistentes, themenuebergreifendes Wissen (ueber diesen einen Bot hinaus)
liegt NICHT hier, sondern im separaten Ida-Memory MCP-Server, den die
Routine zusaetzlich als eigenen Connector nutzt.
"""

from __future__ import annotations

import logging

import uvicorn
from mcp.server.fastmcp import FastMCP, Image
from starlette.responses import JSONResponse

from app.auth import BearerAuthMiddleware
from app.config import load_settings
from app.telegram_client import TelegramClient
from app.telegram_poller import TelegramPoller, _entry_text, start_background
from app.transcription import TranscriptionError, transcribe_audio

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("ida-telegram")

settings = load_settings()
client = TelegramClient(settings)
poller = TelegramPoller(settings, client) if settings.autoreply_enabled else None

mcp = FastMCP(
    "Ida-Telegram",
    instructions=(
        "Werkzeuge fuer die eine fest konfigurierte Person: "
        "neue_nachrichten_abrufen liest, was sie gerade geschrieben hat -- "
        "inklusive Fotos als echte Bildinhalte, die direkt angeschaut werden "
        "koennen. Sprachnachrichten kommen als Hinweistext mit einer voice_id "
        "-- mit sprachnachricht_transkribieren(voice_id) den Inhalt lokal "
        "transkribieren lassen, dann normal darauf antworten. "
        "chat_verlauf liefert zusaetzlich die letzten paar Nachrichten "
        "(beide Richtungen, nur als Text, beliebig oft abrufbar) fuer "
        "Gespraechskontext -- Fotos darin nur als Platzhalter, echte Bilder "
        "gibt es ausschliesslich einmalig ueber neue_nachrichten_abrufen. "
        "nachricht_senden schickt eine Antwort. Es gibt keinen "
        "Empfaenger-Parameter -- alle Tools betreffen immer nur die in "
        "TELEGRAM_CHAT_ID hinterlegte Person."
    ),
    host=settings.mcp_host,
    port=settings.mcp_port,
)


@mcp.tool()
def nachricht_senden(text: str) -> dict:
    """Sendet eine Textnachricht an die fest konfigurierte Person via Telegram.

    text: Nachrichtentext (max. 4096 Zeichen, reiner Text ohne Markdown-Formatierung).
    Gibt bei Erfolg message_id und Zeitpunkt zurueck.
    """
    result = client.send_message(text)
    if poller is not None:
        poller.notify_reply_sent()
        poller.record_outgoing(text)
    return result


@mcp.tool()
def neue_nachrichten_abrufen() -> list:
    """Gibt zurueck, was diesen Lauf ausgeloest hat: Text als String, Fotos als
    echten Bildinhalt (direkt anschaubar), Bildunterschriften als eigener
    Text danach. Sprachnachrichten liefern einen Hinweistext mit einer
    voice_id -- mit sprachnachricht_transkribieren(voice_id) den Inhalt
    abrufen.

    Jeder Eintrag wird nur einmal ausgeliefert (Zwischenspeicher wird beim
    Abrufen geleert). Leere Liste, wenn AUTOREPLY_ENABLED=false ist oder
    gerade nichts Neues ansteht.
    """
    if poller is None:
        return []

    content: list = []
    for entry in poller.pending_entries():
        if entry["kind"] == "text":
            content.append(entry["text"])
        elif entry["kind"] == "photo":
            content.append(Image(data=entry["data"], format="jpeg"))
            if entry.get("caption"):
                content.append(f"Bildunterschrift: {entry['caption']}")
        elif entry["kind"] == "voice":
            content.append(_entry_text(entry))
    return content


@mcp.tool()
def sprachnachricht_transkribieren(voice_id: str) -> str:
    """Transkribiert eine zwischengespeicherte Sprachnachricht zu Text --
    laeuft lokal in diesem Container (faster-whisper), keine Audiodaten
    verlassen die eigene Infrastruktur.

    voice_id: aus dem Hinweistext von neue_nachrichten_abrufen() (z.B.
    "[Sprachnachricht, 12s -- zum Verstehen sprachnachricht_transkribieren(...)]").
    Schlaegt fehl, wenn WHISPER_ENABLED=false ist, die id unbekannt/zu alt
    ist (nur die letzten paar Sprachnachrichten werden vorgehalten), oder
    das Modell noch laedt (erster Aufruf ueberhaupt kann dadurch spuerbar
    laenger dauern als spaetere).
    """
    if not settings.whisper_enabled:
        raise ValueError("WHISPER_ENABLED=false -- Sprachnachrichten-Transkription ist deaktiviert.")
    if poller is None:
        raise ValueError("AUTOREPLY_ENABLED=false -- keine Sprachnachrichten verfuegbar.")

    audio = poller.get_voice_audio(voice_id)
    if audio is None:
        raise ValueError(
            f"Keine zwischengespeicherte Sprachnachricht mit voice_id={voice_id!r} gefunden "
            "(unbekannt oder schon zu lange her -- nur die letzten paar werden vorgehalten)."
        )

    try:
        return transcribe_audio(audio, settings)
    except TranscriptionError as exc:
        raise ValueError(str(exc)) from exc


@mcp.tool()
def chat_verlauf() -> list[dict]:
    """Gibt die letzten CHAT_HISTORY_LENGTH Nachrichten zurueck (Standard 5),
    eingehend UND ausgehend, als leichtgewichtige Text-Zusammenfassung --
    fuer zusaetzlichen Gespraechskontext, unabhaengig davon, was diesen Lauf
    ausgeloest hat.

    Jeder Eintrag: {"richtung": "eingehend"|"ausgehend", "text": str}. Fotos
    stehen hier nur als Platzhalter ("[Foto]"), nicht als echte Bilddaten --
    die liefert ausschliesslich neue_nachrichten_abrufen, sonst wuerde jeder
    Aufruf unnoetig Tokens fuer laengst gesehene Bilder verbrauchen. Anders
    als neue_nachrichten_abrufen NICHT destruktiv -- beliebig oft abrufbar,
    liefert immer den aktuellen Kurzverlauf. Leere Liste, wenn
    AUTOREPLY_ENABLED=false ist.
    """
    if poller is None:
        return []
    return poller.chat_verlauf()


@mcp.tool()
def bot_status() -> dict:
    """Prueft, ob der Telegram-Bot-Token gueltig und die Bot-API erreichbar ist.

    Sendet keine Nachricht, nur ein Verbindungstest (Telegrams getMe).
    """
    return client.bot_status()


async def healthz(request):
    return JSONResponse({"status": "ok"})


def build_app():
    app = mcp.streamable_http_app()
    app.add_route("/healthz", healthz, methods=["GET"])
    app.add_middleware(BearerAuthMiddleware, token=settings.mcp_auth_token)
    return app


def main() -> None:
    app = build_app()
    log.info(
        "Ida-Telegram MCP Server startet auf %s:%s (Endpunkt: /mcp, Health: /healthz)",
        settings.mcp_host,
        settings.mcp_port,
    )
    if poller is not None:
        start_background(poller)
    else:
        log.info("AUTOREPLY_ENABLED=false -- automatisches Antworten ist deaktiviert.")
    # access_log=False: uvicorn wuerde sonst jede Request-Zeile inkl. vollem
    # Pfad loggen -- und damit ein per ?token= mitgeschicktes MCP_AUTH_TOKEN
    # im Klartext in die Docker-Logs schreiben.
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
