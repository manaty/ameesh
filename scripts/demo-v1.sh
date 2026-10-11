#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Démonstration d'ameesh v1 sur le banc : le scénario de l'essai de bout en bout
# (tests/test_bout_en_bout.py), commenté pas à pas, sur une base temporaire
# effacée à la fin (avec le dossier de travail).
#
#   scripts/demo-v1.sh
#
# AMEESH_DEMO_DSN : base d'administration du banc, sans paramètres
# (défaut AMEESH_TEST_DSN, sinon postgresql://agent_mesh@127.0.0.1:55432/agent_mesh) ;
# le mot de passe vient de PGPASSWORD ou de ~/.pgpass. La démonstration crée une
# base `ameesh_demo_…` (repli : un schéma du même nom si la création est refusée)
# et la supprime en sortant, même en cas d'erreur ou de Ctrl-C.
#
# Rien de réel : organisation FICTIVE (acme), faux harnais (tests/fakebin —
# jamais claude, codex ni dsh), faux gh (jamais GitHub), passkey LOGICIELLE
# (tests/webauthn_soft.py — jamais une vraie clé). ameesh-approve n'écoute que
# sur 127.0.0.1 ; le mandataire HTTPS (page Nexlink) et le téléphone sont simulés
# par tests/banc_v1.py ; tout le reste passe par les commandes ameesh.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$PWD
PY=${PYTHON:-python3}
BASE_DSN=${AMEESH_DEMO_DSN:-${AMEESH_TEST_DSN:-postgresql://agent_mesh@127.0.0.1:55432/agent_mesh}}

# -- isolement : aucune variable du mesh réel, aucune configuration du poste --
for var in $(compgen -e | grep -E '^(AMEESH_|AGENT_MESH_|AGENT_MAIL_|GIT_)' || true); do
  unset "$var"
done
WORK=$(mktemp -d "${TMPDIR:-/tmp}/ameesh-demo-v1.XXXXXX")
NOM=ameesh_demo_$(date +%s)_$$
MODE=
APPROVE_PID=

export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"
export AMEESH_CONFIG="$WORK/absent.json" AGENT_MESH_CONFIG="$WORK/absent.json"
export AMEESH_APPROVE_CONFIG="$WORK/absent.json"
export AMEESH_STATE="$WORK/etat" AGENT_MAIL_STATE="$WORK/v0" AGENT_MAIL_CONFIG="$WORK/conf"
export AMEESH_BIN_DIR="$REPO/tests/fakebin" AMEESH_TEST_LOG="$WORK/tours.jsonl"
export AMEESH_CLAUDE_BIN="$REPO/tests/fakebin/claude" AMEESH_CODEX_BIN="$REPO/tests/fakebin/codex"
export AMEESH_DSH_BIN="$REPO/tests/fakebin/dsh"
export AMEESH_GH_BIN="$REPO/tests/fakebin/gh"
export AMEESH_FAKE_GH_STATE="$WORK/gh-etat.json" AMEESH_FAKE_GH_LOG="$WORK/gh-journal.jsonl"
export AMEESH_CANON="$WORK/canon/acme" AMEESH_HOST=atelier
export AMEESH_HUMANS=alice,bruno
# L125 : la démo réveille tout de suite (`agent-runner --once` n'attend pas la
# fenêtre de regroupement du courrier, 90 s par défaut)
export AMEESH_MAIL_BATCH=0
# jauges de forfait isolées, comme dans les tests : la garde de budget reste
# active, mais elle lit un CODEX_HOME vide (effacé avec le dossier de travail),
# jamais les journaux réels du poste (~/.codex) — un forfait réel avancé
# mettrait sinon en pause les agents factices
export CODEX_HOME="$(mktemp -d "$WORK/codex-home.XXXXXX")"
export AMEESH_APPROVE_RP_ID=approve.example.test
export AMEESH_APPROVE_ORIGINS=https://approve.example.test
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=demo GIT_AUTHOR_EMAIL=demo@example.invalid
export GIT_COMMITTER_NAME=demo GIT_COMMITTER_EMAIL=demo@example.invalid

# -- affichage --------------------------------------------------------------
if [ -t 1 ]; then GRAS=$'\033[1m'; PALE=$'\033[2m'; FIN=$'\033[0m'; else GRAS=; PALE=; FIN=; fi
etape() { printf '\n%s== %s%s\n' "$GRAS" "$*" "$FIN"; }
note() { printf '%s   # %s%s\n' "$PALE" "$*" "$FIN"; }
montre() { printf '\n$ %s\n' "$*"; }   # une ligne de commande, telle quelle
affiche() {                              # les arguments, entre guillemets si besoin
  local ligne= arg
  for arg in "$@"; do
    case "$arg" in
      ''|*[[:space:]\'\"]*) ligne+=" \"${arg//\"/\\\"}\"" ;;
      *) ligne+=" $arg" ;;
    esac
  done
  printf '\n$%s\n' "$ligne"
}
lance() { affiche "$@"; "$@"; }
attendu() {   # attendu <code> <commande…> : la commande DOIT rendre ce code
  local code=$1 rc=0
  shift
  affiche "$@"
  "$@" || rc=$?
  [ "$rc" -eq "$code" ] || { echo "démo interrompue : code $rc, $code attendu" >&2; exit 1; }
  note "code de sortie $rc, comme attendu"
}
json() { "$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$@"; }

ameesh() { "$PY" -m ameesh.main "$@"; }
agent-runner() { "$PY" -m ameesh.runner "$@"; }
ameesh-approve() { "$PY" -m ameesh.approve "$@"; }
banc() { "$PY" -m tests.banc_v1 "$@"; }

# -- nettoyage, quoi qu'il arrive --------------------------------------------
nettoie() {
  local rc=$?
  set +e
  if [ -n "$APPROVE_PID" ]; then
    kill "$APPROVE_PID" 2>/dev/null
    wait "$APPROVE_PID" 2>/dev/null
  fi
  case "$MODE" in
    base)
      psql "$BASE_DSN" -Xq -c "DROP DATABASE IF EXISTS $NOM WITH (FORCE)" >/dev/null 2>&1 \
        || echo "attention : base $NOM non supprimée" >&2 ;;
    schema)
      "$PY" -c 'import sys
from ameesh import config, db
d = db.connect(config.load()); d.execute("DROP SCHEMA IF EXISTS %s CASCADE" % db.quote_ident(sys.argv[1])); d.close()' "$NOM" \
        || echo "attention : schéma $NOM non supprimé" >&2 ;;
  esac
  rm -rf "$WORK"
  if [ "$rc" -eq 0 ]; then
    printf '\n%sdémonstration terminée : base temporaire %s et dossier de travail effacés%s\n' \
      "$GRAS" "$NOM" "$FIN"
  else
    echo "démonstration en échec (code $rc) : base temporaire et dossier effacés" >&2
  fi
  exit "$rc"
}
trap nettoie EXIT
trap 'exit 130' INT TERM

