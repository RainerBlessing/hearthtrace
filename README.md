# HearthTrace

A native Linux Hearthstone tracker focused on replay analysis and learning
from your games.

Most Hearthstone deck trackers exist to answer "what's left in my deck?"
while you're playing. HearthTrace cares about a different, slower question:
after the match is over, what actually happened, turn by turn, and why did
it go the way it did? It reconstructs every turn from your `Power.log` --
board state, mana, hand, attack readiness, generated cards -- into a
step-by-step Replay view (Start → Actions → End) you can page through, and
exports a clean, deterministic Markdown summary of the whole match for
further analysis (e.g. pasting into an AI chat) or your own tooling.

It's aimed at:
- **Linux Hearthstone players** who'd rather not run HDT under Wine/Proton
  or install Overwolf.
- **Players who want to actually get better**, not just see a decklist
  while playing -- reviewing a reconstructed match has already surfaced
  real, concrete mistakes (unused attacks, bad Secret sequencing, AoE
  timed a turn late) and even a few bugs in the tracker itself, each time
  traceable back to the exact turn.
- **Developers and power users** who want an open, `Power.log`-based match
  dataset/export for their own analysis, statistics, or tooling, rather
  than a closed companion app.

MVP scope: your own side of the match only (no opponent deck tracking, no
Arena/Battlegrounds/Tavern Brawl). The app's own UI text is German.

## What it does

- **Live tracking**: your remaining deck, updated as you draw and play.
- **Replay**: page through every turn of a finished match with three
  stages per turn -- **Start** (the full decision-relevant state: mana,
  hero health/armor, your hand, both boards with attack/health and
  keywords like Taunt/Divine Shield), **Actions** (a numbered event list:
  cards and hero powers played with mana cost and spell target, attacks
  with their result, mid-turn draws, generated cards with their source,
  your own Discover picks), and **End** (mana/health/board only, since
  hand/armor changes already showed up in Actions). Same-name minions get
  a stable `#N` tag so you can tell copies apart across the whole match;
  a transformed minion (Polymorph, Hex, ...) keeps its combat history
  (summoning sickness, an already-used attack) across the transform,
  showing both its old and new identity. Hover or click a card for its
  full text, cost, stats, and keywords.
- **History**: every past match, one line each, linked back to its
  Replay -- click a row to reopen that exact match, even if newer matches
  have been played since.
- **Markdown export**: written automatically once a match ends, one file
  per match, human-readable and ready to paste into a chat or feed to
  your own scripts.

## Requirements

- Hearthstone running under Wine (e.g. via Lutris) with `Power.log`
  enabled (`log.config`, `[Power]` section: `FilePrinting=True`,
  `Verbose=True`). The app checks this at startup and, if Hearthstone's
  own 10MB log size limit is still active, offers to disable it for you
  (a full log is needed to reconstruct a whole match -- once Hearthstone
  hits that limit it stops writing to the file entirely, silently losing
  the rest of whatever match was in progress).
- A Python environment with the project's dependencies installed:
  ```bash
  cd hearthtrace
  python -m venv .venv
  .venv/bin/pip install -e ".[dev]"
  ```

## Configuration

On first launch, the tracker writes a template to
`~/.config/hearthtrace/config.toml` and exits with a note to fill it in:

```toml
logs_dir = "/path/to/your/Logs/folder"
export_dir = "~/HearthstoneAnalysis"
```

- `logs_dir`: the folder containing the `Hearthstone_*` session
  subfolders (can live on any Wine drive/mountpoint).
- `export_dir`: where the Markdown match summaries get written.

## Usage

1. **Start Hearthstone** as usual (e.g. via Lutris).
2. **Start HearthTrace** -- either directly:
   ```bash
   cd hearthtrace
   .venv/bin/hearthtrace
   ```
   or install it as a desktop entry so it shows up in your app launcher
   (edit the `Exec=` path in `hearthtrace.desktop` to your own install
   path first):
   ```bash
   cp hearthtrace.desktop ~/.local/share/applications/
   ```
   The order (Hearthstone first or the tracker first) doesn't matter --
   the tracker polls the log folder and picks up the current session
   automatically.
3. **Just play.** Once a match ends (win, loss, tie, or concede), the
   tracker detects it automatically from the log's `PLAYSTATE` tag.
4. **Result:** a file appears automatically -- no notification -- under
   `export_dir`:
   ```
   ~/HearthstoneAnalysis/<date>_<time>_<RESULT>.md
   ```
   e.g. `2026-09-07_20-15-30_WON.md`.

**Note:** the tracker needs to be running during the match, since it
reads live from the log -- exporting after the fact from an old
`Power.log` isn't currently supported by the app itself.

## Development

```bash
.venv/bin/pytest --cov=src/hearthtrace --cov-report=term-missing
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/radon cc src -a -nb
```
