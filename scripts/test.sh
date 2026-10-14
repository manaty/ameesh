#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Suite de tests agent-mesh contre le conteneur Postgres local.
#
#   scripts/test.sh [motif unittest]
#
# Deux passes : pilote `psql` (toujours disponible) puis pilote `psycopg` si un
# venv local peut l'installer — les deux chemins de db.py sont ainsi couverts.
set -euo pipefail
cd "$(dirname "$0")/.."

# AMEESH_PG_UP=0 : la base est déjà là (Postgres jetable d'un job déporté, L159b)
[ "${AMEESH_PG_UP:-1}" = 0 ] || ./scripts/pg-up.sh

#: DSN sans mot de passe : le secret vient de PGPASSWORD ou de ~/.pgpass.
export AMEESH_TEST_DSN=${AMEESH_TEST_DSN:-${AGENT_MESH_TEST_DSN:-postgresql://agent_mesh@127.0.0.1:55432/agent_mesh}}
export AMEESH_REQUIRE_DB=1
# jamais la configuration de l'hôte (~/.config/ameesh/config.json) : sur un
# poste basculé, elle fixerait canon, fils et hôte sous les tests. Fichier VIDE
# mais existant : un chemin explicite absent est refusé (#1127).
AMEESH_CONFIG="$(mktemp "${TMPDIR:-/tmp}/ameesh-config-vide-XXXXXX")"
printf '{}\n' > "$AMEESH_CONFIG"
export AMEESH_CONFIG
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
# aucun réseau dans les tests : ni clé de fournisseur, ni relevé de solde (L26)
unset DEEPSEEK_API_KEY AMEESH_DEEPSEEK_API_BASE
export AMEESH_BALANCE_INTERVAL=0
PATTERN=("$@")

echo
echo "=== pilote psql ==="
AMEESH_DRIVER=psql python3 -m unittest discover -s tests -t . -v "${PATTERN[@]}"

VENV=${AMEESH_TEST_VENV:-.venv}  # L159b : venv en cache sur la VM de déport
if [ ! -x "$VENV/bin/python" ]; then
  echo "=== création de $VENV (pilote psycopg, dépendance du paquet depuis L61) ==="
  python3 -m venv "$VENV" || true
fi
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import psycopg, yaml" 2>/dev/null; then
  echo
  echo "=== pilote psycopg ==="
  AMEESH_DRIVER=psycopg "$VENV/bin/python" -m unittest discover -s tests -t . -v "${PATTERN[@]}"
elif [ -x "$VENV/bin/pip" ]; then
  echo "=== installation de psycopg dans $VENV ==="
  if "$VENV/bin/pip" install --quiet 'psycopg[binary]' cryptography pyyaml 2>/dev/null; then
    echo
    echo "=== pilote psycopg ==="
    AMEESH_DRIVER=psycopg "$VENV/bin/python" -m unittest discover -s tests -t . -v "${PATTERN[@]}"
  else
    echo "psycopg non installé (hors ligne ?) : passe psql seulement"
  fi
fi

echo
echo "tests terminés"
