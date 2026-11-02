#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'HELP'
Usage: ./scripts/sync.sh [--dry-run] user@server:/path/to/hse-monitor/

Copy the Python sources, Docker deployment files and local .env with rsync over SSH.
The destination's parent directory must exist and be writable.
The local .env replaces the remote .env and is stored with permissions 600.
Data, caches and tests are not transferred; remote files are not deleted.
For a custom SSH port, set RSYNC_RSH='ssh -p 2222'.
HELP
}

rsync_options=(-avz --no-owner --no-group --itemize-changes)
case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --dry-run) rsync_options+=(--dry-run); shift ;;
esac
if [[ $# -ne 1 || -z "${1:-}" || "$1" == -* ]]; then
    usage >&2
    exit 2
fi

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
destination="${1%/}/"

if [[ ! -f "$project_dir/.env" ]]; then
    echo 'Missing local .env: create it from .env.example and fill in the settings.' >&2
    exit 1
fi

rsync "${rsync_options[@]}" \
    --include='/hse_monitor/' \
    --include='/hse_monitor/*.py' \
    --include='/Dockerfile' \
    --include='/compose.yaml' \
    --include='/requirements.txt' \
    --include='/.dockerignore' \
    --include='/.env.example' \
    --include='/README.md' \
    --exclude='*' \
    -- "$project_dir/" "$destination"

# Transfer secrets separately to restrict their permissions without changing source file modes.
# Checksum also detects same-size edits made within the same filesystem timestamp interval.
rsync "${rsync_options[@]}" --checksum --copy-links --chmod=F600 \
    -- "$project_dir/.env" "$destination"
