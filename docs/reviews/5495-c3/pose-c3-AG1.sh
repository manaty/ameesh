#!/usr/bin/env bash
# #5495 C3 — pose sur AG1 (compte ameesh-staging, uid 980), lancée depuis
# bureau-goal (root SSH existant). À BLANC PAR DÉFAUT : sans --apply, seules les
# lectures et les contrôles tournent, et les gestes sont imprimés.
#   ./pose-c3-AG1.sh [--apply] prealables <canon-XXXXXXXX.tar.gz> <canon-XXXXXXXX-receipt.json>
#       P1 canon qualifié = canon des agents ; P2 minuterie 1.44.1 (#5853) ;
#       P3 arguments = ceux de BG (--absent manaty-mesh-1, --rapporteurs).
#       Ni activation, ni minuterie activée, ni demande soumise.
#   ./pose-c3-AG1.sh [--apply] activer
#       Seulement au feu du pilote, après « sonde-c3.sh prealables » OK à T-15 :
#       config de soumission du compte ameesh d'AG1, puis minuterie L2 activée.
# Arrêt au premier écart ; retour : retour-c3-AG1.sh.
set -euo pipefail
ICI=$(cd "$(dirname "$0")" && pwd)
AG1=78.232.56.22
H=/home/ameesh-staging U=$H/.config/systemd/user C=$H/.config/ameesh
SUBMIT=/etc/ameesh/staging-submit.json
EXEC_SHA_AVANT=c420d2815dcaecfed226f8c92fb30a0a0ee73ee426c9ac211c6c3a17d1b7c9ba
CANON_AVANT=ab4ae5b1d6f3d91b2c477db7bb7a1acb2de0c5a5
TIMER_5C1=f6460989527742e6d9abbe32758f9f3ee417aebfbb722f0c7b85b3d309b0f425
TIMER_144=d59f9a50f6f7572ea95b17d7f21c3e9ac7c12301987cb9e546bf7afa0e3563d6
REL_144=5f5bc7f2aaae1b36e3f199bff1c119eef6686656
ARGS_JSON='["--sans-poste","--absent","manaty-mesh-1=central runtime deferred; follow up separately","--rapporteurs","bureau mesh-design auditeur"]'
R() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$AG1" "$@"; }
stop() { echo "STOP : $*" >&2; exit 1; }
APPLY=0; [ "${1:-}" = --apply ] && { APPLY=1; shift; }
geste() { echo "GESTE : $1"; [ $APPLY = 1 ] && R "$2"; true; }
mode=${1:?prealables | activer}; shift
TS=$(date -u +%Y%m%dT%H%MZ); RECUS=/root/ameesh-operator-C3-AG1-$TS
echo "pose C3 AG1 mode=$mode apply=$APPLY TS=$TS"

# Contrôles communs : jamais pendant une séquence, unité arrêtée.
[ "$(R "systemctl --user -M ameesh-staging@ is-active ameesh-staging.service" || true)" = inactive ] \
  || stop "ameesh-staging.service n'est pas inactif"
[ "$(systemctl --user list-units --type=service --state=active --no-legend 2>/dev/null | grep -c -i staging)" = 0 ] \
  || stop "une séquence BG est active"

