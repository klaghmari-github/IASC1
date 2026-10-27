#!/usr/bin/env bash
# Accepte les invitations GitHub en attente.
# Le détail de chaque acceptation est ajouté à invitations_acceptees.csv.
set -euo pipefail

repertoire="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 est introuvable." >&2
  exit 1
fi

exec python3 "$repertoire/accept.py" "$@"
