# Ida-Telegram

Ein eigenständiger MCP-Server (Model Context Protocol), getrennt von
Ida-Untis und ohne Verbindung dazu. Zwei Dinge in einem Container:

1. Zwei MCP-Werkzeuge für Claude: einer fest konfigurierten Person eine
   Telegram-Nachricht schicken (`nachricht_senden`), und lesen, was sie
   gerade geschrieben hat (`neue_nachrichten_abrufen`) -- Fotos kommen dabei
   als echter Bildinhalt mit, den die Routine wirklich "sehen" kann.
2. Ein Hintergrund-Loop, der neue Telegram-Nachrichten erkennt (Text, Fotos,
   Sprachnachrichten) und dafür eine **claude.ai Routine** triggert -- die
   Routine liest die Nachricht dann selbst über die MCP-Tools oben und
   antwortet.

Läuft als Docker-Container und wird über einen bestehenden Cloudflare
Tunnel unter einer eigenen Domain erreichbar gemacht.

## Architektur

```
                          claude.ai Routine (Cloud-Agent)
                           |                        ^
                    (per API-Trigger)      (MCP: nachricht_senden,
                           |                neue_nachrichten_abrufen)
                           |                        |
                           |                        v
Claude (MCP-Client)  --https-->  Cloudflare Tunnel (öffentliche Domain)
                                          |
                                          v
                              127.0.0.1:4567 auf deinem Server
                                          |
                                          v
                          Docker-Container "ida-telegram-mcp"
                                          ^
                                          | (long-polling, ausgehend)
                                 Telegram Bot HTTP-API
                                          ^
                                Telegram-Nutzer schreibt dem Bot
```

Wichtig: **Dieser Container ruft selbst nie eine Claude-API auf.** Er macht
nur zwei Dinge -- Telegram per Long-Polling nach neuen Nachrichten fragen,
und bei neuen Nachrichten eine claude.ai Routine über deren eigenen
API-Trigger anstoßen. Die eigentliche "Intelligenz" (Nachricht lesen,
Antwort formulieren) läuft komplett in der Routine bei Anthropic, die sich
dafür ganz normal als MCP-Client mit diesem Server verbindet -- genau wie
Claude Code oder claude.ai es auch tun.

Der Container published seinen Port **nur auf `127.0.0.1`** -- von außen
nicht direkt erreichbar, nur über den bereits laufenden `cloudflared`-Prozess.
Zusätzlich verlangt der Server bei jeder Anfrage ein geheimes Token
(`MCP_AUTH_TOKEN`).

**Wichtigste Design-Entscheidung:** Es gibt keinen Empfänger-Parameter.
`TELEGRAM_CHAT_ID` in der `.env` legt die einzige Person fest, an die dieser
Server jemals schreiben kann -- weder Claude noch sonst jemand kann über die
Tools eine andere chat_id angeben. Eine Nachricht erreicht eine echte Person
und lässt sich nicht zurückholen, deshalb ist der Empfänger bewusst fest
verdrahtet statt frei wählbar.

## Voraussetzungen

- Docker + Docker Compose auf dem Server
- Ein bereits eingerichteter und verbundener Cloudflare Tunnel auf diesem Server
- Ein Telegram-Bot (in wenigen Minuten selbst erstellt, siehe unten)
- Ein claude.ai-Account, um die Routine anzulegen

## 1. Telegram-Bot erstellen

1. In Telegram den Chat mit **@BotFather** öffnen.
2. `/newbot` senden, Namen vergeben -- du bekommst einen Token wie
   `123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`.
3. Die Person, die die Nachrichten empfangen soll, schreibt dem neuen Bot
   einmal eine beliebige Nachricht (z.B. "hi"). **Wichtig:** Ein Bot kann
   niemanden anschreiben, der ihm nicht vorher selbst geschrieben hat.
4. Im Browser aufrufen (mit echtem Token):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Im JSON nach `"chat":{"id": ...}` suchen -- das ist die `chat_id`.

## 2. Einrichten, bauen, starten

```bash
git clone https://github.com/<dein-user>/Ida-Telegram.git
cd Ida-Telegram
cp .env.example .env
```

`.env` erstmal mit `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` und
`MCP_AUTH_TOKEN` ausfüllen (siehe Tabelle unten) -- `ROUTINE_ID`
und `ROUTINE_API_KEY` folgen in Schritt 5, dafür muss der Server erst
erreichbar sein.

Image bauen lassen: Bei jedem Push auf `main` baut
`.github/workflows/docker-publish.yml` das Image automatisch nach
`ghcr.io/<dein-user>/ida-telegram:latest`. Einmalig auf öffentlich stellen
(GitHub -> Profil -> **Packages** -> `ida-telegram` -> Package settings ->
Change visibility -> Public), damit `docker compose` es ohne Login ziehen
kann.

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

