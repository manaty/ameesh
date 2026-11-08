#!/usr/bin/env bash
# #5495 C3 — retour à BG (repli #190310 §5), lancé depuis bureau-goal. À BLANC
# PAR DÉFAUT ; --apply pour agir. Aucune réparation sur place : on désactive L2
# et BG reprend la coupure, après preuve qu'aucune séquence ne tourne.
#   ./retour-c3-AG1.sh [--apply] activation
#       minuterie L2 désactivée (jamais l'unité tuée en cours de séquence :
#       son piège TERM fait le retour d'hôte), config de soumission retirée.
#   ./retour-c3-AG1.sh [--apply] prealables <TS de pose-c3-AG1.sh prealables>
#       remet canon ab4ae5b1, canon_ref, minuterie 5c1 et arguments d'avant.
set -euo pipefail
AG1=78.232.56.22
H=/home/ameesh-staging U=$H/.config/systemd/user C=$H/.config/ameesh
SUBMIT=/etc/ameesh/staging-submit.json SOUMISSION=/home/ameesh/.config/ameesh/staging-executor.json
PY=/home/ameesh/.local/share/ameesh/venv-courant/bin/python
R() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$AG1" "$@"; }
stop() { echo "STOP : $*" >&2; exit 1; }
APPLY=0; [ "${1:-}" = --apply ] && { APPLY=1; shift; }
geste() { echo "GESTE : $1"; [ $APPLY = 1 ] && R "$2"; true; }
mode=${1:?activation | prealables}; shift
TS=$(date -u +%Y%m%dT%H%MZ)
echo "retour C3 AG1 mode=$mode apply=$APPLY TS=$TS"

if [ "$mode" = activation ]; then
  geste "minuterie L2 d'AG1 désactivée" "systemctl --user -M ameesh-staging@ disable --now ameesh-staging.timer"
  etat=$(R "systemctl --user -M ameesh-staging@ is-active ameesh-staging.service" || true)
  if [ "$etat" != inactive ]; then
    echo "SÉQUENCE EN COURS (ameesh-staging.service $etat) : ne pas tuer. Son retour d'hôte et sa" \
         "levée de drain lui appartiennent ; attendre l'état terminal, puis relancer ce retour." >&2
    exit 3
  fi
  geste "config de soumission retirée du compte ameesh (gardée dans /root)" \
    "install -d -m 700 /root/ameesh-operator-C3-AG1-retour-$TS && if [ -e $SOUMISSION ]; then mv $SOUMISSION /root/ameesh-operator-C3-AG1-retour-$TS/; fi"
  echo "dernier enregistrement mesh-staging (lecture) :"
  R "runuser -u ameesh -- env HOME=/home/ameesh $PY -c 'from ameesh import config,db,staging_executor as s;import json;d=s.latest(db.connect(config.load(),writer=False),\"mesh-staging\") or {};print(json.dumps({k:d.get(k) for k in (\"state\",\"host\",\"sha\",\"run\",\"blocked_reason\")}))'"
  cat <<'EOF'
BG reprend la coupure, comme le 08/11 10:00Z : nouveau dossier
~/.local/state/ameesh/operators/staging-cut-<TS>/ (worker.py copié, SHA demandé,
--sans-poste --absent manaty-mesh-1=… --rapporteurs "bureau mesh-design auditeur"),
seulement quand « staging_watch in_flight » est vide. Une demande L2 restée
pending n'est pas rejouée : réactiver L2 exige un nouveau feu, et l'exécuteur
refuse une demande plus ancienne que le contenu actif (staging_pending).
EOF
fi

if [ "$mode" = prealables ]; then
  POSE=${1:?TS de la pose des préalables}; RECUS=/root/ameesh-operator-C3-AG1-$POSE
  R "test -d $RECUS" || stop "reçus $RECUS absents"
  [ "$(R "systemctl --user -M ameesh-staging@ is-enabled ameesh-staging.timer" || true)" = disabled ] || stop "minuterie encore activée : retour activation d'abord"
  [ "$(R "systemctl --user -M ameesh-staging@ is-active ameesh-staging.service" || true)" = inactive ] || stop "unité active"
  REF=$(R "python3 -I -c 'import json;print(json.load(open(\"$RECUS/canon-receipt.json\"))[\"ref\"])'")
  RET=$H/staging/canon-ab4ae5b1-retained-before-${REF:0:8}
  geste "canon remis (b8… conservé en canon-${REF:0:8}-retired-$TS)" \
    "runuser -u ameesh-staging -- mv -T $H/staging/canon $H/staging/canon-${REF:0:8}-retired-$TS && runuser -u ameesh-staging -- mv -T $RET $H/staging/canon"
  geste "config.json, minuterie et arguments d'avant (octets des reçus), daemon-reload" \
    "install -m 600 -o ameesh-staging -g ameesh-staging $RECUS/config.json $C/config.json && install -m 600 -o ameesh-staging -g ameesh-staging $RECUS/staging-executor.json $C/staging-executor.json && install -m 644 -o ameesh-staging -g ameesh-staging $RECUS/ameesh-staging.timer $U/ameesh-staging.timer && install -m 644 -o root -g root $RECUS/staging-submit.json $SUBMIT && systemctl --user -M ameesh-staging@ daemon-reload && sha256sum $U/ameesh-staging.timer $C/staging-executor.json $SUBMIT && runuser -u ameesh-staging -- git -C $H/staging/canon rev-parse HEAD"
fi
