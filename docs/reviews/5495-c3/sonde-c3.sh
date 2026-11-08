#!/usr/bin/env bash
# #5495 C3 — sonde en LECTURE SEULE, lancée depuis bureau-goal (root SSH existant).
# Aucun geste : stat, sha256sum, systemctl show/is-*, git rev-parse, SELECT.
#   ./sonde-c3.sh prealables [--timer-attendu SHA]   préflight T-15 et sonde N7
#   ./sonde-c3.sh apres --depuis 2026-11-08T22:00:00Z   preuves après le premier C3
# Code retour : 0 si toutes les cases sont OK, 1 sinon (la liste des KO est imprimée).
set -uo pipefail
AG1=78.232.56.22 AG2=78.232.60.1 AG3=51.15.137.245
H=/home/ameesh-staging U=$H/.config/systemd/user
GUARD_SHA=4bde802e931ab7017934f0ee2f42f07b33b2408905a8aabe3a98063ebeeffdd9
DROPIN_SHA=ce3ed09c7a816b1f02358bd6301a1d8bdde0ee3110228a2fe10ea5e0a5a334cc
SERVICE_SHA=f8add4eb6e5cecdb4c79471a1558f7a3ad30e689ad013456af6e305bf13d8bec
TIMER_5C1=f6460989527742e6d9abbe32758f9f3ee417aebfbb722f0c7b85b3d309b0f425
TIMER_144=d59f9a50f6f7572ea95b17d7f21c3e9ac7c12301987cb9e546bf7afa0e3563d6
PY=/home/ameesh/.local/share/ameesh/venv-courant/bin/python
R() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$1" "${@:2}"; }
SQL() {  # requête en lecture, sous ameesh d'AG2 (route existante vers la base)
  R $AG2 "runuser -u ameesh -- env HOME=/home/ameesh $PY -c 'import sys,psycopg;c=psycopg.connect(host=\"10.77.0.1\",user=\"ameesh\",dbname=\"ameesh\");c.read_only=True;[print(*r,sep=\"|\") for r in c.execute(sys.argv[1]).fetchall()]' \"$1\""
}
KO=0
case_() { if [ "$2" = "$3" ]; then echo "OK  $1 ($2)"; else echo "KO  $1 : lu « $2 », attendu « $3 »"; KO=$((KO+1)); fi; }
mode=${1:-prealables}; shift || true
TIMER_ATTENDU=$TIMER_144 DEPUIS=
while [ $# -gt 0 ]; do case $1 in
  --timer-attendu) TIMER_ATTENDU=$2; shift 2 ;;
  --depuis) DEPUIS=$2; shift 2 ;;
  *) echo "argument inconnu : $1"; exit 2 ;; esac; done
echo "sonde C3 #5495 mode=$mode $(date -u +%FT%TZ)"

