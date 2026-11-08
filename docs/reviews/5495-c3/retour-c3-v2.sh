#!/usr/bin/env bash
# #5495 C3 v2 — retour à BG (repli #190310 §5), depuis bureau-goal. À BLANC PAR
# DÉFAUT ; --apply pour agir. Aucune réparation sur place.
#   ./retour-c3-v2.sh [--apply] activation <TS de « pose-c3-v2.sh activer »>
#       AG1 et AG2 : minuterie L2 désactivée (unité jamais tuée en séquence : son
#       piège TERM fait le retour d'hôte ; rc 3 si une séquence tourne), drop-in
#       --no-staging de la release reposé (octets des reçus), soumission retirée.
#   ./retour-c3-v2.sh [--apply] prealables <AG1|AG2> <TS de « pose-c3-v2.sh prealables »>
#       canon, canon_ref, minuterie 5c1, arguments d'avant ; sur AG2 aussi
#       bascule.env du 04/11, garde et drop-in retirés (gardés dans les reçus).
set -euo pipefail
declare -A IP=([AG1]=78.232.56.22 [AG2]=78.232.60.1)
H=/home/ameesh-staging U=$H/.config/systemd/user C=$H/.config/ameesh
SUBMIT=/etc/ameesh/staging-submit.json SOUMISSION=/home/ameesh/.config/ameesh/staging-executor.json
RD=/home/ameesh/.config/systemd/user/ameesh-release.service.d RDROP_SHA=3c213e4349d334a0
PY=/home/ameesh/.local/share/ameesh/venv-courant/bin/python
R() { local h=$1; shift; ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@${IP[$h]}" "$@"; }
stop() { echo "STOP : $*" >&2; exit 1; }
APPLY=0; [ "${1:-}" = --apply ] && { APPLY=1; shift; }
geste() { echo "GESTE [$1] : $2"; [ $APPLY = 1 ] && R "$1" "$3"; true; }
mode=${1:?activation | prealables}; shift
TS=$(date -u +%Y%m%dT%H%MZ)
echo "retour C3 v2 mode=$mode apply=$APPLY TS=$TS"

if [ "$mode" = activation ]; then
  ACT=${1:?"TS de l'activation"}
  for h in AG1 AG2; do
    geste $h "minuterie L2 désactivée" "systemctl --user -M ameesh-staging@ disable --now ameesh-staging.timer"
  done
  for h in AG1 AG2; do
    etat=$(R $h "systemctl --user -M ameesh-staging@ is-active ameesh-staging.service" || true)
    if [ "$etat" != inactive ]; then
      echo "SÉQUENCE EN COURS sur $h (ameesh-staging.service $etat) : ne pas tuer ; son retour d'hôte" \
           "et ses levées de drain lui appartiennent. Attendre l'état terminal, puis relancer ce retour." >&2
      exit 3
    fi
  done
  for h in AG1 AG2; do
    A=/root/ameesh-operator-C3-$h-activation-$ACT
    R $h "test -f $A/10-sans-staging.conf" || stop "$h : drop-in de release absent des reçus $A"
    [ "$(R $h "sha256sum $A/10-sans-staging.conf" | cut -c1-16)" = $RDROP_SHA ] || stop "$h : drop-in des reçus ≠ 3c213e43"
    geste $h "drop-in --no-staging reposé, daemon-reload du compte ameesh, contrôle ExecStart" \
      "install -m 644 -o ameesh -g ameesh $A/10-sans-staging.conf $RD/10-sans-staging.conf && systemctl --user -M ameesh@ daemon-reload && systemctl --user -M ameesh@ show ameesh-release.service -p ExecStart --value | grep -c -- --no-staging"
    geste $h "config de soumission retirée (gardée dans $A)" \
      "if [ -e $SOUMISSION ]; then mv $SOUMISSION $A/staging-executor.json.retire-$TS; fi"
  done
  echo "dernier enregistrement mesh-staging (lecture) :"
  R AG1 "runuser -u ameesh -- env HOME=/home/ameesh $PY -c 'from ameesh import config,db,staging_executor as s;import json;d=s.latest(db.connect(config.load(),writer=False),\"mesh-staging\") or {};print(json.dumps({k:d.get(k) for k in (\"state\",\"host\",\"sha\",\"run\",\"blocked_reason\")}))'"
  cat <<'EOF'
BG reprend la coupure comme le 08/11 10:00Z (staging-cut-<TS>, même worker et mêmes
arguments), seulement quand « staging_watch in_flight » est vide. Une demande L2
restée pending n'est pas rejouée : réactiver L2 exige un nouveau feu, et l'exécuteur
refuse une demande plus ancienne que le contenu actif (staging_pending).
EOF
fi

if [ "$mode" = prealables ]; then
  h=${1:?AG1 | AG2}; POSE=${2:?TS de la pose}; RECUS=/root/ameesh-operator-C3-$h-$POSE
  R $h "test -d $RECUS" || stop "$h reçus $RECUS absents"
  [ "$(R $h "systemctl --user -M ameesh-staging@ is-enabled ameesh-staging.timer" || true)" = disabled ] || stop "$h minuterie encore activée : retour activation d'abord"
  [ "$(R $h "systemctl --user -M ameesh-staging@ is-active ameesh-staging.service" || true)" = inactive ] || stop "$h unité active"
  REF=$(R $h "python3 -I -c 'import json;print(json.load(open(\"$RECUS/canon-receipt.json\"))[\"ref\"])'")
  AVANT=$(R $h "python3 -I -c 'import json;print(json.load(open(\"$RECUS/config.json\"))[\"canon_ref\"])'")
  RET=$H/staging/canon-${AVANT:0:8}-retained-before-${REF:0:8}
  geste $h "canon ${AVANT:0:8} remis (${REF:0:8} gardé en canon-${REF:0:8}-retired-$TS)" \
    "runuser -u ameesh-staging -- mv -T $H/staging/canon $H/staging/canon-${REF:0:8}-retired-$TS && runuser -u ameesh-staging -- mv -T $RET $H/staging/canon"
  geste $h "config.json, staging-executor.json, minuterie, bascule.env et $SUBMIT d'avant (octets des reçus)" \
    "for f in config.json staging-executor.json bascule.env; do install -m 600 -o ameesh-staging -g ameesh-staging $RECUS/\$f $C/\$f; done && install -m 644 -o ameesh-staging -g ameesh-staging $RECUS/ameesh-staging.timer $U/ameesh-staging.timer && install -m 644 -o root -g root $RECUS/staging-submit.json $SUBMIT"
  if [ $h = AG2 ]; then
    geste AG2 "garde et drop-in retirés (gardés dans $RECUS)" \
      "mv $U/ameesh-staging.service.d $RECUS/ameesh-staging.service.d.retire && mv $H/.local/libexec $RECUS/libexec.retire"
  fi
  geste $h "daemon-reload et relevé" \
    "systemctl --user -M ameesh-staging@ daemon-reload && sha256sum $U/ameesh-staging.timer $C/staging-executor.json $C/bascule.env $SUBMIT && runuser -u ameesh-staging -- git -C $H/staging/canon rev-parse HEAD && systemctl --user -M ameesh-staging@ show ameesh-staging.service -p DropInPaths --value"
fi
