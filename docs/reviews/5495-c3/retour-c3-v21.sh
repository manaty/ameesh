#!/usr/bin/env bash
# #5495 C3 v2.1 — retour à BG (repli #190310 §5), depuis bureau-goal. À BLANC PAR DÉFAUT.
#   ./retour-c3-v21.sh [--apply] activation <TS de « pose-c3-v21.sh activer »>
#       AG1 et AG2 : minuterie L2 désactivée (unité jamais tuée en séquence : rc 3) ; fenêtre de release
#       fermée (minuterie release arrêtée) ; drop-in --no-staging reposé depuis les reçus (sha
#       contrôlé) et ExecStart vérifié ; soumission retirée ; minuterie release restaurée.
#   ./retour-c3-v21.sh [--apply] prealables <AG1|AG2> <TS de « pose-c3-v21.sh prealables »>
#       Canon remis SEULEMENT si le canon conservé existe et porte l'ancien HEAD, et si le canon
#       courant est celui de la pose ; sinon canon laissé tel quel (signalé). Octets d'avant des
#       configurations, minuterie, bascule.env ; AG2 : garde et drop-in retirés s'ils existent.
#       Contrôle après restauration.
set -euo pipefail
declare -A IP=([AG1]=78.232.56.22 [AG2]=78.232.60.1)
H=/home/ameesh-staging U=$H/.config/systemd/user C=$H/.config/ameesh
SUBMIT=/etc/ameesh/staging-submit.json SOUMISSION=/home/ameesh/.config/ameesh/staging-executor.json
RD=/home/ameesh/.config/systemd/user/ameesh-release.service.d RDROP_SHA=3c213e4349d334a0
PY=/home/ameesh/.local/share/ameesh/venv-courant/bin/python
R() { local h=$1; shift; ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@${IP[$h]}" "$@"; }
stop() { echo "STOP : $*" >&2; exit 1; }
APPLY=0; [ "${1:-}" = --apply ] && { APPLY=1; shift; }
geste() { echo "GESTE [$1] : $2"; if [ $APPLY = 1 ]; then R "$1" "$3" || stop "geste en échec [$1] : $2"; fi; }
mode=${1:?activation | prealables}; shift
TS=$(date -u +%Y%m%dT%H%M%SZ)-$$
echo "retour C3 v2.1 mode=$mode apply=$APPLY TS=$TS"

if [ "$mode" = activation ]; then
  ACT=${1:?"TS de l'activation"}
  for h in AG1 AG2; do
    R $h "test -d /root/ameesh-operator-C3-$h-activation-$ACT" || stop "$h reçus d'activation absents"
  done
  for h in AG1 AG2; do geste $h "minuterie L2 désactivée" "systemctl --user -M ameesh-staging@ disable --now ameesh-staging.timer"; done
  for h in AG1 AG2; do
    etat=$(R $h "systemctl --user -M ameesh-staging@ show ameesh-staging.service -p ActiveState --value") || stop "$h état du service illisible"
    if [ "$etat" != inactive ]; then
      echo "SÉQUENCE EN COURS sur $h ($etat) : ne pas tuer ; attendre l'état terminal puis relancer ce retour." >&2; exit 3
    fi
  done
  for h in AG1 AG2; do
    A=/root/ameesh-operator-C3-$h-activation-$ACT
    geste $h "fenêtre de release fermée (minuterie arrêtée), tick inactif exigé" \
      "systemctl --user -M ameesh@ stop ameesh-release.timer && [ \"\$(systemctl --user -M ameesh@ show ameesh-release.service -p ActiveState --value)\" = inactive ]"
    if [ $APPLY = 1 ]; then
      if R $h "test -f $A/10-sans-staging.conf"; then
        [ "$(R $h "sha256sum $A/10-sans-staging.conf" | cut -c1-16)" = $RDROP_SHA ] || stop "$h drop-in des reçus ≠ 3c213e43"
      else
        R $h "test -f $RD/10-sans-staging.conf" || stop "$h drop-in ni en place ni dans les reçus"
      fi
    fi
    geste $h "drop-in --no-staging reposé (si retiré), daemon-reload, ExecStart avec --no-staging" \
      "if [ -f $A/10-sans-staging.conf ]; then install -m 644 -o ameesh -g ameesh $A/10-sans-staging.conf $RD/10-sans-staging.conf && mv $A/10-sans-staging.conf $A/10-sans-staging.conf.repose-$TS; fi && systemctl --user -M ameesh@ daemon-reload && systemctl --user -M ameesh@ show ameesh-release.service -p ExecStart --value | grep -q -- --no-staging"
    geste $h "soumission retirée (gardée dans $A)" \
      "if [ -e $SOUMISSION ]; then mv $SOUMISSION $A/staging-executor.json.retire-$TS; fi; test ! -e $SOUMISSION"
    geste $h "minuterie release restaurée dans l'état gardé" \
      "if [ \"\$(sed -n 2p $A/release-timer.etat)\" = active ]; then systemctl --user -M ameesh@ start ameesh-release.timer; fi"
  done
  echo "dernier enregistrement mesh-staging (lecture) :"
  R AG1 "runuser -u ameesh -- env HOME=/home/ameesh $PY -c 'from ameesh import config,db,staging_executor as s;import json;d=s.latest(db.connect(config.load(),writer=False),\"mesh-staging\") or {};print(json.dumps({k:d.get(k) for k in (\"state\",\"host\",\"sha\",\"run\",\"blocked_reason\")}))'"
  echo "BG reprend la coupure (staging-cut-<TS>) dès que staging_watch.in_flight est vide (sonde-c3-v21.sh go, case in_flight)."
fi

if [ "$mode" = prealables ]; then
  h=${1:?AG1 | AG2}; POSE=${2:?TS de la pose}; RECUS=/root/ameesh-operator-C3-$h-$POSE
  R $h "test -d $RECUS" || stop "$h reçus $RECUS absents"
  [ "$(R $h "systemctl --user -M ameesh-staging@ is-enabled ameesh-staging.timer" || true)" = disabled ] || stop "$h minuterie encore activée : retour activation d'abord"
  [ "$(R $h "systemctl --user -M ameesh-staging@ show ameesh-staging.service -p ActiveState --value")" = inactive ] || stop "$h unité active"
  AVANT=$(R $h "cat $RECUS/canon-avant") || stop "$h canon-avant absent des reçus"
  REF=$(R $h "python3 -I -c 'import json;print(json.load(open(\"$RECUS/canon-receipt.json\"))[\"ref\"])'" 2>/dev/null || echo "")
  RET=$H/staging/canon-${AVANT:0:8}-retained-before-${REF:0:8}
  COURANT=$(R $h "runuser -u ameesh-staging -- git -C $H/staging/canon rev-parse HEAD") || stop "$h canon courant illisible"
  CANON_REMIS=0
  if [ -n "$REF" ] && R $h "test -d $RET" && [ "$(R $h "runuser -u ameesh-staging -- git -C $RET rev-parse HEAD")" = "$AVANT" ] && [ "$COURANT" = "$REF" ]; then
    geste $h "canon ${AVANT:0:8} remis (${REF:0:8} gardé en canon-${REF:0:8}-retired-$TS)" \
      "runuser -u ameesh-staging -- mv -T $H/staging/canon $H/staging/canon-${REF:0:8}-retired-$TS && runuser -u ameesh-staging -- mv -T $RET $H/staging/canon"
    CANON_REMIS=1
  elif [ "$COURANT" = "$AVANT" ]; then
    echo "canon déjà à ${AVANT:0:8} (pose interrompue avant le renommage) : canon laissé tel quel"
    R $h "test ! -e $H/staging/canon-${REF:0:8}-prepared" || geste $h "dossier préparé retiré" "rm -rf $H/staging/canon-${REF:0:8}-prepared"
  else
    stop "$h canon courant ${COURANT:0:8} ni ${REF:0:8} ni ${AVANT:0:8}, ou canon conservé absent : canon NON touché, à examiner"
  fi
  geste $h "configurations, minuterie, bascule.env et $SUBMIT d'avant (octets des reçus)" \
    "for f in config.json staging-executor.json bascule.env; do install -m 600 -o ameesh-staging -g ameesh-staging $RECUS/\$f $C/\$f; done && install -m 644 -o ameesh-staging -g ameesh-staging $RECUS/ameesh-staging.timer $U/ameesh-staging.timer && install -m 644 -o root -g root $RECUS/staging-submit.json $SUBMIT"
  if [ $h = AG2 ]; then
    geste AG2 "garde et drop-in retirés s'ils existent (gardés dans $RECUS)" \
      "if [ -e $U/ameesh-staging.service.d ]; then mv $U/ameesh-staging.service.d $RECUS/ameesh-staging.service.d.retire; fi; if [ -e $H/.local/libexec ]; then mv $H/.local/libexec $RECUS/libexec.retire; fi"
  fi
  geste $h "daemon-reload" "systemctl --user -M ameesh-staging@ daemon-reload"
  if [ $APPLY = 1 ]; then
    # Contrôle après restauration (avis impl-03 N) : octets = reçus, canon = avant, unités chargées.
    for f in "$U/ameesh-staging.timer:ameesh-staging.timer" "$C/staging-executor.json:staging-executor.json" "$C/config.json:config.json" "$C/bascule.env:bascule.env" "$SUBMIT:staging-submit.json"; do
      [ "$(R $h "sha256sum < ${f%%:*}")" = "$(R $h "sha256sum < $RECUS/${f#*:}")" ] || stop "$h ${f%%:*} ≠ reçu après restauration"
    done
    [ "$(R $h "runuser -u ameesh-staging -- git -C $H/staging/canon rev-parse HEAD")" = "$AVANT" ] || stop "$h canon ≠ ${AVANT:0:8} après restauration"
    dp=$(R $h "systemctl --user -M ameesh-staging@ show ameesh-staging.service -p DropInPaths --value")
    if [ $h = AG2 ]; then [ -z "$dp" ] || stop "AG2 DropInPaths non vide : $dp"
    else [ "$dp" = "$U/ameesh-staging.service.d/10-native-actor.conf" ] || stop "AG1 DropInPaths : $dp"; fi
    echo "retour prealables $h vérifié (canon remis : $CANON_REMIS)"
  fi
fi