if [ "$mode" = prealables ]; then
  # N7 : garde et drop-in, empreintes et unicité du drop-in chargé (DropInPaths).
  case_ "AG1 guard sha256" "$(R $AG1 "sha256sum $H/.local/libexec/staging-startup-guard.py" | cut -c1-64)" $GUARD_SHA
  case_ "AG1 DropInPaths (un seul)" "$(R $AG1 "systemctl --user -M ameesh-staging@ show ameesh-staging.service -p DropInPaths --value")" "$U/ameesh-staging.service.d/10-native-actor.conf"
  case_ "AG1 drop-in sha256" "$(R $AG1 "sha256sum $U/ameesh-staging.service.d/10-native-actor.conf" | cut -c1-64)" $DROPIN_SHA
  case_ "AG1 fichiers du dossier drop-in" "$(R $AG1 "ls -A $U/ameesh-staging.service.d | tr '\n' ' '")" "10-native-actor.conf "
  case_ "AG1 FragmentPath" "$(R $AG1 "systemctl --user -M ameesh-staging@ show ameesh-staging.service -p FragmentPath --value")" "$U/ameesh-staging.service"
  case_ "AG1 service sha256" "$(R $AG1 "sha256sum $U/ameesh-staging.service" | cut -c1-64)" $SERVICE_SHA
  case_ "AG1 timer sha256" "$(R $AG1 "sha256sum $U/ameesh-staging.timer" | cut -c1-64)" "$TIMER_ATTENDU"
  case_ "AG1 aucune autre unité ameesh-staging*" "$(R $AG1 "ls -A $U | grep -c '^ameesh-staging'")" 3
  # Compte et environnement de l'exécuteur.
  case_ "AG1 bascule.env 980:0600" "$(R $AG1 "stat -c '%u:%a' $H/.config/ameesh/bascule.env")" "980:600"
  case_ "AG1 AMEESH_OPERATEUR" "$(R $AG1 "grep -c '^AMEESH_OPERATEUR=ameesh:staging\$' $H/.config/ameesh/bascule.env")" 1
  case_ "AG1 venv-courant" "$(R $AG1 "readlink $H/.local/share/ameesh/venv-courant")" venv-5c1e679
  case_ "AG1 config exécuteur = /etc/ameesh/staging-submit.json" \
    "$(R $AG1 "sha256sum $H/.config/ameesh/staging-executor.json /etc/ameesh/staging-submit.json | cut -c1-64 | uniq | wc -l")" 1
  case_ "AG1 --absent manaty-mesh-1 dans arguments" \
    "$(R $AG1 "python3 -I -c 'import json;a=json.load(open(\"$H/.config/ameesh/staging-executor.json\"))[\"arguments\"];print(\"--absent\" in a and any(x.startswith(\"manaty-mesh-1=\") for x in a))'")" True
  # Canon : la copie de l'exécuteur, son canon_ref et le canon des agents.
  c980=$(R $AG1 "runuser -u ameesh-staging -- git -C $H/staging/canon rev-parse HEAD")
  cref=$(R $AG1 "python3 -I -c 'import json;print(json.load(open(\"$H/.config/ameesh/config.json\"))[\"canon_ref\"])'")
  cag=$(R $AG1 "runuser -u ameesh -- git -C /home/ameesh/canon rev-parse HEAD")
  case_ "AG1 canon 980 = canon_ref" "$c980" "$cref"
  case_ "AG1 canon_ref = canon des agents" "$cref" "$cag"
  case_ "AG1 canon 980 propre" "$(R $AG1 "runuser -u ameesh-staging -- git -C $H/staging/canon status --porcelain | wc -l")" 0
  # Accès de déploiement (2B) : root sur les trois VM depuis AG1 sous 980.
  for p in ameesh-agents-1:$AG1 ameesh-agents-2:$AG2 ameesh-agents-3:$AG3; do
    case_ "AG1→${p%%:*} ssh root" "$(R $AG1 "runuser -u ameesh-staging -- ssh -F $H/.ssh/config root@${p#*:} id -u" 2>/dev/null)" 0
  done
  case_ "AG1 clé privée illisible par ameesh" "$(R $AG1 "runuser -u ameesh -- test -r $H/.ssh/id_ed25519 && echo lisible || echo refus")" refus
  # Élection, base, exclusivité.
  case_ "détenteur mesh-design" "$(R $AG2 "runuser -u ameesh -- env HOME=/home/ameesh bash -lc 'ameesh role status mesh-design' | sed -n '1s/.*holder \([^ ]*\).*/\1/p'")" ameesh-agents-1
  case_ "migrations non additives > 0544" "$(SQL "select count(*) from schema_non_additive_migrations where version>544")" 0
  case_ "AG2 timer L2" "$(R $AG2 "systemctl --user -M ameesh-staging@ is-enabled ameesh-staging.timer")" disabled
  case_ "BG : aucune séquence staging active" \
    "$(systemctl --user list-units --type=service --state=active --no-legend 2>/dev/null | grep -c -i staging)" 0
  R $AG1 "systemctl --user -M ameesh-staging@ is-enabled ameesh-staging.timer; systemctl --user -M ameesh-staging@ is-active ameesh-staging.timer ameesh-staging.service" | paste -sd' ' | sed 's/^/INFO AG1 timer enabled|timer actif|service actif : /'
fi

if [ "$mode" = apres ]; then
  [ -n "$DEPUIS" ] || { echo "--depuis requis"; exit 2; }
  # Preuve cgroup #5967/0550 : vm.sh n'émet l'acteur ameesh:staging que si
  # /proc/self/cgroup finit par /ameesh-staging.service (preuve_unite_staging) ;
  # un drain consigné sous cet acteur prouve donc le chemin, et aucun drain
  # humain ne doit apparaître dans la fenêtre.
  echo "drains ameesh:staging depuis $DEPUIS :"
  SQL "select agent,paused,principal,to_char(created_at,'HH24:MI:SS'),left(reason,80) from operator_pause_events where actor='ameesh:staging' and created_at>='$DEPUIS' order by id" | sed 's/^/  /'
  n=$(SQL "select count(*) from operator_pause_events where actor='ameesh:staging' and created_at>='$DEPUIS'")
  [ "${n:-0}" -gt 0 ] && echo "OK  drains sous ameesh:staging ($n)" || { echo "KO  aucun drain sous ameesh:staging"; KO=$((KO+1)); }
  case_ "drains humains dans la fenêtre" "$(SQL "select count(*) from operator_pause_events where actor like 'human:%' and created_at>='$DEPUIS' and reason like 'drain de déploiement%'")" 0
  echo "dernier enregistrement de la demande mesh-staging :"
  R $AG1 "runuser -u ameesh -- env HOME=/home/ameesh $PY -c 'from ameesh import config,db,staging_executor as s;import json;d=s.latest(db.connect(config.load(),writer=False),\"mesh-staging\") or {};print(json.dumps({k:d.get(k) for k in (\"state\",\"host\",\"sha\",\"run\",\"canon_ref\",\"blocked_reason\",\"fingerprints\")}))'" | sed 's/^/  /'
  echo "journal de l'unité (fin) :"
  R $AG1 "journalctl --user -M ameesh-staging@ -u ameesh-staging.service --since '${DEPUIS/T/ }' --no-pager -o short-iso | tail -25" | sed 's/^/  /'
fi
echo "verdict : $([ $KO -eq 0 ] && echo OK || echo "KO ($KO)")"
[ $KO -eq 0 ]
