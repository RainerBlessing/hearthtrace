# HS Tracker

Nativer Linux-Begleiter für Hearthstone (gespielt über Wine/Lutris). Zeigt
live das eigene Restdeck an und exportiert nach jedem Match automatisch eine
Markdown-Zusammenfassung zur späteren Analyse mit einem KI-Chat.

MVP-Umfang: nur das eigene Deck (kein Opponent-Tracking, kein
Arena/Battlegrounds/Tavern Brawl).

## Voraussetzungen

- Hearthstone läuft unter Wine (z. B. via Lutris) und schreibt ein
  `Power.log` (siehe `docs/plans/2026-09-06-hs-tracker-design.md`,
  Abschnitt "Log-Zugriff unter Wine", falls das noch nicht eingerichtet ist).
- Python-Umgebung mit den Projektabhängigkeiten installiert:
  ```bash
  cd /home/rainer/projects/hs-tracker
  python -m venv .venv
  .venv/bin/pip install -e ".[dev]"
  ```

## Konfiguration

Beim ersten Start legt der Tracker automatisch eine Vorlage unter
`~/.config/hs-tracker/config.toml` an und beendet sich mit einem Hinweis,
den Pfad einzutragen:

```toml
logs_dir = "/pfad/zum/Logs-ordner"
export_dir = "~/HearthstoneAnalysis"
```

- `logs_dir`: der Ordner, der die `Hearthstone_*`-Session-Unterordner
  enthält (kann auf einem beliebigen Wine-Laufwerk/Mountpoint liegen).
- `export_dir`: wohin die Markdown-Zusammenfassungen geschrieben werden.

## Markdown-Zusammenfassungen generieren lassen

1. **Hearthstone starten** (wie gewohnt über Lutris).
2. **HS Tracker starten** — entweder direkt:
   ```bash
   cd /home/rainer/projects/hs-tracker
   .venv/bin/python -m hs_tracker.app
   ```
   oder als Desktop-Eintrag installieren, damit er im App-Launcher
   auftaucht:
   ```bash
   cp hs-tracker.desktop ~/.local/share/applications/
   ```
   Die Reihenfolge (Hearthstone zuerst oder Tracker zuerst) ist egal — der
   Tracker pollt den Log-Ordner und findet die aktuelle Session automatisch.
3. **Einfach spielen.** Sobald ein Match endet (Sieg, Niederlage,
   Unentschieden oder Aufgabe), erkennt der Tracker das automatisch am
   `PLAYSTATE`-Tag im Log.
4. **Ergebnis:** Eine Datei landet automatisch — ohne Benachrichtigung —
   unter `export_dir`:
   ```
   ~/HearthstoneAnalysis/<Datum>_<Uhrzeit>_<ERGEBNIS>.md
   ```
   z. B. `2026-09-07_20-15-30_WON.md`. Diese Datei kann direkt in einen
   Claude-Chat eingefügt werden, um die Partie analysieren zu lassen.
   Enthalten sind Ergebnis, Klassen, Deck, Mulligan (behalten/zurückgelegt),
   Zugverlauf und Restdeck bei Spielende.

**Hinweis:** Der Tracker muss während des Matches laufen, da live aus dem
Log gelesen wird — ein nachträglicher Export aus einem alten `Power.log`
ist über die App aktuell nicht vorgesehen.

## Entwicklung

```bash
.venv/bin/pytest --cov=src/hs_tracker --cov-report=term-missing
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/radon cc src -a -nb
```

Design und Implementierungsplan liegen unter `docs/plans/`.