if [ "$mode" = prealables ]; then
  ARCH=${1:?archive du canon}; RECU=${2:?reçu du canon}
  read -r REF TREE ASHA GSHA < <(python3 -I -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d["ref"],d["tree"],d["archive_sha256"],d["git_config_sha256"])' "$RECU")
  [ "$(sha256sum "$ARCH" | cut -c1-64)" = "$ASHA" ] || stop "archive ≠ reçu"
  AGENTS=$(R "runuser -u ameesh -- git -C /home/ameesh/canon rev-parse HEAD")
  [ "$REF" = "$AGENTS" ] || stop "reçu $REF ≠ canon des agents d'AG1 $AGENTS : refaire preparer-canon-c3.sh"
  [ "$(R "systemctl --user -M ameesh-staging@ is-enabled ameesh-staging.timer" || true)" = disabled ] || stop "minuterie L2 déjà activée"
  [ "$(R "runuser -u ameesh-staging -- git -C $H/staging/canon rev-parse HEAD")" = $CANON_AVANT ] || stop "canon 980 ≠ ab4ae5b1"
  [ "$(R "sha256sum $U/ameesh-staging.timer" | cut -c1-64)" = $TIMER_5C1 ] || stop "minuterie ≠ 5c1"
  [ "$(R "sha256sum $C/staging-executor.json $SUBMIT | cut -c1-64 | sort -u")" = $EXEC_SHA_AVANT ] || stop "config exécuteur ≠ c420d281"
  R "test ! -e $RECUS && test ! -e $H/staging/canon-${REF:0:8}-prepared" || stop "reçus ou dossier préparé déjà présents"
  NOUVEAU_TIMER=$(git -C /home/agent/development/manaty/ameesh show $REL_144:deploy/systemd/ameesh-staging.timer)
  [ "$(printf '%s\n' "$NOUVEAU_TIMER" | sha256sum | cut -c1-64)" = $TIMER_144 ] || stop "minuterie 1.44.1 ≠ d59f9a50"
  echo "préalables OK : canon $CANON_AVANT → $REF (arbre $TREE), minuterie $TIMER_5C1 → $TIMER_144"
  [ $APPLY = 1 ] || { echo "à blanc : aucun geste (relancer avec --apply)"; }

  geste "reçus $RECUS (0700) et copies avant" \
    "install -d -m 700 $RECUS && cp -p $U/ameesh-staging.timer $C/staging-executor.json $C/config.json $SUBMIT $RECUS/ && chmod 600 $RECUS/*"
  echo "GESTE : archive du canon vers $RECUS/ (root 0600) et contrôle des membres"
  if [ $APPLY = 1 ]; then
    R "umask 077; cat > $RECUS/canon.tar.gz" < "$ARCH"
    R "umask 077; cat > $RECUS/canon-receipt.json" < "$RECU"
    [ "$(R "sha256sum $RECUS/canon.tar.gz" | cut -c1-64)" = "$ASHA" ] || stop "archive altérée en transit"
    R "python3 -I -c '