# ===========================================================================
etape "0. Une base temporaire"
if command -v psql >/dev/null 2>&1 \
    && psql "$BASE_DSN" -Xq -v ON_ERROR_STOP=1 -c "CREATE DATABASE $NOM" >/dev/null 2>&1; then
  MODE=base
  export AMEESH_DSN="${BASE_DSN%/*}/$NOM"
  note "base $NOM créée sur le banc ; elle sera supprimée en sortant"
else
  MODE=schema
  export AMEESH_DSN="$BASE_DSN" AMEESH_SCHEMA="$NOM"
  note "création de base refusée : schéma $NOM dans la base du banc, supprimé en sortant"
fi
lance ameesh migrate

# ===========================================================================
etape "1. Le canon d'exemple (organisation fictive acme) dans un dépôt git"
note "un dépôt nu tient lieu de branche canonique distante ; ameesh lit origin/main"
note "par les objets git, jamais l'arbre de travail (spec §4.1)"
mkdir -p "$WORK/canon" "$WORK/travail/orchestre" "$WORK/travail/relecteur" "$AMEESH_CANON"
git init -q --bare -b main "$WORK/canon/acme.git"
git init -q -b main "$AMEESH_CANON"
cp -r examples/canon/. "$AMEESH_CANON/"
# les dossiers de travail des agents de l'hôte atelier, ramenés dans la démo
sed -i "s|^cwd: .*|cwd: $WORK/travail/orchestre|" "$AMEESH_CANON/placements/orchestre-atelier.md"
sed -i "s|^cwd: .*|cwd: $WORK/travail/relecteur|" "$AMEESH_CANON/placements/relecteur-atelier.md"
git -C "$AMEESH_CANON" add -A
git -C "$AMEESH_CANON" commit -qm "canon initial"
git -C "$AMEESH_CANON" remote add origin "$WORK/canon/acme.git"
git -C "$AMEESH_CANON" push -q origin main
git -C "$AMEESH_CANON" fetch -q origin
lance ameesh canon check

