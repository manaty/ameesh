#!/usr/bin/env bash
# #5495 C3 v2 — pose sur AG1 et AG2 (compte ameesh-staging, uid 980), lancée
# depuis bureau-goal (root SSH existant). À BLANC PAR DÉFAUT : sans --apply,
# seuls les contrôles en lecture tournent et les gestes sont imprimés.
#   ./pose-c3-v2.sh [--apply] prealables <AG1|AG2> <canon-X.tar.gz> <canon-X-receipt.json>
#       P1 canon qualifié = canon des agents ; P2 minuterie 1.44.1 (#5853) ;
#       P3 arguments de BG ; sur AG2 seulement, P5 parité 2A V6 (bascule.env =
#       PUBLIC_ENV avec acteur machine, garde 4bde802e, drop-in ce3ed09c).
#       Ni minuterie activée, ni drop-in de release retiré, ni soumission.
#   ./pose-c3-v2.sh [--apply] activer
#       Au feu du pilote, après « sonde-c3-v2.sh go » OK à T-15, sur AG1 ET AG2
#       (#200655 Q5) : config de soumission du compte ameesh, minuterie L2
#       activée, puis drop-in --no-staging de la release retiré (#200655 Q1).
# Arrêt au premier écart ; retour : retour-c3-v2.sh.
set -euo pipefail
ICI=$(cd "$(dirname "$0")" && pwd)
declare -A IP=([AG1]=78.232.56.22 [AG2]=78.232.60.1)
H=/home/ameesh-staging U=$H/.config/systemd/user C=$H/.config/ameesh
SUBMIT=/etc/ameesh/staging-submit.json SOUMISSION=/home/ameesh/.config/ameesh/staging-executor.json
RD=/home/ameesh/.config/systemd/user/ameesh-release.service.d
RDROP_SHA=3c213e4349d334a0   # 10-sans-staging.conf, identique sur AG1 et AG2 (relevé 08/11)
EXEC_SHA_AVANT=c420d2815dcaecfed226f8c92fb30a0a0ee73ee426c9ac211c6c3a17d1b7c9ba
TIMER_5C1=f6460989527742e6d9abbe32758f9f3ee417aebfbb722f0c7b85b3d309b0f425
TIMER_144=d59f9a50f6f7572ea95b17d7f21c3e9ac7c12301987cb9e546bf7afa0e3563d6
REL_144=5f5bc7f2aaae1b36e3f199bff1c119eef6686656
GUARD_SHA=4bde802e931ab7017934f0ee2f42f07b33b2408905a8aabe3a98063ebeeffdd9
GUARD_SRC=/home/ameesh/preparation-5495-AG1-20261105/phase2A-review-v6/AG1-L2-phase2A-v6/staging-startup-guard.py
DROPIN_SHA=ce3ed09c7a816b1f02358bd6301a1d8bdde0ee3110228a2fe10ea5e0a5a334cc
ENV_AG2_AVANT=15887afb   # bascule.env d'AG2 du 04/11 (AMEESH_OPERATEUR=human:smichea)
ENV_SHA=1223707f
ARGS_JSON='["--sans-poste","--absent","manaty-mesh-1=central runtime deferred; follow up separately","--rapporteurs","bureau mesh-design auditeur"]'
R() { local h=$1; shift; ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@${IP[$h]}" "$@"; }
stop() { echo "STOP : $*" >&2; exit 1; }
APPLY=0; [ "${1:-}" = --apply ] && { APPLY=1; shift; }
geste() { echo "GESTE [$1] : $2"; [ $APPLY = 1 ] && R "$1" "$3"; true; }
ecrire_json() {  # écriture atomique 0600 sous 980 : chemin, clé, valeur JSON
  echo "runuser -u ameesh-staging -- python3 -I -c '
import json, os, sys
p, k, v = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]); d = json.load(open(p)); d[k] = v
t = p + \".c3.tmp\"; fd = os.open(t, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, \"w\") as f: json.dump(d, f, indent=2); f.write(\"\\n\"); f.flush(); os.fsync(f.fileno())
os.replace(t, p)' $1 $2 '$3'"
}
mode=${1:?prealables | activer}; shift
TS=$(date -u +%Y%m%dT%H%MZ)
echo "pose C3 v2 mode=$mode apply=$APPLY TS=$TS"
[ "$(systemctl --user list-units --type=service --state=active --no-legend 2>/dev/null | grep -c -i staging)" = 0 ] \
  || stop "une séquence BG est active"

