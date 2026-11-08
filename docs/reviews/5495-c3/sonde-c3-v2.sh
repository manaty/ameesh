#!/usr/bin/env bash
# #5495 C3 v2 — sonde en LECTURE SEULE sur AG1 ET AG2, lancée depuis bureau-goal.
# Aucun geste : stat, sha256sum, systemctl show/is-*, git rev-parse, SELECT, ssh id -u.
#   ./sonde-c3-v2.sh prealables   préalables P1-P5 posés, rien d'activé (minuteries
#                                 désactivées, drop-in --no-staging en place)
#   ./sonde-c3-v2.sh go           T-15 : prealables + détenteur mesh-design + base
#   ./sonde-c3-v2.sh actif        après « pose-c3-v2.sh --apply activer » : minuteries
#                                 actives, tick de release SANS --no-staging, soumission posée
#   ./sonde-c3-v2.sh apres --depuis 2026-11-09T10:00:00Z   preuves du C3 (cgroup, enregistrement)
# Options : --timer-attendu SHA (défaut 1.44.1/4f9), --hotes "AG1 AG2" (défaut les deux).
# Code retour 0 si toutes les cases sont OK ; chaque KO est imprimé.
set -uo pipefail
declare -A IP=([AG1]=78.232.56.22 [AG2]=78.232.60.1 [AG3]=51.15.137.245) NOM=([AG1]=ameesh-agents-1 [AG2]=ameesh-agents-2)
H=/home/ameesh-staging U=$H/.config/systemd/user C=$H/.config/ameesh
RD=/home/ameesh/.config/systemd/user/ameesh-release.service.d
GUARD_SHA=4bde802e931ab7017934f0ee2f42f07b33b2408905a8aabe3a98063ebeeffdd9
DROPIN_SHA=ce3ed09c7a816b1f02358bd6301a1d8bdde0ee3110228a2fe10ea5e0a5a334cc
SERVICE_SHA=f8add4eb6e5cecdb4c79471a1558f7a3ad30e689ad013456af6e305bf13d8bec
ENV_SHA=1223707f   # PUBLIC_ENV de la garde (8 premiers hexa, comparé en préfixe)
TIMER_144=d59f9a50f6f7572ea95b17d7f21c3e9ac7c12301987cb9e546bf7afa0e3563d6
PY=/home/ameesh/.local/share/ameesh/venv-courant/bin/python
R() { local h=$1; shift; ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@${IP[$h]}" "$@"; }
SQL() { R AG2 "runuser -u ameesh -- env HOME=/home/ameesh $PY -c 'import sys,psycopg;c=psycopg.connect(host=\"10.77.0.1\",user=\"ameesh\",dbname=\"ameesh\");c.read_only=True;[print(*r,sep=\"|\") for r in c.execute(sys.argv[1]).fetchall()]' \"$1\""; }
KO=0
case_() { if [ "$2" = "$3" ]; then echo "OK  $1 ($2)"; else echo "KO  $1 : lu « $2 », attendu « $3 »"; KO=$((KO+1)); fi; }
mode=${1:-prealables}; shift || true
TIMER_ATTENDU=$TIMER_144 DEPUIS= HOTES="AG1 AG2"
while [ $# -gt 0 ]; do case $1 in
  --timer-attendu) TIMER_ATTENDU=$2; shift 2 ;;
  --depuis) DEPUIS=$2; shift 2 ;;
  --hotes) HOTES=$2; shift 2 ;;
  *) echo "argument inconnu : $1"; exit 2 ;; esac; done
echo "sonde C3 v2 #5495 mode=$mode hôtes=$HOTES $(date -u +%FT%TZ)"

