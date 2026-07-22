"""Persistenter, mehrteiliger Notiz-Speicher fuer die Routine -- nach Themen
in einzelne Markdown-Dateien aufgeteilt (wie ein kleines Wiki) statt einer
einzigen, immer komplett gelesenen Datei.

Warum: Claude Code Routinen sind zustandslos -- jeder Trigger startet eine
neue, leere Sitzung ohne Kenntnis frueherer Laeufe. Ein einzelnes Gedaechtnis-
File wuerde mit der Zeit wachsen und bei jedem Lauf komplett gelesen unnoetig
viele Tokens kosten. Stattdessen: eine kompakte Uebersicht (eine Zeile pro
Themen-Datei) wird immer zuerst gelesen; die Routine entscheidet anhand davon
selbst, welche einzelne(n) Datei(en) fuer die aktuelle Nachricht ueberhaupt
relevant sind, und liest gezielt nur die.

Format: Die erste Zeile jeder .md-Datei ist ihre Kurzbeschreibung (taucht in
der Uebersicht auf), der Rest ist frei nutzbarer Inhalt. Dateien koennen sich
per [[anderer-dateiname]] aufeinander beziehen -- das ist reine Text-
Konvention fuer die Routine selbst, es gibt dafuer keine technische
Verlinkung/Aufloesung in diesem Store.

Neue Themen brauchen keine Strukturaenderung: ein neuer Dateiname taucht
automatisch in der naechsten Uebersicht auf, sobald er einmal beschrieben
wurde.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

_MAX_FILE_LENGTH = 20_000
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class MemoryError(RuntimeError):
    """Fehler, die 1:1 als verständliche Meldung an die Routine zurückgehen sollen."""


class MemoryStore:
    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)
        self._lock = threading.Lock()
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, name: str) -> Path:
        if not _NAME_PATTERN.match(name):
            raise MemoryError(
                f"Ungueltiger Dateiname '{name}': nur Buchstaben, Zahlen, "
                "'_' und '-' erlaubt (max. 64 Zeichen), keine Dateiendung "
                "und keine Pfadtrenner angeben -- z.B. 'person' statt "
                "'person.md' oder '../person'."
            )
        return self._dir / f"{name}.md"

    def overview(self) -> str:
        """Eine Zeile pro vorhandener Themen-Datei: Name + ihre erste Zeile
        als Kurzbeschreibung. Das ist bewusst die einzige Sicht, die immer
        alles auf einmal zeigt -- der Rest bleibt bis zum gezielten
        gedaechtnis_lesen() ungelesen."""
        with self._lock:
            files = sorted(self._dir.glob("*.md"))
            if not files:
                return "(Noch keine Themen-Dateien vorhanden.)"
            lines = []
            for f in files:
                erste_zeile = next(iter(f.read_text(encoding="utf-8").splitlines()), "").strip()
                lines.append(f"- {f.stem}: {erste_zeile or '(leer)'}")
            return "\n".join(lines)

    def read(self, name: str) -> str:
        path = self._path_for(name)
        with self._lock:
            if not path.exists():
                return ""
            return path.read_text(encoding="utf-8")

    def write(self, name: str, content: str) -> str:
        path = self._path_for(name)
        content = content[:_MAX_FILE_LENGTH]
        with self._lock:
            path.write_text(content, encoding="utf-8")
        return content