if [ "$mode" = prealables ]; then
  h=${1:?AG1 | AG2}; ARCH=${2:?archive du canon}; RECU=${3:?reçu du canon}
  [ -n "${IP[$h]:-}" ] || stop "hôte inconnu $h"
  RECUS=/root/ameesh-operator-C3-$h-$TS
  read -r REF TREE ASHA < <(python3 -I -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d["ref"],d["tree"],d["archive_sha256"])' "$RECU")
  [ "$(sha256sum "$ARCH" | cut -c1-64)" = "$ASHA" ] || stop "archive ≠ reçu"
  [ "$(R $h "systemctl --user -M ameesh-staging@ is-active ameesh-staging.service" || true)" = inactive ] || stop "$h ameesh-staging.service actif"
  [ "$(R $h "systemctl --user -M ameesh-staging@ is-enabled ameesh-staging.timer" || true)" = disabled ] || stop "$h minuterie L2 déjà activée"
  AGENTS=$(R $h "runuser -u ameesh -- git -C /home/ameesh/canon rev-parse HEAD")
  [ "$REF" = "$AGENTS" ] || stop "reçu $REF ≠ canon des agents de $h $AGENTS : refaire preparer-canon-c3.sh"
  AVANT=$(R $h "runuser -u ameesh-staging -- git -C $H/staging/canon rev-parse HEAD")
  [ "$(R $h "python3 -I -c 'import json;print(json.load(open(\"$C/config.json\"))[\"canon_ref\"])'")" = "$AVANT" ] || stop "$h canon_ref ≠ HEAD du canon 980"
  [ "$(R $h "sha256sum $U/ameesh-staging.timer" | cut -c1-64)" = $TIMER_5C1 ] || stop "$h minuterie ≠ 5c1"
  [ "$(R $h "sha256sum $C/staging-executor.json $SUBMIT | cut -c1-64 | sort -u")" = $EXEC_SHA_AVANT ] || stop "$h config exécuteur ≠ c420d281"
  R $h "test ! -e $RECUS && test ! -e $H/staging/canon-${REF:0:8}-prepared" || stop "$h reçus ou dossier préparé déjà présents"
  NOUVEAU_TIMER=$(git -C /home/agent/development/manaty/ameesh show $REL_144:deploy/systemd/ameesh-staging.timer)
  [ "$(printf '%s\n' "$NOUVEAU_TIMER" | sha256sum | cut -c1-64)" = $TIMER_144 ] || stop "minuterie 1.44.1 ≠ d59f9a50"
  if [ $h = AG2 ]; then
    [ "$(R AG2 "sha256sum $C/bascule.env" | cut -c1-8)" = $ENV_AG2_AVANT ] || stop "AG2 bascule.env ≠ 15887afb (04/11)"
    R AG2 "test ! -e $H/.local/libexec && test ! -e $U/ameesh-staging.service.d" || stop "AG2 garde ou drop-in déjà présents"
    [ "$(R AG2 "sha256sum $GUARD_SRC" | cut -c1-64)" = $GUARD_SHA ] || stop "source de la garde ≠ 4bde802e"
  else
    [ "$(R AG1 "sha256sum $C/bascule.env" | cut -c1-8)" = $ENV_SHA ] || stop "AG1 bascule.env ≠ PUBLIC_ENV"
  fi
  echo "contrôles OK ($h) : canon $AVANT → $REF (arbre $TREE) ; minuterie → $TIMER_144"
  [ $APPLY = 1 ] || echo "à blanc : aucun geste (relancer avec --apply)"

  geste $h "reçus $RECUS (0700) et copies avant" \
    "install -d -m 700 $RECUS && cp -p $U/ameesh-staging.timer $C/staging-executor.json $C/config.json $C/bascule.env $SUBMIT $RECUS/ && chmod 600 $RECUS/*"
  echo "GESTE [$h] : archive et reçu du canon vers $RECUS/, contrôle des membres"
  if [ $APPLY = 1 ]; then
    R $h "umask 077; cat > $RECUS/canon.tar.gz" < "$ARCH"
    R $h "umask 077; cat > $RECUS/canon-receipt.json" < "$RECU"
    [ "$(R $h "sha256sum $RECUS/canon.tar.gz" | cut -c1-64)" = "$ASHA" ] || stop "archive altérée en transit"
    R $h "python3 -I -c '