hote_prealables() {  # $1 = AG1|AG2
  local h=$1 c980 cref cag
  # N7 : garde, drop-in unique, service, minuterie (empreintes et DropInPaths).
  case_ "$h guard sha256" "$(R $h "sha256sum $H/.local/libexec/staging-startup-guard.py 2>/dev/null" | cut -c1-64)" $GUARD_SHA
  case_ "$h DropInPaths (un seul)" "$(R $h "systemctl --user -M ameesh-staging@ show ameesh-staging.service -p DropInPaths --value")" "$U/ameesh-staging.service.d/10-native-actor.conf"
  case_ "$h drop-in sha256" "$(R $h "sha256sum $U/ameesh-staging.service.d/10-native-actor.conf 2>/dev/null" | cut -c1-64)" $DROPIN_SHA
  case_ "$h dossier drop-in" "$(R $h "ls -A $U/ameesh-staging.service.d 2>/dev/null | tr '\n' ' '")" "10-native-actor.conf "
  case_ "$h FragmentPath" "$(R $h "systemctl --user -M ameesh-staging@ show ameesh-staging.service -p FragmentPath --value")" "$U/ameesh-staging.service"
  case_ "$h service sha256" "$(R $h "sha256sum $U/ameesh-staging.service" | cut -c1-64)" $SERVICE_SHA
  case_ "$h timer sha256" "$(R $h "sha256sum $U/ameesh-staging.timer" | cut -c1-64)" "$TIMER_ATTENDU"
  case_ "$h unités ameesh-staging* (service, timer, .d)" "$(R $h "ls -A $U | grep -c '^ameesh-staging'")" 3
  # Environnement public pinné par la garde, acteur machine, runtime.
  case_ "$h bascule.env 980:0600 = PUBLIC_ENV" "$(R $h "stat -c '%u:%a' $C/bascule.env; sha256sum $C/bascule.env | cut -c1-8" | paste -sd' ')" "980:600 $ENV_SHA"
  case_ "$h garde (lecture) sous 980" "$(R $h "runuser -u ameesh-staging -- env -i AMEESH_OPERATEUR=ameesh:staging /usr/bin/python3.12 -I -B $H/.local/libexec/staging-startup-guard.py >/dev/null 2>&1 && echo 0 || echo refus")" 0
  case_ "$h venv-courant" "$(R $h "readlink $H/.local/share/ameesh/venv-courant")" "${VENV_ATTENDU:-venv-5c1e679}"
  case_ "$h config exécuteur = staging-submit" "$(R $h "sha256sum $C/staging-executor.json /etc/ameesh/staging-submit.json | cut -c1-64 | uniq | wc -l")" 1
  case_ "$h arguments = ceux de BG" "$(R $h "python3 -I -c 'import json;print(json.load(open(\"$C/staging-executor.json\"))[\"arguments\"])'")" \
    "['--sans-poste', '--absent', 'manaty-mesh-1=central runtime deferred; follow up separately', '--rapporteurs', 'bureau mesh-design auditeur']"
  case_ "$h empreintes attendues (fichiers) = réelles" "$(R $h "python3 -I -c '
import hashlib, json, os, sys
d = json.load(open(\"$C/staging-executor.json\"))[\"expected_sha256\"]
ok = all((hashlib.sha256(open(p, \"rb\").read()).hexdigest() if os.path.isfile(p) else None) == v for p, v in d.items() if not os.path.isdir(p))
print(\"fichiers OK\" if ok else \"fichier différent\")'")" "fichiers OK"
  # Canon : copie 980 = canon_ref = canon des agents de l'hôte.
  c980=$(R $h "runuser -u ameesh-staging -- git -C $H/staging/canon rev-parse HEAD")
  cref=$(R $h "python3 -I -c 'import json;print(json.load(open(\"$C/config.json\"))[\"canon_ref\"])'")
  cag=$(R $h "runuser -u ameesh -- git -C /home/ameesh/canon rev-parse HEAD")
  case_ "$h canon 980 = canon_ref" "$c980" "$cref"
  case_ "$h canon_ref = canon des agents" "$cref" "$cag"
  case_ "$h canon 980 propre" "$(R $h "runuser -u ameesh-staging -- git -C $H/staging/canon status --porcelain | wc -l")" 0
  # Accès de déploiement : root sur les trois VM d'agents, clé privée illisible par ameesh.
  for t in AG1 AG2 AG3; do
    case_ "$h→$t ssh root (uid 980)" "$(R $h "runuser -u ameesh-staging -- ssh -F $H/.ssh/config root@${IP[$t]} id -u" 2>/dev/null)" 0
  done
  case_ "$h clé privée illisible par ameesh" "$(R $h "runuser -u ameesh -- test -r $H/.ssh/id_ed25519 && echo lisible || echo refus")" refus
}

hote_etat() {  # $1 hôte, $2 attendu timer (disabled|enabled), $3 drop-in release (present|absent), $4 soumission (absent|present)
  local h=$1
  case_ "$h minuterie L2" "$(R $h "systemctl --user -M ameesh-staging@ is-enabled ameesh-staging.timer")" "$2"
  case_ "$h drop-in --no-staging de la release" "$(R $h "test -e $RD/10-sans-staging.conf && echo present || echo absent")" "$3"
  case_ "$h tick de release avec --no-staging" "$(R $h "systemctl --user -M ameesh@ show ameesh-release.service -p ExecStart --value | grep -c -- --no-staging")" \
    "$([ "$3" = present ] && echo 1 || echo 0)"
  case_ "$h config de soumission (compte ameesh)" "$(R $h "test -e /home/ameesh/.config/ameesh/staging-executor.json && echo present || echo absent")" "$4"
}

if [ "$mode" = prealables ] || [ "$mode" = go ]; then
  for h in $HOTES; do hote_prealables $h; hote_etat $h disabled present absent; done
  case_ "BG : aucune séquence staging active" "$(systemctl --user list-units --type=service --state=active --no-legend 2>/dev/null | grep -c -i staging)" 0
fi
if [ "$mode" = go ]; then
  holder=$(R AG2 "runuser -u ameesh -- env HOME=/home/ameesh bash -lc 'ameesh role status mesh-design' | sed -n '1s/.*holder \([^ ]*\).*/\1/p'")
  case "$holder" in ameesh-agents-1|ameesh-agents-2) echo "OK  détenteur mesh-design = $holder (porteur de ce C3)";;
    *) echo "KO  détenteur mesh-design illisible ou hors candidats : « $holder »"; KO=$((KO+1));; esac
  case_ "migrations non additives inconnues du runtime L2" "$(SQL "select count(*) from schema_non_additive_migrations where version>${MAX_MIGRATION_L2:-544}")" 0
  case_ "demande mesh-staging en cours" "$(R AG1 "runuser -u ameesh -- env HOME=/home/ameesh $PY -c 'from ameesh import config,db,staging_executor as s;d=s.latest(db.connect(config.load(),writer=False),\"mesh-staging\") or {};print(d.get(\"state\") in (\"pending\",\"running\"))'")" False
fi
if [ "$mode" = actif ]; then
  for h in $HOTES; do
    hote_etat $h enabled absent present
    case_ "$h minuterie L2 armée (NextElapse)" "$(R $h "systemctl --user -M ameesh-staging@ show ameesh-staging.timer -p NextElapseUSecMonotonic --value | grep -c -v '^\(0\|infinity\|\)\$'")" 1
  done
fi
if [ "$mode" = apres ]; then
  [ -n "$DEPUIS" ] || { echo "--depuis requis"; exit 2; }
  # Preuve cgroup #5967/0550 : vm.sh n'émet ameesh:staging que dans le cgroup de
  # ameesh-staging.service (preuve_unite_staging) ; aucun drain humain attendu.
  echo "drains ameesh:staging depuis $DEPUIS :"
  SQL "select agent,paused,principal,to_char(created_at,'HH24:MI:SS'),left(reason,80) from operator_pause_events where actor='ameesh:staging' and created_at>='$DEPUIS' order by id" | sed 's/^/  /'
  n=$(SQL "select count(*) from operator_pause_events where actor='ameesh:staging' and created_at>='$DEPUIS'")
  [ "${n:-0}" -gt 0 ] && echo "OK  drains sous ameesh:staging ($n)" || { echo "KO  aucun drain sous ameesh:staging"; KO=$((KO+1)); }
  case_ "drains humains de déploiement dans la fenêtre" "$(SQL "select count(*) from operator_pause_events where actor like 'human:%' and created_at>='$DEPUIS' and reason like 'drain de déploiement%'")" 0
  echo "dernier enregistrement mesh-staging :"
  R AG1 "runuser -u ameesh -- env HOME=/home/ameesh $PY -c 'from ameesh import config,db,staging_executor as s;import json;d=s.latest(db.connect(config.load(),writer=False),\"mesh-staging\") or {};print(json.dumps({k:d.get(k) for k in (\"state\",\"host\",\"sha\",\"run\",\"canon_ref\",\"blocked_reason\",\"fingerprints\")}))'" | sed 's/^/  /'
  for h in $HOTES; do
    echo "journal $h (fin) :"; R $h "journalctl --user -M ameesh-staging@ -u ameesh-staging.service --since '${DEPUIS/T/ }' --no-pager -o short-iso | tail -15" | sed 's/^/  /'
  done
fi
echo "verdict : $([ $KO -eq 0 ] && echo OK || echo "KO ($KO)")"
[ $KO -eq 0 ]
