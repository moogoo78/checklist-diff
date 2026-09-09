#!/usr/bin/env bash
#
# Import every TaiCOL name export sitting in ./checklists into the database.
#
#   ./scripts/import_taicol.sh              # ingest, then check IDs and diff
#   ./scripts/import_taicol.sh --replace    # re-ingest releases already loaded
#
# Drop the exports in ./checklists as TaiCOL_name_YYYYMMDD.zip (or .csv) — that
# directory is bind-mounted into the container at /checklists. The release
# version and date are read from the filename, so nothing here needs editing
# when the next export is published.
#
# This runs on the host and drives the container; it installs nothing locally.

set -euo pipefail

cd "$(dirname "$0")/.."

CODE=taicol
COMPOSE=(docker compose)
EXEC=("${COMPOSE[@]}" exec -T app)

REPLACE=""
[[ "${1:-}" == "--replace" ]] && REPLACE="--replace"

if ! "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx app; then
    echo "starting app container..."
    "${COMPOSE[@]}" up -d
fi

# Safe to re-run: the schema is versioned, so this is a no-op once current.
"${EXEC[@]}" alembic upgrade head

# `checklist add` refuses a code that is already taken, which is the normal case
# on every re-run. Tolerate exactly that failure, and let any other one stop the
# script — swallowing all of them would hide a real misconfiguration.
if add_output=$("${EXEC[@]}" ckdiff checklist add \
        --code "$CODE" \
        --title "TaiCOL — Taiwan Catalogue of Life (name export)" \
        --publisher "TaiBIF / Biodiversity Research Center, Academia Sinica" \
        --homepage "https://taicol.tw/" \
        --license "CC BY 4.0" \
        --id-stability unstable 2>&1); then
    echo "$add_output"
elif grep -q "already exists" <<<"$add_output"; then
    echo "checklist '$CODE' already registered"
else
    echo "$add_output" >&2
    exit 1
fi

shopt -s nullglob
files=(checklists/TaiCOL_name_*.zip checklists/TaiCOL_name_*.csv)
shopt -u nullglob

if [[ ${#files[@]} -eq 0 ]]; then
    echo "no TaiCOL_name_* exports in ./checklists" >&2
    exit 1
fi

versions=()
for path in $(printf '%s\n' "${files[@]}" | sort); do
    base=$(basename "$path")

    # TaiCOL_name_20260625.zip -> version 2026.06.25, released_on 2026-06-25
    if [[ ! "$base" =~ ([0-9]{4})([0-9]{2})([0-9]{2}) ]]; then
        echo "skipping $base: no YYYYMMDD in filename" >&2
        continue
    fi
    y=${BASH_REMATCH[1]} m=${BASH_REMATCH[2]} d=${BASH_REMATCH[3]}
    version="${y}.${m}.${d}"
    versions+=("$version")

    echo
    echo "=== ingesting $base as $CODE/$version ==="
    # Already-ingested releases are skipped rather than treated as an error, so
    # adding one older export to a loaded database does not mean reloading the
    # others. `--replace` is the way to ask for that deliberately.
    if out=$("${EXEC[@]}" ckdiff ingest \
            --checklist "$CODE" \
            --version "$version" \
            --file "/checklists/$base" \
            --reader taicol-name \
            --released-on "${y}-${m}-${d}" \
            $REPLACE 2>&1); then
        echo "$out"
    elif grep -q "already ingested" <<<"$out"; then
        echo "already loaded, skipping (use --replace to reload)"
    else
        echo "$out" >&2
        exit 1
    fi
done

if [[ ${#versions[@]} -lt 2 ]]; then
    echo
    echo "only ${#versions[@]} release(s) loaded — need two to diff."
    exit 0
fi

# Diff *consecutive* pairs, not oldest against newest: a change set is defined
# between successive releases, and a taxon that was sunk and then restored would
# vanish entirely from an endpoints-only comparison.
for (( i = 0; i < ${#versions[@]} - 1; i++ )); do
    from=${versions[i]}
    to=${versions[i + 1]}

    echo
    echo "=== ID stability: $from -> $to ==="
    # Run before the diff: it reports what fraction of name_ids survive between
    # releases, which is what decides the anchoring strategy.
    "${EXEC[@]}" ckdiff checklist check-ids --code "$CODE" --from "$from" --to "$to"

    echo
    echo "=== diff: $from -> $to ==="
    "${EXEC[@]}" ckdiff diff run --checklist "$CODE" \
        --from "$from" --to "$to" ${REPLACE:+--force}
done

echo
echo "done. Browse at http://localhost:${CKDIFF_PORT:-8087}, or:"
echo "  docker compose exec app ckdiff diff show --checklist $CODE \\"
echo "      --from ${versions[0]} --to ${versions[1]}"