import sys, tarfile
for m in tarfile.open(sys.argv[1]):
    p = m.name.split(\"/\")
    if p[0] != \"canon\" or \"..\" in p or m.name.startswith(\"/\") or not (m.isfile() or m.isdir()):
        sys.exit(\"membre refusé : \" + m.name)
' $RECUS/canon.tar.gz" || stop "membre d'archive refusé"
  fi
  P=$H/staging/canon-${REF:0:8}-prepared
  geste "extraction sous 980 dans $P" \
    "runuser -u ameesh-staging -- install -d -m 755 $P && runuser -u ameesh-staging -- tar -xzf - -C $P --no-same-owner --no-same-permissions < $RECUS/canon.tar.gz"
  echo "GESTE : vérification du clone préparé (HEAD, arbre, propre, sans hook, .git/config, fichiers suivis)"
  if [ $APPLY = 1 ]; then
    [ "$(R "runuser -u ameesh-staging -- git -C $P/canon rev-parse HEAD 'HEAD^{tree}'" | paste -sd' ')" = "$REF $TREE" ] || stop "HEAD/arbre du clone préparé"
    [ -z "$(R "runuser -u ameesh-staging -- git -C $P/canon status --porcelain; find $P/canon/.git/hooks -mindepth 1")" ] || stop "clone sale ou hooks"
    R "python3 -I -c '
import hashlib, json, sys
d = json.load(open(sys.argv[1])); root = sys.argv[2]
h = lambda p: hashlib.sha256(open(p, \"rb\").read()).hexdigest()
assert h(root + \"/.git/config\") == d[\"git_config_sha256\"], \".git/config\"
bad = [e[\"path\"] for e in d[\"tracked\"] if h(root + \"/\" + e[\"path\"]) != e[\"sha256\"]]
assert not bad, bad[:3]
print(\"fichiers suivis conformes :\", len(d[\"tracked\"]))
' $RECUS/canon-receipt.json $P/canon" || stop "clone préparé ≠ reçu"
  fi
  RET=$H/staging/canon-${CANON_AVANT:0:8}-retained-before-${REF:0:8}
  geste "bascule du canon (ancien conservé : $RET) et canon_ref=$REF" \
    "runuser -u ameesh-staging -- mv -T $H/staging/canon $RET && runuser -u ameesh-staging -- mv -T $P/canon $H/staging/canon && runuser -u ameesh-staging -- rmdir $P && runuser -u ameesh-staging -- python3 -I -c '
import json, os, sys
p, ref = sys.argv[1:3]; d = json.load(open(p)); d[\"canon_ref\"] = ref
t = p + \".c3.tmp\"; fd = os.open(t, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, \"w\") as f: json.dump(d, f, indent=2); f.write(\"\\n\"); f.flush(); os.fsync(f.fileno())
os.replace(t, p)
' $C/config.json $REF"
  echo "GESTE : minuterie 1.44.1 ($TIMER_144) sous 980, daemon-reload, minuterie toujours désactivée"
  if [ $APPLY = 1 ]; then
    printf '%s\n' "$NOUVEAU_TIMER" | R "runuser -u ameesh-staging -- sh -c 'umask 022; cat > $U/.ameesh-staging.timer.c3 && mv -f $U/.ameesh-staging.timer.c3 $U/ameesh-staging.timer'"
    R "systemctl --user -M ameesh-staging@ daemon-reload"
    [ "$(R "sha256sum $U/ameesh-staging.timer" | cut -c1-64)" = $TIMER_144 ] || stop "minuterie posée ≠ d59f9a50"
    [ "$(R "systemctl --user -M ameesh-staging@ is-enabled ameesh-staging.timer" || true)" = disabled ] || stop "minuterie activée par erreur"
  fi
  geste "arguments = ceux de BG, sur la copie 980 et sur $SUBMIT (octets identiques)" \
    "runuser -u ameesh-staging -- python3 -I -c '
import json, os, sys
p, args = sys.argv[1], json.loads(sys.argv[2]); d = json.load(open(p)); d[\"arguments\"] = args
t = p + \".c3.tmp\"; fd = os.open(t, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, \"w\") as f: json.dump(d, f, indent=2); f.write(\"\\n\"); f.flush(); os.fsync(f.fileno())
os.replace(t, p)
' $C/staging-executor.json '$ARGS_JSON' && install -m 644 -o root -g root $C/staging-executor.json $SUBMIT.c3.tmp && mv -f $SUBMIT.c3.tmp $SUBMIT && sha256sum $C/staging-executor.json $SUBMIT"
  [ $APPLY = 1 ] && "$ICI/sonde-c3.sh" prealables || true
  echo "fin prealables ; TS=$TS (retour : retour-c3-AG1.sh prealables $TS)"
fi

if [ "$mode" = activer ]; then
  "$ICI/sonde-c3.sh" prealables || stop "sonde préalable KO : pas d'activation (BG porte la coupure)"
  RECUS=/root/ameesh-operator-C3-AG1-activation-$TS
  geste "config de soumission du compte ameesh d'AG1 (copie de $SUBMIT, ameesh 0600)" \
    "install -d -m 700 $RECUS && runuser -u ameesh -- test ! -e /home/ameesh/.config/ameesh/staging-executor.json && install -m 600 -o ameesh -g ameesh $SUBMIT /home/ameesh/.config/ameesh/staging-executor.json"
  geste "minuterie L2 d'AG1 activée" \
    "systemctl --user -M ameesh-staging@ enable --now ameesh-staging.timer && systemctl --user -M ameesh-staging@ show ameesh-staging.timer -p ActiveState -p NextElapseUSecMonotonic"
  echo "fin activer ; preuves : sonde-c3.sh apres --depuis $(date -u +%FT%TZ) ; retour : retour-c3-AG1.sh activation"
fi
