"""Ida-Telegram MCP Server.

Stellt genau einer, in TELEGRAM_CHAT_ID fest konfigurierten Person eine
Telegram-Nachricht als MCP-Tool fuer Claude bereit -- ueber Streamable HTTP,
damit es per Remote-MCP-Verbindung (z.B. ueber einen Cloudflare Tunnel)
genutzt werden kann. Der Endpunkt ist per Shared-Secret-Token abgesichert
(siehe app/auth.py). Absichtlich kein Parameter fuer eine andere chat_id --
dieser Server kann strukturell nur an die eine konfigurierte Person schreiben.

Wenn AUTOREPLY_ENABLED=true erkennt ein Hintergrund-Loop (app/telegram_poller.py)
neue Telegram-Nachrichten und triggert dafuer eine claude.ai Routine ueber
deren eigenen API-Token (ROUTINE_TRIGGER_URL/ROUTINE_API_KEY). Der Container
ruft dabei selbst keine Claude-API auf -- die Routine liest die Nachricht(en)
ueber das Tool neue_nachrichten_abrufen und antwortet ueber nachricht_senden.
"""

from __future__ import annotations

import logging

import uvicorn
from mcp.server.fastmcp import FastMCP, Image
from starlette.responses import JSONResponse

from app.auth import BearerAuthMiddleware
from app.config import load_settings
from app.memory_store import MemoryError, MemoryStore
from app.telegram_client import TelegramClient
from app.telegram_poller import TelegramPoller, start_background

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("ida-telegram")

settings = load_settings()
client = TelegramClient(settings)
poller = TelegramPoller(settings, client) if settings.autoreply_enabled else None
memory = MemoryStore(settings.memory_dir)

mcp = FastMCP(
    "Ida-Telegram",
    instructions=(
        "Werkzeuge fuer die eine fest konfigurierte Person: "
        "neue_nachrichten_abrufen liest, was sie gerade geschrieben hat -- "
        "inklusive Fotos als echte Bildinhalte, die direkt angeschaut werden "
        "koennen. Sprachnachrichten werden erkannt, aber nicht transkribiert "
        "(kein Audio-Verstaendnis verfuegbar) -- das steht dann als Hinweistext "
        "dabei. nachricht_senden schickt eine Antwort. "
        "\n\n"
        "Gedaechtnis (weil jeder Routine-Trigger sonst bei null anfaengt): "
        "in mehrere Themen-Dateien aufgeteilt statt einer einzigen. Immer "
        "zuerst gedaechtnis_uebersicht aufrufen (eine Zeile pro Themen-"
        "Datei) -- anhand der Beschreibungen entscheiden, welche Datei(en) "
        "fuer die aktuelle Nachricht ueberhaupt relevant sind, und nur die "
        "gezielt mit gedaechtnis_lesen(datei) laden. Nach dem Antworten nur "
        "bei wirklich merkenswerten neuen Infos mit gedaechtnis_schreiben "
        "aktualisieren -- passende bestehende Datei wiederverwenden oder "
        "bei neuem Themenbereich einen neuen Dateinamen waehlen, keine "
        "zentrale Struktur muss dafuer angepasst werden. Nicht das ganze "
        "Gespraech protokollieren, nur das kompakt Wichtige. "
        "Es gibt keinen Empfaenger-Parameter -- alle Tools betreffen immer "
        "nur die in TELEGRAM_CHAT_ID hinterlegte Person."
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
    return client.send_message(text)


@mcp.tool()
def neue_nachrichten_abrufen() -> list:
    """Gibt zurueck, was diesen Lauf ausgeloest hat: Text als String, Fotos als
    echten Bildinhalt (direkt anschaubar), Bildunterschriften als eigener
    Text danach. Sprachnachrichten liefern nur einen Hinweistext (Dauer),
    keine automatische Transkription -- der Inhalt ist nicht bekannt.

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
    return content


@mcp.tool()
def gedaechtnis_uebersicht() -> str:
    """Zeigt alle vorhandenen Gedaechtnis-Themen mit Kurzbeschreibung (eine
    Zeile pro Themen-Datei) -- OHNE deren Inhalt zu laden. IMMER als erstes
    aufrufen, bevor du antwortest: das zeigt dir kompakt und guenstig, was es
    an Wissen gibt. Anhand der Beschreibungen entscheidest du, welche
    Datei(en) fuer die aktuelle Nachricht ueberhaupt relevant sind, und
    liest nur die gezielt mit gedaechtnis_lesen -- nicht jede vorhandene
    Datei durchlesen, das waere unnoetig teuer.
    """
    return memory.overview()


@mcp.tool()
def gedaechtnis_lesen(datei: str) -> str:
    """Liest den vollen Inhalt einer einzelnen Themen-Datei.

    datei: Name ohne .md-Endung, wie in gedaechtnis_uebersicht angezeigt
    (z.B. "person"). Leerer String, wenn die Datei noch nicht existiert.
    """
    return memory.read(datei)


@mcp.tool()
def gedaechtnis_schreiben(datei: str, inhalt: str) -> dict:
    """Erstellt oder ueberschreibt eine einzelne Themen-Datei komplett (kein
    Anhaengen).

    datei: Name ohne .md-Endung, nur Buchstaben/Zahlen/_/- (z.B. "person",
    "projekte", "termine"). Fuer ein neues Themengebiet einfach einen neuen,
    treffenden Namen waehlen -- taucht automatisch in der naechsten
    gedaechtnis_uebersicht auf, keine zentrale Struktur muss angepasst
    werden. Fuer bestehende Themen den gleichen Namen wiederverwenden statt
    Duplikate anzulegen.

    inhalt: die ERSTE ZEILE wird die Kurzbeschreibung in
    gedaechtnis_uebersicht -- also kurz und praegnant. Danach frei nutzbarer
    Inhalt, kann sich mit [[anderer-dateiname]] auf andere Themen-Dateien
    beziehen. Nur wirklich merkenswerte, kompakte Fakten speichern, nicht
    das ganze Gespraech protokollieren -- das kostet bei jedem zukuenftigen
    Lauf unnoetig Tokens. Max. 20.000 Zeichen pro Datei.
    """
    try:
        saved = memory.write(datei, inhalt)
    except MemoryError as exc:
        return {"gespeichert": False, "fehler": str(exc)}
    return {"gespeichert": True, "datei": datei, "laenge": len(saved)}


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
