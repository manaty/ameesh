#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Suite de tests agent-mesh contre le conteneur Postgres local.
#
#   scripts/test.sh [noms unittest… | -p motif.py | -k filtre]
#
# Deux passes : pilote `psql` (toujours disponible) puis pilote `psycopg` si un
# venv local peut l'installer — les deux chemins de db.py sont ainsi couverts.
#
# Le banc partagé reçoit une base jetable par exécution (#1992) ; un
# AMEESH_TEST_DSN explicite (CI, job déporté) est respecté tel quel.
set -euo pipefail
cd "$(dirname "$0")/.."

# AMEESH_PG_UP=0 : la base est déjà là (Postgres jetable d'un job déporté, L159b)
[ "${AMEESH_PG_UP:-1}" = 0 ] || ./scripts/pg-up.sh

#: DSN sans mot de passe : le secret vient de PGPASSWORD ou de ~/.pgpass.
ADMIN_DSN=${AMEESH_TEST_DSN:-${AGENT_MESH_TEST_DSN:-postgresql://agent_mesh@127.0.0.1:55432/agent_mesh}}
# #1992 : le banc local est partagé par tous les agents de l'hôte ; deux
# exécutions concurrentes sur la même base échangent leurs NOTIFY (faux rouges
# du banc : le LISTEN d'un test recevait le payload d'un autre module, constat
# impl-08 du 16/10). Une exécution qui gère elle-même le banc prend donc sa
# propre base, jetable à la sortie, quel que soit le code de retour. Un DSN
# explicite (CI, job déporté, outil) reste respecté tel quel : ces exécutions
# ont déjà leur base.
AMEESH_TEST_DB=""
# jamais la configuration de l'hôte (~/.config/ameesh/config.json) : sur un
# poste basculé, elle fixerait canon, fils et hôte sous les tests. Fichier VIDE
# mais existant : un chemin explicite absent est refusé (#1127) ; il est
# supprimé à la sortie, quel que soit le code de retour (#1145).
AMEESH_CONFIG="$(mktemp "${TMPDIR:-/tmp}/ameesh-config-vide-XXXXXX")"
export AMEESH_CONFIG
nettoyer() {
  rm -f -- "$AMEESH_CONFIG" || true
  if [ -n "$AMEESH_TEST_DB" ]; then
    psql --dbname "$ADMIN_DSN" -v ON_ERROR_STOP=1 -q \
      -c "DROP DATABASE IF EXISTS \"$AMEESH_TEST_DB\" WITH (FORCE)" >/dev/null 2>&1 || true
  fi
}
trap nettoyer EXIT
printf '{}\n' > "$AMEESH_CONFIG"
if [ "${AMEESH_PG_UP:-1}" != 0 ] && [ -z "${AMEESH_TEST_DSN:-}" ] \
   && [ -z "${AGENT_MESH_TEST_DSN:-}" ]; then
  AMEESH_TEST_DB="agent_mesh_test_$(date +%s)_$$_${RANDOM}"
  psql --dbname "$ADMIN_DSN" -v ON_ERROR_STOP=1 -q \
    -c "CREATE DATABASE \"$AMEESH_TEST_DB\""
  export AMEESH_TEST_DSN="${ADMIN_DSN%/*}/$AMEESH_TEST_DB"
else
  export AMEESH_TEST_DSN="$ADMIN_DSN"
fi
export AMEESH_REQUIRE_DB=1
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
# aucun réseau dans les tests : ni clé de fournisseur, ni relevé de solde (L26)
unset DEEPSEEK_API_KEY AMEESH_DEEPSEEK_API_BASE
export AMEESH_BALANCE_INTERVAL=0
# Les noms qualifiés se chargent directement : discover les interpréterait
# comme ses positionnels (start/pattern/top), parfois en lançant toute la suite.
# Sans nom, conserver discovery, y compris ses filtres -p et -k documentés.
NAMED=0
OPTION_VALUE=0
for ARG in "$@"; do
  if [ "$OPTION_VALUE" = 1 ]; then
    OPTION_VALUE=0
    continue
  fi
  case "$ARG" in
    -p|--pattern|-s|--start-directory|-t|--top-level-directory|-k|--durations)
      OPTION_VALUE=1 ;;
    -*) ;;
    *) NAMED=1 ;;
  esac
done
if [ "$NAMED" = 1 ]; then
  TEST_ARGS=(-m unittest -v "$@")
else
  TEST_ARGS=(-m unittest discover -s tests -t . -v "$@")
fi

echo
echo "=== pilote psql ==="
AMEESH_DRIVER=psql python3 "${TEST_ARGS[@]}"

VENV=${AMEESH_TEST_VENV:-.venv}  # L159b : venv en cache sur la VM de déport
if [ ! -x "$VENV/bin/python" ]; then
  echo "=== création de $VENV (pilote psycopg, dépendance du paquet depuis L61) ==="
  python3 -m venv "$VENV" || true
fi
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import psycopg, yaml" 2>/dev/null; then
  echo
  echo "=== pilote psycopg ==="
  AMEESH_DRIVER=psycopg "$VENV/bin/python" "${TEST_ARGS[@]}"
elif [ -x "$VENV/bin/pip" ]; then
  echo "=== installation de psycopg dans $VENV ==="
  if "$VENV/bin/pip" install --quiet 'psycopg[binary]' cryptography pyyaml 2>/dev/null; then
    echo
    echo "=== pilote psycopg ==="
    AMEESH_DRIVER=psycopg "$VENV/bin/python" "${TEST_ARGS[@]}"
  else
    echo "psycopg non installé (hors ligne ?) : passe psql seulement"
  fi
fi

echo
echo "tests terminés"