# ===========================================================================
etape "2. Du canon vers le registre de l'hôte atelier"
note "obligatoire sur chaque hôte avant toute réclamation : sans état « ok » du canon,"
note "aucun agent du canon n'est réclamable (fail closed) ; la première fois, la branche"
note "canonique de confiance du registre des authentificateurs est donnée par --bootstrap-ref"
note "(journalisé, ignoré ensuite : les sync suivants partent du dernier commit appliqué)"
lance ameesh canon sync --bootstrap-ref main
lance ameesh show orchestre

# ===========================================================================
etape "3. L'exécuteur réclame les deux agents factices"
lance ameesh mail send orchestre "Bonjour orchestre, le lot 7 d'acme-web est à préparer." --from alice
lance ameesh mail send relecteur "Bonjour relecteur, tu reliras le lot 7 d'acme-web." --from alice
lance agent-runner --once
note "chaque tour a lancé un FAUX harnais (tests/fakebin) avec l'identité liée au bail :"
"$PY" -c 'import json, sys
for line in open(sys.argv[1]):
    t = json.loads(line)
    print("     %-7s pour %-10s bail epoch %s, hôte %s" % (t["harness"], t["env"]["AGENT_MAIL_NAME"],
          t["env"]["AMEESH_LEASE_EPOCH"], t["env"]["AMEESH_HOST"]))' "$AMEESH_TEST_LOG"
lance ameesh list

# ===========================================================================
etape "4. Un message d'agent à agent, lisible dans le fil"
MESSAGE="Relecteur, le lot 7 est prêt : relis la PR acme/acme-web#7 avant la fusion."
montre "AGENT_MAIL_NAME=orchestre ameesh mail send relecteur \"$MESSAGE\""
AGENT_MAIL_NAME=orchestre ameesh mail send relecteur "$MESSAGE"
lance ameesh fil show acme-web
note "la boîte Postgres distribue ; le fil est la référence lisible par les humains (R12) ;"
note "son projet est l'équipe (team) des agents au canon : acme-web"
lance agent-runner --once
note "relecteur a été réveillé par le message (un tour du faux codex)"

# ===========================================================================
etape "5. Une action git-merge (faux gh), classe irréversible : refusée sans reçu"
printf '{"prs": {"acme/acme-web#7": {"state": "OPEN", "mergeCommit": null}}}\n' >"$AMEESH_FAKE_GH_STATE"
montre "AGENT_MAIL_NAME=orchestre ameesh action propose --connector git-merge --operation merge \\
    --project acme-web --target acme/acme-web#7 --args '{\"method\": \"squash\"}' --approver human:alice"
AGENT_MAIL_NAME=orchestre ameesh action propose --connector git-merge --operation merge \
  --project acme-web --target acme/acme-web#7 --args '{"method": "squash"}' \
  --approver human:alice --json >"$WORK/action.json"
ACTION=$(json "$WORK/action.json" action_id)
DIGEST=$(json "$WORK/action.json" digest)
note "action $ACTION, empreinte $DIGEST"
attendu 1 ameesh action execute "$ACTION"
lance ameesh decisions --for human:alice

# ===========================================================================
etape "6. ameesh-approve, et la passkey d'alice enrôlée par la voie du canon"
note "en production : utilisateur Unix dédié, exposé en HTTPS par une page Nexlink"
note "(acte du propriétaire) ; ici, 127.0.0.1 et un port éphémère"
install -d -m 700 "$WORK/approve"
lance ameesh-approve gen-token --token-file "$WORK/approve/service-token"
montre "ameesh-approve serve --bind 127.0.0.1 --port 0 --token-file … --state-dir … &"
# lancé directement (pas par la fonction) : $! est bien le processus du service
"$PY" -m ameesh.approve serve --bind 127.0.0.1 --port 0 --token-file "$WORK/approve/service-token" \
  --state-dir "$WORK/approve/etat" >"$WORK/approve/serve.log" 2>&1 &
APPROVE_PID=$!
PORT=
for _ in $(seq 1 150); do
  PORT=$(sed -n 's|.*http://127\.0\.0\.1:\([0-9][0-9]*\).*|\1|p' "$WORK/approve/serve.log" | head -n 1)
  [ -n "$PORT" ] && break
  kill -0 "$APPROVE_PID" 2>/dev/null || { cat "$WORK/approve/serve.log" >&2; exit 1; }
  sleep 0.2
