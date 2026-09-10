# HearthTrace

**A native Linux Hearthstone tracker for replay analysis and learning from
your games.**

HearthTrace reconstructs Hearthstone matches from `Power.log` and lets you
review them turn by turn:

**Start → Actions → End**

See the board, hand, mana, generated cards, attacks and transformations
exactly as they happened, then export the complete match as deterministic
Markdown for further analysis.

> 🚧 **Early development.** Live deck tracking currently covers your own
> deck only; the replay reconstructs visible board state and actions for
> both players. Opponent deck tracking, Arena, Battlegrounds and Tavern
> Brawl are not yet supported. The app's own UI text (and its Markdown
> export) is German.

<!-- TODO: screenshot or short GIF of the Replay view goes here -->

## Why HearthTrace?

Most deck trackers answer one question during the game:

> What's left in my deck?

HearthTrace also focuses on the question after the game:

> What happened, turn by turn, and which decisions mattered?

## Who it's for

- **Linux Hearthstone players** who'd rather not run HDT under Wine/Proton
  or install Overwolf.
- **Players who want to improve their play** by reviewing individual
  decisions, missed attacks, Secret sequencing, removal timing and board
  development.
- **Developers and power users** who want an open, `Power.log`-based match
  dataset/export for their own analysis, statistics, or tooling, rather
  than a closed companion app.

## Features

### Live deck tracking
See the cards remaining in your deck as you draw and play.

### Turn-by-turn replay
Every turn is reconstructed in three stages:
- **Start** -- mana, health, armor, hand and both boards
- **Actions** -- plays, attacks, draws, generated cards and Discover choices
- **End** -- resulting board and resource state

### Accurate entity tracking
Same-name minions get a stable `#N` tag so you can tell copies apart across
the whole match. A transformed minion (Polymorph, Hex, ...) keeps its
underlying combat history -- summoning sickness, an already-used attack --
across the transform, instead of being treated as a freshly summoned one.

### Card details
Hover or click a card for its full text, cost, stats and keywords.

### Match history
Every past match, linked back to its replay -- reopen it at any time, even
if newer matches have been played since.

### Markdown export
Finished matches are exported automatically, one file per match, as
deterministic, human-readable Markdown -- ready to paste into an AI chat
for analysis or feed to your own scripts.

## Example replay

Real output from HearthTrace's Markdown export (trimmed for length, not
translated -- this is exactly what the app writes, German text included):

```text
## Zug 4 – Du

### Start
Mana: 2/2 | Gesperrt: 0 | Überladen: 0
Heldenleben: Du 30 | Gegner 30
Waffe: Du (keine) | Gegner (keine)
Rüstung: Du 0 | Gegner 0

Board (Du):
- Hexe in Ausbildung #1 (0/1, Spott)

Board (Gegner):
- (leer)

### Aktionen
1. Du: Himmelswallwächter #2 gespielt (Mana: 2 → 0)
   → Hexe in Ausbildung #1: 0/1 → 1/1
   → Soldat von Al’Akir #4 beschworen
2. Du: Hexe in Ausbildung #1 (1 Angriff) → Gegnerischer Held: 30 → 30
   → Hexe in Ausbildung #1: 1/1 → 1/-1
   → Secret ausgelöst: Sprengfalle
   → Dein Held: 30 → 28
   → Himmelswallwächter #2: 1/2 → 1/0
   → Soldat von Al’Akir #4: 1/2 → 1/0
   → Hexe in Ausbildung #1 stirbt
   → Himmelswallwächter #2 stirbt
   → Soldat von Al’Akir #4 stirbt

### Ende
Mana: 0/2 | Gesperrt: 0 | Überladen: 0
Heldenleben: Du 28 | Gegner 30
```

A routine 1-attack trade triggered an Explosive Trap and wiped the entire
board -- exactly the kind of moment worth reviewing turn by turn, instead of
just remembering "I lost my board".

## Privacy

HearthTrace runs entirely locally: it reads `Power.log` from disk, keeps
match history and replay data on your machine, and writes Markdown exports
to a folder you choose. No account, no cloud service, no telemetry.

## Known limitations

- No opponent deck tracking (only visible board state, not an inferred
  remaining opponent deck).
- No Arena, Battlegrounds or Tavern Brawl support.
- No import of old `Power.log` files -- HearthTrace needs to be running
  during the match, since it reads the log live.

## Roadmap

Small, concrete next steps:
- Replay review hints (e.g. flagging unused attacks left at turn end)
- Board threat / lethal indicators
- Selected-turn export (share one decision, not the whole match)

## Quick Start

1. Enable Hearthstone's `Power.log` -- see [Hearthstone log
   configuration](#hearthstone-log-configuration) below.
2. Install HearthTrace -- see [Installation](#installation) below.
3. Start Hearthstone, in any order relative to HearthTrace.
4. Play normally. HearthTrace detects the active log session and records
   matches automatically.

## Installation

```bash
git clone https://github.com/rainerblessing/hearthtrace.git
cd hearthtrace
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/hearthtrace
```

Or install it as a desktop entry so it shows up in your app launcher (edit
the `Exec=` path in `hearthtrace.desktop` to your own install path first):
```bash
cp hearthtrace.desktop ~/.local/share/applications/
```

### Hearthstone log configuration

HearthTrace reads Hearthstone's own debug log, not a hooked/overlay
integration -- two separate config files, both inside Hearthstone's `Logs`
folder:

- `log.config`, `[Power]` section -- makes Hearthstone write `Power.log` in
  the first place, and with enough detail to reconstruct a match:
  ```ini
  [Power]
  FilePrinting=true
  Verbose=true
  ```
- `client.config`, `[Log]` section -- Hearthstone caps `Power.log` at 10MB
  by default; once hit, it stops writing to the file *entirely* for the
  rest of that session, silently losing the rest of whatever match is in
  progress:
  ```ini
  [Log]
  FileSizeLimit.Int=-1
  ```

HearthTrace checks both at startup and offers to fix them for you if
they're missing or the size limit is still active.

## Configuration

On first launch, HearthTrace writes a template to
`~/.config/hearthtrace/config.toml` and exits with a note to fill it in:

```toml
logs_dir = "/path/to/your/Logs/folder"
export_dir = "~/HearthstoneAnalysis"
```

- `logs_dir`: the folder containing the `Hearthstone_*` session
  subfolders (can live on any Wine drive/mountpoint).
- `export_dir`: where the Markdown match summaries get written.

## Usage details

Once a match ends (win, loss, tie, or concede), HearthTrace detects it
automatically from the log's `PLAYSTATE` tag and writes a file -- no
notification -- under `export_dir`:
```
~/HearthstoneAnalysis/<date>_<time>_<RESULT>.md
```
e.g. `2026-09-07_20-15-30_WON.md`.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest --cov=src/hearthtrace --cov-report=term-missing
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/radon cc src -a -nb
```
