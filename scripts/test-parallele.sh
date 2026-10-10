#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Suite de tests en N parts parallèles, chacune avec son propre Postgres en
# conteneur (L116) — le même découpage que la CI (scripts/test-parts.py).
#
#   scripts/test-parallele.sh [-n N] [-p psql|psycopg|les-deux]
#                             [--liste FICHIER] [--maj-durees]
#
#   -n N          nombre de parts par pilote (défaut : 4)
#   -p PILOTE     psql, psycopg ou les-deux (défaut : les-deux, en même temps)
#   --liste F     seulement les modules de F (ex. tests/parts/pilote-psql.txt)
#   --maj-durees  réécrit tests/parts/durees.json avec les durées mesurées
#
# Les conteneurs sont jetables : données en tmpfs, fsync coupé, port publié
# sur la boucle locale seulement, authentification « trust » (aucun mot de
# passe, aucun secret sur la machine). Ils sont supprimés à la sortie.
# Le port est choisi par Docker (libre au moment du lancement).
# AMEESH_PG_IMAGE (défaut postgres:17) et AMEESH_PARTS_NOM (préfixe des
# conteneurs) s'ajustent.
set -euo pipefail
cd "$(dirname "$0")/.."

N=4
PILOTES=(psql psycopg)
LISTE=()
MAJ=0
while [ $# -gt 0 ]; do
  case "$1" in
    -n) N=$2; shift 2 ;;
    -p) case "$2" in les-deux) PILOTES=(psql psycopg) ;; psql|psycopg) PILOTES=("$2") ;;
          *) echo "pilote inconnu : $2" >&2; exit 2 ;; esac; shift 2 ;;
    --liste) LISTE=(--liste "$2"); shift 2 ;;
    --maj-durees) MAJ=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "option inconnue : $1" >&2; exit 2 ;;
  esac
done

IMAGE=${AMEESH_PG_IMAGE:-postgres:17}
NOM=${AMEESH_PARTS_NOM:-ameesh-pg-part-$$}
SORTIE=$(mktemp -d "${TMPDIR:-/tmp}/ameesh-parts-XXXXXX")

CONTENEURS=()
PIDS=()
nettoyer() {
  for pid in "${PIDS[@]}"; do pkill -P "$pid" 2>/dev/null || true; kill "$pid" 2>/dev/null || true; done
  for c in "${CONTENEURS[@]}"; do docker rm -f "$c" >/dev/null 2>&1 || true; done
}
trap nettoyer EXIT
trap 'echo "interrompu" >&2; exit 130' INT TERM

# --- environnement Python du pilote psycopg (comme scripts/test.sh) ---------
PY_psql=python3
PY_psycopg=.venv/bin/python
if [[ " ${PILOTES[*]} " == *" psycopg "* ]] && ! .venv/bin/python -c "import psycopg, yaml" 2>/dev/null; then
  echo "=== installation de psycopg dans .venv ==="
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/pip install --quiet 'psycopg[binary]' cryptography pyyaml
fi

# --- une base par part -------------------------------------------------------
for pilote in "${PILOTES[@]}"; do
  for k in $(seq 1 "$N"); do
    c="$NOM-$pilote-$k"
    docker run -d --rm --name "$c" \
      -e POSTGRES_USER=ameesh -e POSTGRES_DB=ameesh -e POSTGRES_HOST_AUTH_METHOD=trust \
      -p 127.0.0.1::5432 --tmpfs /var/lib/postgresql/data \
      "$IMAGE" -c fsync=off -c synchronous_commit=off -c full_page_writes=off >/dev/null
    CONTENEURS+=("$c")
  done
done
for c in "${CONTENEURS[@]}"; do
  for _ in $(seq 1 60); do
    # pg_isready seul ne suffit pas : l'image redémarre le serveur après l'init
    docker exec "$c" psql -U ameesh -d ameesh -h 127.0.0.1 -Atc 'SELECT 1' >/dev/null 2>&1 && break
    sleep 1
  done
done
echo "${#CONTENEURS[@]} base(s) prête(s) ; journaux dans $SORTIE"

# --- environnement des tests (comme scripts/test.sh) --------------------------
export AMEESH_REQUIRE_DB=1
export AMEESH_CONFIG="$PWD/.config-absente-pour-les-tests.json"
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
unset DEEPSEEK_API_KEY AMEESH_DEEPSEEK_API_BASE AMEESH_DSN AGENT_MESH_DSN
export AMEESH_BALANCE_INTERVAL=0
# identité git des dépôts temporaires des tests, si la machine n'en a pas
if [ -z "$(git config --global user.name 2>/dev/null || true)" ]; then
  export GIT_CONFIG_COUNT=3
  export GIT_CONFIG_KEY_0=user.name GIT_CONFIG_VALUE_0="ameesh tests"
  export GIT_CONFIG_KEY_1=user.email GIT_CONFIG_VALUE_1="tests@ameesh.invalid"
  export GIT_CONFIG_KEY_2=init.defaultBranch GIT_CONFIG_VALUE_2=main
fi

# --- les parts, en parallèle ---------------------------------------------------
debut=$(date +%s)
i=0
for pilote in "${PILOTES[@]}"; do
  py_var="PY_$pilote"
  for k in $(seq 1 "$N"); do
    port=$(docker port "${CONTENEURS[$i]}" 5432/tcp | head -1)
    port=${port##*:}
    durees=()
    [ "$MAJ" = 1 ] && durees=(--durees-sortie "$SORTIE/$pilote-$k.json")
    (
      t0=$(date +%s)
      rc=0
      AMEESH_DRIVER=$pilote AMEESH_TEST_DSN="postgresql://ameesh@127.0.0.1:$port/ameesh" \
        "${!py_var}" scripts/test-parts.py lancer --parts "$N" --part "$k" --pilote "$pilote" \
        "${LISTE[@]}" "${durees[@]}" >"$SORTIE/$pilote-$k.log" 2>&1 || rc=$?
      echo $(( $(date +%s) - t0 )) >"$SORTIE/$pilote-$k.duree"
      exit "$rc"
    ) &
    PIDS+=("$!")
    i=$((i + 1))
  done
done

echec=0
i=0
for pilote in "${PILOTES[@]}"; do
  for k in $(seq 1 "$N"); do
    if wait "${PIDS[$i]}"; then etat=ok; else etat=ÉCHEC; echec=1; fi
    bilan=$(grep -E '^(Ran |OK|FAILED)' "$SORTIE/$pilote-$k.log" | tr '\n' ' ')
    printf '%-8s part %s/%s : %-5s %s(%s s)\n' "$pilote" "$k" "$N" "$etat" "$bilan" "$(cat "$SORTIE/$pilote-$k.duree" 2>/dev/null || echo ?)"
    i=$((i + 1))
  done
done
PIDS=()
echo "durée totale : $(( $(date +%s) - debut )) s"

if [ "$MAJ" = 1 ]; then
  for pilote in "${PILOTES[@]}"; do
    python3 scripts/test-parts.py durees --pilote "$pilote" "$SORTIE/$pilote"-*.json
  done
fi
if [ "$echec" = 1 ]; then
  echo "des parts ont échoué : voir $SORTIE/*.log" >&2
  exit 1
fi
echo "tests terminés"