(Mit `AUTOREPLY_ENABLED=true` als Standard startet der Container zunächst
mit Fehler, weil `ROUTINE_ID`/`ROUTINE_API_KEY` noch fehlen --
das ist normal, kommt in Schritt 5. Alternativ jetzt schon `AUTOREPLY_ENABLED=false`
setzen und später wieder auf `true`.)

## 3. An den bestehenden Cloudflare Tunnel anbinden

Analog zu Ida-Untis, nur mit eigenem Hostname und Port 4567:

```yaml
ingress:
  - hostname: telegram.deine-domain.de
    service: http://localhost:4567
  - service: http_status:404
```

(Bzw. im Zero-Trust-Dashboard unter Public Hostname eintragen.) Danach
`cloudflared` neu laden.

## 4. Als claude.ai Connector hinzufügen

Genau wie bei Ida-Untis: claude.ai -> Einstellungen -> Connectors -> Add
custom connector -> als URL
`https://telegram.deine-domain.de/mcp?token=<MCP_AUTH_TOKEN>` eintragen.

## 5. Routine anlegen (der eigentliche Auto-Antwort-Teil)

1. Auf [claude.ai/code/routines](https://claude.ai/code/routines) -> **Neue
   Routine**.
2. **Name:** z.B. "Ida Telegram Autoreply".
3. **Anweisungen:**
   > Du bist der Telegram-Assistent. Ruf ueber den Ida-Telegram-Connector das
   > Tool `neue_nachrichten_abrufen` auf, um zu sehen, was gerade geschrieben
   > wurde -- das koennen Text, Fotos (die du dir direkt anschauen kannst)
   > oder Hinweise auf Sprachnachrichten sein. Antworte darauf kurz,
   > freundlich und hilfreich auf Deutsch, indem du das Tool
   > `nachricht_senden` verwendest. Bei Sprachnachrichten kannst du den
   > Inhalt nicht hoeren -- sag das ehrlich und bitte ggf. um eine
   > Text-Nachricht. Wenn `neue_nachrichten_abrufen` nichts liefert, mach
   > nichts.
   >
   > Fuer Wissen ueber vergangene Unterhaltungen: nutze zusaetzlich den
   > Ida-Memory-Connector (separates Projekt, gemeinsames Gedaechtnis fuer
   > alle deine KI-Verbindungen -- nicht nur diesen Bot).
4. **Trigger:** "API" auswählen (nicht Zeitplan). claude.ai zeigt dir danach
   einmalig einen **API-Token** an (`sk-ant-oat01-...`) -- sofort notieren,
   er wird danach nicht mehr im Klartext angezeigt.
5. Bei **Konnektoren** den gerade hinzugefügten `Ida-Telegram`-Connector
   auswählen.
6. Routine speichern.
7. Die Routine-ID aus der URL ablesen, die claude.ai beim Bearbeiten der
   Routine anzeigt (`claude.ai/code/routines/trig_...` -- der Teil ab `trig_`
   ist die ID), und zusammen mit dem Token in `.env` eintragen:

```bash
ROUTINE_ID=<trig_...>
ROUTINE_API_KEY=<der-notierte-api-token>
```

Technischer Hintergrund: der Server ruft dafür
`POST https://api.anthropic.com/v1/claude_code/routines/<ROUTINE_ID>/fire`
auf (offizieller Endpunkt für Routinen-Trigger, siehe
[Doku](https://platform.claude.com/docs/en/api/claude-code/routines-fire)) --
`ROUTINE_ID` und `ROUTINE_API_KEY` sind alles, was dafür gebraucht wird.

8. Neu starten: `docker compose up -d`

Ab jetzt: schreibt die konfigurierte Person dem Telegram-Bot, triggert der
Container die Routine, die Routine liest die Nachricht über MCP und
antwortet über MCP zurück auf Telegram.

## Verfügbare MCP-Tools

| Tool | Zweck |
|---|---|
| `nachricht_senden(text)` | Schickt `text` an die fest konfigurierte Person |
| `neue_nachrichten_abrufen()` | Gibt zurück, was den aktuellen Routine-Lauf ausgelöst hat (jeweils nur einmal): Text als String, Fotos als echten Bildinhalt, Bildunterschriften als eigener Text, Sprachnachrichten nur als Hinweistext (keine Transkription) |
| `bot_status()` | Prüft nur, ob Token/Bot erreichbar sind (sendet nichts) |

Persistentes Gedächtnis (über einzelne Routine-Läufe hinweg, gemeinsam
nutzbar von mehreren KIs/Connectors) liegt bewusst **nicht** hier, sondern
im separaten [Ida-Memory](https://github.com/L8teNever/Ida-Memory)-Projekt
-- der Routine dafür zusätzlich diesen Connector geben.

**Unterstützte Nachrichtentypen:**

| Typ | Verhalten |
|---|---|
| Text (auch formatiert, z.B. **fett**) | Wird 1:1 als Text an die Routine weitergegeben |
| Foto | Wird heruntergeladen und als echter Bildinhalt weitergegeben -- die Routine kann es tatsächlich "sehen" (Claude-Vision über MCP-Bildinhalte) |
| Sprachnachricht | Löst einen Trigger aus, aber **keine automatische Transkription** -- Claude hat keine native Audio-Eingabe. Die Routine bekommt nur einen Hinweis ("Sprachnachricht, X Sekunden") und sollte um eine Text-Nachricht bitten, falls nötig |
| Sticker, Videos, Dokumente | Werden aktuell ignoriert |

## Wie der Auto-Antwort-Loop funktioniert

Wenn `AUTOREPLY_ENABLED=true` (Standard) läuft im Container ein
Hintergrund-Thread:

1. Fragt Telegram per Long-Polling nach neuen Nachrichten der konfigurierten
   Person (`TELEGRAM_CHAT_ID`) -- Nachrichten von anderen werden ignoriert.
2. Kommen mehrere Nachrichten schnell hintereinander, wartet der Server
   `AUTOREPLY_DEBOUNCE_SECONDS` auf weiteren Nachschub und bündelt alles --
   die Routine wird dann **einmal** getriggert statt einmal pro Nachricht.
3. Schickt einen `POST` mit `Authorization: Bearer $ROUTINE_API_KEY` an den
   Routinen-Endpunkt (`ROUTINE_ID` in der URL) -- der gebündelte Text geht als
   `text`-Feld direkt mit (sofortiger Kontext für die Routine), zusätzlich
   liefert `neue_nachrichten_abrufen` denselben Text noch einmal ab, falls
   die Routine ihn lieber über MCP nachlesen will.

Kein Doppelt-Antworten: Telegrams `getUpdates`-Offset-Mechanismus sorgt von
selbst dafür, dass jede Nachricht genau einmal in den Zwischenspeicher
wandert, auch nach einem Neustart des Containers; `neue_nachrichten_abrufen`
liefert jede Nachricht ebenfalls nur einmal aus.

**Kosten:** Jeder Routine-Lauf verbraucht claude.ai-Nutzung auf deinem
Account (Cloud-Agent-Sitzung), nicht eine separate API-Rechnung.

## Lokal testen ohne Cloudflare

```bash
docker compose up -d
curl -H "Authorization: Bearer $MCP_AUTH_TOKEN" http://127.0.0.1:4567/healthz
```

## Troubleshooting

- **Container startet nicht**: `docker compose logs` -- meist fehlt eine
  Pflicht-Variable in `.env` (z.B. `ROUTINE_ID`/`ROUTINE_API_KEY`
  fehlen, obwohl `AUTOREPLY_ENABLED=true` ist).
- **`Telegram-API-Fehler: chat not found`**: Die Zielperson hat dem Bot noch
  nie geschrieben (siehe Schritt 1.3), oder die `chat_id` ist falsch.
- **`Telegram-API-Fehler: Unauthorized`**: `TELEGRAM_BOT_TOKEN` falsch/abgelaufen.
- **Claude bekommt 401**: Token in Client-Konfiguration und `.env` vergleichen.
- **Routine wird nicht getriggert**: `docker compose logs -f` prüfen -- Zeile
  "Telegram-Autoreply-Loop gestartet" sollte beim Start erscheinen, und bei
  neuer Nachricht "Neue Nachricht(en) erhalten, triggere Routine...". Bei
  einem HTTP-Fehler danach: `ROUTINE_ID`/`ROUTINE_API_KEY` prüfen (401 =
  Token falsch/gehört nicht zu dieser Routine, 404 = `ROUTINE_ID` falsch).
- **Routine läuft, antwortet aber nicht**: In claude.ai unter Routinen die
  letzte Sitzung öffnen und den Verlauf prüfen -- meist fehlt der
  Ida-Telegram-Connector bei den Konnektoren der Routine, oder
  `neue_nachrichten_abrufen` liefert eine leere Liste (Race Condition sehr
  unwahrscheinlich, aber möglich bei extrem kurzem `AUTOREPLY_DEBOUNCE_SECONDS`).
- **`Conflict: terminated by other getUpdates request`**: Der Bot-Token wird
  gleichzeitig noch woanders per `getUpdates` abgefragt (oder es ist ein
  Webhook für den Bot gesetzt) -- ein Telegram-Bot-Token kann immer nur von
  einem Prozess gleichzeitig per Long-Polling abgefragt werden.
