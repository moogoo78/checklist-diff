# ChecklistDiff

Track taxonomic checklists across releases, and trace what happened to a
scientific name.

Checklists republish on a schedule, and every release quietly rewrites names: a
species is sunk into synonymy, a genus is split, an author string is corrected, a
record simply disappears. If your specimen database or occurrence records
reference those names, ChecklistDiff answers *what happened to this name since I
recorded it?*

It ingests successive **releases** of **many checklists**, keeps every release
intact, computes a typed **change set** between consecutive releases, and traces
one name across its whole recorded life.

**Scope:** local, regional, and specialist checklists — the kind published as a
Darwin Core Archive or a spreadsheet by a small team. Not GBIF Backbone or
Catalogue of Life; GlobalNames already covers those, and matching against a global
backbone is a different problem from tracking a checklist's own revision history.

## Quick start

```bash
docker compose build
docker compose up -d
docker compose exec app alembic upgrade head
docker compose exec app ckdiff health
```

Drop source files in `./checklists/` — they appear inside the container at
`/checklists`.

```bash
docker compose exec app ckdiff checklist add \
    --code mylist --title "My Regional Checklist"
docker compose exec app ckdiff ingest \
    --checklist mylist --version 2023.1 \
    --file /checklists/v2023.csv --map /checklists/map.yml
docker compose exec app ckdiff ingest \
    --checklist mylist --version 2024.1 \
    --file /checklists/v2024.csv --map /checklists/map.yml

# Is the publisher's taxon ID trustworthy across releases?
docker compose exec app ckdiff checklist check-ids \
    --code mylist --from 2023.1 --to 2024.1

docker compose exec app ckdiff diff run \
    --checklist mylist --from 2023.1 --to 2024.1
docker compose exec app ckdiff trace "Zanthoxylum ailanthoides"
```

The web UI is at <http://localhost:8087>.

### TaiCOL

TaiCOL's *name* export gets a dedicated reader (`--reader taicol-name`), because
three of its conventions cannot be expressed as a field map: a name placed in
several taxa carries parallel comma-joined `usage_status` and `taxon_id` lists
and has to become several usages; synonyms link to their accepted name only
indirectly through a shared `taxon_id`; and `usage_status` is blank for names
that are catalogued but unplaced.

Drop the exports in `./checklists/` as `TaiCOL_name_YYYYMMDD.zip` and run:

```bash
./scripts/import_taicol.sh
```

It registers the checklist, ingests every export it finds — reading the version
and release date from each filename — then runs `check-ids` and diffs the oldest
against the newest. Re-run with `--replace` to reload releases already ingested.

Prefer the *name* export over the *taxon* export: the latter packs synonyms into
a comma-joined column with their authorship stripped, which both loses author
strings and risks collapsing homonyms into a single `Name` row.

## Where the data lives

`./data/checklistdiff.sqlite` — a bind mount, so it is an ordinary file you own.
Open it with any SQLite tool on the host, no Docker involved:

```bash
sqlite3 data/checklistdiff.sqlite
datasette data/checklistdiff.sqlite     # if you have it
cp data/checklistdiff.sqlite backup.sqlite
```

It is gitignored. The container runs as uid 1000 to match a typical host user; if
yours differs, start with `CKDIFF_UID=$(id -u) CKDIFF_GID=$(id -g) docker compose up -d`.

One caution, and it is not hypothetical — it bit during development. SQLite has no
server arbitrating access: every client manipulates the file directly. A second
*container* mounted on this directory as root can clobber the file and lock the
app out of its own database. Reading from the host is fine; pointing another
container at `./data` is not worth the risk.

Because the database runs in WAL mode, it also cannot be opened from a *read-only*
mount at all — SQLite needs to create the `-shm` sidecar. So "mount it read-only
for safety" is not an available mitigation. If you want a genuinely safe copy to
hand to a tool or a colleague, checkpoint and copy:

```bash
docker compose exec app python -c \
  "from checklistdiff.db import get_engine; from sqlalchemy import text; \
   get_engine().connect().execute(text('PRAGMA wal_checkpoint(TRUNCATE)'))"
cp data/checklistdiff.sqlite snapshot.sqlite
```

## How it works

Three layers, kept deliberately separate:

| Layer | Table | What it holds |
| --- | --- | --- |
| Nomenclatural | `name` | One row per distinct scientific name in the whole database, deduped on a normalised key. |
| Assertion | `usage` | What one release says about one name — status, accepted name, parent. Append-only. |
| Continuity | `track` | The thread joining "the same taxon" across releases of one checklist. |

Because every checklist's usages point at the same `name` row, comparing
checklists is a single join rather than a matching step.

`usage` rows are never updated after ingest. That immutability is what makes the
reconstructed history trustworthy — `change` rows are derived and can always be
recomputed with `ckdiff diff run --force`.

## What it detects

| Change | Meaning |
| --- | --- |
| `added` / `removed` | A taxon appeared or disappeared. |
| `renamed` | Canonical name changed — only detectable under ID anchoring. |
| `author_changed` | Same name, different authorship. Usually a correction, not a taxonomic act. |
| `status_changed` | e.g. accepted → synonym. |
| `accepted_changed` | A synonym now points at a **different** accepted name. |
| `reclassified` | Moved to a different parent. |
| `rank_changed` | e.g. species → subspecies. |
| `lumped` | Several accepted taxa collapsed into one. |
| `split` | One accepted taxon became several. |
| `id_replaced` | Same name, new taxonID — bookkeeping, not taxonomy. |
| `probable_rename` | *Advisory.* A fuzzy guess that an add/remove pair is really a rename. |

`accepted_changed` is usually the one that matters most: it silently
re-identifies records that referenced the synonym.

`lumped` and `split` are why this is not `diff a.csv b.csv`. Neither is visible
in any single row — they live in the relationship between rows, and they are
normally the changes a taxonomist actually wants to read about.

`probable_rename` is the only inferred output. It is scored, marked advisory, and
never suppresses the underlying `added`/`removed` rows — a fuzzy guess must not
erase the facts it is guessing about.

## Two keys, not one

Every name is stored once, deduped on a normalised key. There are actually two:

- **`norm_key`** = canonical name + authorship → identity of a `Name` row.
  Authorship must be included, or *Aotus* Endl. (a plant) and *Aotus* Illiger,
  1811 (a monkey) would collapse into one row.
- **`canonical_key`** = canonical name alone → what tracks anchor on.

The split matters: if tracks anchored on `norm_key`, a release that merely
corrected an author string would look like a deletion plus an addition. Anchoring
on `canonical_key` keeps the thread intact and reports an honest
`author_changed`.

## Anchoring: the setting that matters most

To diff two releases, the engine must decide which record in release N+1 *is* a
given record from release N. Two strategies:

- **`persistent`** — anchor on the publisher's `taxonID`.
- **`unstable` / `none`** — anchor on the name itself.

Small checklists are often re-exported from a spreadsheet, which renumbers rows.
Anchoring on IDs that shuffle produces a diff of pure noise — thousands of
spurious adds and removes — so the default is the pessimistic `unstable`.

Don't guess. `ckdiff checklist check-ids` reports what fraction of IDs actually
survive between two releases, so the setting is an observation.

The trade-off is real in both directions: name anchoring cannot detect a rename
(the name *is* the anchor), so renames surface as an add/remove pair, partly
recovered as advisory `probable_rename` changes.

## Development

```bash
docker compose exec app python -m pytest
docker compose exec app alembic revision --autogenerate -m "message"
```

Requirements: Docker. Nothing is installed on the host.