import sys, tarfile
for m in tarfile.open(sys.argv[1]):
    p = m.name.split(\"/\")
    if p[0] != \"canon\" or \"..\" in p or m.name.startswith(\"/\") or not (m.isfile() or m.isdir()):
        sys.exit(\"membre refusé : \" + m.name)' $RECUS/canon.tar.gz" || stop "membre d'archive refusé"
  fi
  P=$H/staging/canon-${REF:0:8}-prepared
  geste $h "extraction sous 980 dans $P" \
    "runuser -u ameesh-staging -- install -d -m 755 $P && runuser -u ameesh-staging -- tar -xzf - -C $P --no-same-owner --no-same-permissions < $RECUS/canon.tar.gz"
  echo "GESTE [$h] : vérification du clone préparé (HEAD, arbre, propre, sans hook, .git/config, fichiers suivis)"
  if [ $APPLY = 1 ]; then
    [ "$(R $h "runuser -u ameesh-staging -- git -C $P/canon rev-parse HEAD 'HEAD^{tree}'" | paste -sd' ')" = "$REF $TREE" ] || stop "HEAD/arbre du clone préparé"
    [ -z "$(R $h "runuser -u ameesh-staging -- git -C $P/canon status --porcelain; find $P/canon/.git/hooks -mindepth 1")" ] || stop "clone sale ou hooks"
    R $h "python3 -I -c '
import hashlib, json, sys
d = json.load(open(sys.argv[1])); root = sys.argv[2]
h = lambda p: hashlib.sha256(open(p, \"rb\").read()).hexdigest()
assert h(root + \"/.git/config\") == d[\"git_config_sha256\"], \".git/config\"
bad = [e[\"path\"] for e in d[\"tracked\"] if h(root + \"/\" + e[\"path\"]) != e[\"sha256\"]]
assert not bad, bad[:3]
print(\"fichiers suivis conformes :\", len(d[\"tracked\"]))' $RECUS/canon-receipt.json $P/canon" || stop "clone préparé ≠ reçu"
  fi
  RET=$H/staging/canon-${AVANT:0:8}-retained-before-${REF:0:8}
  geste $h "bascule du canon (ancien conservé : $RET) et canon_ref=$REF" \
    "runuser -u ameesh-staging -- mv -T $H/staging/canon $RET && runuser -u ameesh-staging -- mv -T $P/canon $H/staging/canon && runuser -u ameesh-staging -- rmdir $P && $(ecrire_json $C/config.json canon_ref "\"$REF\"")"
  echo "GESTE [$h] : minuterie 1.44.1 ($TIMER_144) sous 980, daemon-reload, toujours désactivée"
  if [ $APPLY = 1 ]; then
    printf '%s\n' "$NOUVEAU_TIMER" | R $h "runuser -u ameesh-staging -- sh -c 'umask 022; cat > $U/.ameesh-staging.timer.c3 && mv -f $U/.ameesh-staging.timer.c3 $U/ameesh-staging.timer'"
    R $h "systemctl --user -M ameesh-staging@ daemon-reload"
    [ "$(R $h "sha256sum $U/ameesh-staging.timer" | cut -c1-64)" = $TIMER_144 ] || stop "minuterie posée ≠ d59f9a50"
    [ "$(R $h "systemctl --user -M ameesh-staging@ is-enabled ameesh-staging.timer" || true)" = disabled ] || stop "minuterie activée par erreur"
  fi
  geste $h "arguments = ceux de BG, copie 980 puis $SUBMIT (octets identiques)" \
    "$(ecrire_json $C/staging-executor.json arguments "$ARGS_JSON") && install -m 644 -o root -g root $C/staging-executor.json $SUBMIT.c3.tmp && mv -f $SUBMIT.c3.tmp $SUBMIT && sha256sum $C/staging-executor.json $SUBMIT"
  if [ $h = AG2 ]; then
    # P5 : parité 2A V6. Seule la ligne d'acteur change dans bascule.env ; le
    # résultat doit être PUBLIC_ENV de la garde (1223707f), contrôlé avant renommage.
    geste AG2 "bascule.env : AMEESH_OPERATEUR=human:smichea → ameesh:staging (= PUBLIC_ENV 1223707f), 980:0600" \
      "runuser -u ameesh-staging -- python3 -I -c '
import hashlib, os, sys
p = sys.argv[1]; d = open(p, \"rb\").read()
n = d.replace(b\"AMEESH_OPERATEUR=human:smichea\\n\", b\"AMEESH_OPERATEUR=ameesh:staging\\n\")
assert n != d and hashlib.sha256(n).hexdigest().startswith(\"$ENV_SHA\"), \"PUBLIC_ENV\"
t = p + \".c3.tmp\"; fd = os.open(t, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, \"wb\") as f: f.write(n); f.flush(); os.fsync(f.fileno())
os.replace(t, p)' $C/bascule.env"
    geste AG2 "garde 4bde802e (980 0755) depuis l'archive V6 relue" \
      "runuser -u ameesh-staging -- install -d -m 755 $H/.local/libexec && cat $GUARD_SRC | runuser -u ameesh-staging -- sh -c 'umask 022; cat > $H/.local/libexec/.guard.c3 && chmod 755 $H/.local/libexec/.guard.c3 && mv -f $H/.local/libexec/.guard.c3 $H/.local/libexec/staging-startup-guard.py' && sha256sum $H/.local/libexec/staging-startup-guard.py"
    geste AG2 "drop-in 10-native-actor.conf (ce3ed09c, 980 0644), daemon-reload" \
      "runuser -u ameesh-staging -- install -d -m 755 $U/ameesh-staging.service.d && printf '%s\n' '[Service]' 'Environment=AMEESH_OPERATEUR=ameesh:staging' 'ExecStartPre=/usr/bin/python3.12 -I -B $H/.local/libexec/staging-startup-guard.py' | runuser -u ameesh-staging -- sh -c 'umask 022; cat > $U/ameesh-staging.service.d/10-native-actor.conf' && systemctl --user -M ameesh-staging@ daemon-reload"
    if [ $APPLY = 1 ]; then
      [ "$(R AG2 "sha256sum $C/bascule.env $H/.local/libexec/staging-startup-guard.py $U/ameesh-staging.service.d/10-native-actor.conf" | cut -c1-64 | paste -sd' ')" \
        = "$(R AG1 "sha256sum $C/bascule.env" | cut -c1-64) $GUARD_SHA $DROPIN_SHA" ] || stop "AG2 parité : empreintes ≠ AG1"
      R AG2 "runuser -u ameesh-staging -- env -i AMEESH_OPERATEUR=ameesh:staging /usr/bin/python3.12 -I -B $H/.local/libexec/staging-startup-guard.py" || stop "AG2 : la garde refuse"
    fi
  fi
  [ $APPLY = 1 ] && { "$ICI/sonde-c3-v2.sh" prealables --hotes $h || true; }
  echo "fin prealables $h ; TS=$TS (retour : retour-c3-v2.sh prealables $h $TS)"
fi

if [ "$mode" = activer ]; then
  "$ICI/sonde-c3-v2.sh" go || stop "sonde go KO : pas d'activation (BG porte la coupure)"
  for h in AG1 AG2; do
    RECUS=/root/ameesh-operator-C3-$h-activation-$TS
    [ "$(R $h "sha256sum $RD/10-sans-staging.conf" | cut -c1-16)" = $RDROP_SHA ] || stop "$h drop-in release ≠ 3c213e43"
    [ "$(R $h "systemctl --user -M ameesh@ is-active ameesh-release.service" || true)" != active ] || stop "$h tick de release en cours : réessayer"
    geste $h "reçus $RECUS ; config de soumission (copie de $SUBMIT, ameesh 0600)" \
      "install -d -m 700 $RECUS && test ! -e $SOUMISSION && install -m 600 -o ameesh -g ameesh $SUBMIT $SOUMISSION"
    geste $h "minuterie L2 activée" "systemctl --user -M ameesh-staging@ enable --now ameesh-staging.timer"
    geste $h "drop-in --no-staging de la release retiré (gardé dans $RECUS), daemon-reload du compte ameesh" \
      "mv $RD/10-sans-staging.conf $RECUS/ && systemctl --user -M ameesh@ daemon-reload && systemctl --user -M ameesh@ show ameesh-release.service -p ExecStart --value | grep -c -- --no-staging || true"
  done
  [ $APPLY = 1 ] && "$ICI/sonde-c3-v2.sh" actif
  echo "fin activer ; preuves : sonde-c3-v2.sh apres --depuis $(date -u +%FT%TZ) ; retour : retour-c3-v2.sh activation $TS"
fi