done
[ -n "$PORT" ] || { echo "ameesh-approve ne répond pas" >&2; exit 1; }
note "ameesh-approve écoute sur 127.0.0.1:$PORT"
# ameesh, client du service : l'URL de l'API et le jeton de service (fichier 0600)
export AMEESH_APPROVE_URL="http://127.0.0.1:$PORT"
export AMEESH_APPROVE_TOKEN_FILE="$WORK/approve/service-token"
montre "ameesh-approve enroll-link --approver human:alice --state-dir …"
LIEN=$(ameesh-approve enroll-link --approver human:alice --state-dir "$WORK/approve/etat")
note "lien à usage unique, à ouvrir SUR LE TÉLÉPHONE : ${LIEN%/*}/<jeton>"
montre "banc enroll …        # le « téléphone » crée la passkey (logicielle)"
banc enroll --port "$PORT" --link "$LIEN" --key "$WORK/passkey-alice.json" | tee "$WORK/enrolement.json"
PROPOSITION="$WORK/approve/etat/proposals/$(json "$WORK/enrolement.json" proposal)"
note "rien n'est actif : le service n'écrit qu'une PROPOSITION de canon"
lance ameesh authenticator list --approver human:alice
montre "banc canon-entry …   # la PR : l'entrée proposée rejoint la fiche Member d'alice"
banc canon-entry --proposal "$PROPOSITION" --member "$AMEESH_CANON/membres/alice.md"
lance git -C "$AMEESH_CANON" diff --stat
git -C "$AMEESH_CANON" commit -qam "canon : passkey d'alice (PR revue, fusionnée)"
git -C "$AMEESH_CANON" push -q origin main
git -C "$AMEESH_CANON" fetch -q origin
note "PR fusionnée (commit $(git -C "$AMEESH_CANON" rev-parse --short HEAD) poussé sur la branche canonique)"
lance ameesh canon check
lance ameesh canon sync
note "canon sync recopie Member.authenticators dans le registre de confiance (spec §8.2),"
note "depuis le commit fusionné de la branche canonique seulement"
lance ameesh authenticator list --approver human:alice

# ===========================================================================
etape "7. Demande d'approbation, signature au téléphone, reçu"
note "ameesh dépose la demande (AMEESH_APPROVE_URL, jeton de service en 0600, jamais affiché)"
montre "AGENT_MAIL_NAME=orchestre ameesh action request $ACTION --approver human:alice"
AGENT_MAIL_NAME=orchestre ameesh action request "$ACTION" --approver human:alice --json \
  >"$WORK/demande.json"
LIEN=$(json "$WORK/demande.json" link)
note "lien à usage unique pour le téléphone d'alice : ${LIEN%/*}/<jeton> (écrit aussi dans le fil)"
attendu 5 ameesh action fetch-receipt "$ACTION"
montre "banc approve …       # le téléphone affiche le résumé RECALCULÉ par le service et signe"
banc approve --port "$PORT" --link "$LIEN" --key "$WORK/passkey-alice.json"
note "ameesh récupère le reçu, le VÉRIFIE lui-même, puis l'attache à l'action"
lance ameesh action fetch-receipt "$ACTION" --out "$WORK/recu.json" --by human:alice
lance ameesh receipt verify "$WORK/recu.json" --digest "$DIGEST" --action-id "$ACTION"

# ===========================================================================
etape "8. Exécution : gh fusionne, puis la réponse se perd"
note "faux gh : mode lose (fusion puis coupure) et relecture impossible : issue inconnue"
montre "AMEESH_FAKE_GH_MODE=lose AMEESH_FAKE_GH_VIEW=absent ameesh action execute $ACTION"
RC=0
AMEESH_FAKE_GH_MODE=lose AMEESH_FAKE_GH_VIEW=absent ameesh action execute "$ACTION" || RC=$?
[ "$RC" -eq 4 ] || { echo "démo interrompue : code $RC, 4 (issue inconnue) attendu" >&2; exit 1; }
note "code de sortie 4 : issue inconnue — jamais de nouvelle tentative automatique"
lance ameesh decisions --for human:alice
attendu 1 ameesh action execute "$ACTION"

# ===========================================================================
etape "9. Réconciliation"
lance ameesh action reconcile "$ACTION"
lance ameesh action show "$ACTION"
note "chaque transition de l'action est aussi dans le fil du projet (spec §7.1) :"
lance ameesh fil show acme-web --last 6
FUSIONS=$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1]))["prs"]["acme/acme-web#7"]["merges"])' "$AMEESH_FAKE_GH_STATE")
note "fusions réellement faites sur la PR (faux gh) : $FUSIONS — aucun doublon"
note "le nonce du reçu a été consommé au lancement : le rejouer échoue"
attendu 1 ameesh receipt verify "$WORK/recu.json" --digest "$DIGEST" --action-id "$ACTION"
