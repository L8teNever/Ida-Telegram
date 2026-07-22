# Ida-Telegram

Ein eigenständiger MCP-Server (Model Context Protocol), getrennt von
Ida-Untis und ohne Verbindung dazu. Gibt Claude genau ein Werkzeug: einer
fest konfigurierten Person eine Telegram-Nachricht schicken. Läuft als
Docker-Container und wird über einen bestehenden Cloudflare Tunnel unter
einer eigenen Domain erreichbar gemacht.

## Architektur

```
Claude  --https-->  Cloudflare Tunnel (öffentliche Domain)
                            |
                            v
                 127.0.0.1:8001 auf deinem Server
                            |
                            v
              Docker-Container "ida-telegram-mcp"
                            |
                            v
                    Telegram Bot HTTP-API
```

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

## 2. Einrichten

```bash
git clone https://github.com/<dein-user>/Ida-Telegram.git
cd Ida-Telegram
cp .env.example .env
```

`.env` ausfüllen:

| Variable | Bedeutung |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token von @BotFather |
| `TELEGRAM_CHAT_ID` | Die eine feste Ziel-Person (siehe Schritt 1) |
| `MCP_AUTH_TOKEN` | Langes Zufalls-Token, das Claude beim Verbinden mitschicken muss. Erzeugen mit `openssl rand -hex 32` |
| `MCP_PORT` | Lokaler Port (Standard `8001`, damit es neben Ida-Untis auf 8000 laufen kann) |
| `GITHUB_OWNER` | Dein GitHub-Benutzername in Kleinbuchstaben (für das Image aus GHCR) |

## 3. Image bauen lassen (GitHub Actions)

Bei jedem Push auf `main` baut `.github/workflows/docker-publish.yml` das
Image automatisch und veröffentlicht es nach
`ghcr.io/<dein-user>/ida-telegram:latest`.

Damit `docker compose` es ohne Login ziehen kann, einmalig auf öffentlich
stellen: GitHub -> Profil -> **Packages** -> `ida-telegram` -> Package
settings -> Change visibility -> Public. (Oder `docker login ghcr.io` auf
dem Server, falls privat bleiben soll.)

## 4. Starten

```bash
docker compose pull
docker compose up -d
docker compose logs -f
```

## 5. An den bestehenden Cloudflare Tunnel anbinden

Analog zu Ida-Untis, nur mit eigenem Hostname und Port 8001:

```yaml
ingress:
  - hostname: telegram.deine-domain.de
    service: http://localhost:8001
  - service: http_status:404
```

(Bzw. im Zero-Trust-Dashboard unter Public Hostname eintragen.) Danach
`cloudflared` neu laden.

## 6. Mit Claude verbinden

Endpunkt: `https://telegram.deine-domain.de/mcp` (Streamable HTTP). Token via:

- Header `Authorization: Bearer <MCP_AUTH_TOKEN>`
- Header `X-API-Key: <MCP_AUTH_TOKEN>`
- Query-Parameter `?token=<MCP_AUTH_TOKEN>` (falls der Client keinen Header
  konfigurieren lässt, z.B. manche Custom-Connector-UIs)

**Claude Code CLI:**

```bash
claude mcp add --transport http ida-telegram \
  https://telegram.deine-domain.de/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```

**claude.ai / Claude Desktop (Custom Connector):** Einstellungen ->
Connectors -> Add custom connector -> als URL
`https://telegram.deine-domain.de/mcp?token=<MCP_AUTH_TOKEN>` eintragen,
OAuth-Felder leer lassen.

## Verfügbare Tools

| Tool | Zweck |
|---|---|
| `nachricht_senden(text)` | Schickt `text` an die fest konfigurierte Person |
| `bot_status()` | Prüft nur, ob Token/Bot erreichbar sind (sendet nichts) |

## Lokal testen ohne Cloudflare

```bash
docker compose up -d
curl -H "Authorization: Bearer $MCP_AUTH_TOKEN" http://127.0.0.1:8001/healthz
```

## Troubleshooting

- **Container startet nicht**: `docker compose logs` -- meist fehlt eine
  Pflicht-Variable in `.env`.
- **`Telegram-API-Fehler: chat not found`**: Die Zielperson hat dem Bot noch
  nie geschrieben (siehe Schritt 1.3), oder die `chat_id` ist falsch.
- **`Telegram-API-Fehler: Unauthorized`**: `TELEGRAM_BOT_TOKEN` falsch/abgelaufen.
- **Claude bekommt 401**: Token in Client-Konfiguration und `.env` vergleichen.
