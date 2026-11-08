#!/usr/bin/env bash
# #5495 C3 v2.1 — sonde en LECTURE SEULE, garde dure, AG1 et AG2, lancée depuis bureau-goal.
# Toute lecture en échec est un KO (jamais un faux « absent » ou « 0 »). Aucun geste.
#   ./sonde-c3-v21.sh prealables [--hotes "AG1 AG2"] [--timer-attendu SHA]
#        N7 + configuration + canon + accès, minuterie L2 disabled/inactive, drop-in --no-staging
#        de la release présent, pas de soumission ; aucune dérogation.
#   ./sonde-c3-v21.sh prealables --sans-detenteur     identique (prealables ne lit pas le détenteur ;
#        option acceptée pour l'appel à T-60 par pose-c3-v21.sh, sans effet sur les cases)
#   ./sonde-c3-v21.sh go          T-15 : prealables des deux hôtes + détenteur candidat + base
#        (migrations non additives inconnues du runtime L2, in_flight vide, aucune demande en cours)
#        + dernière release ameesh taguée attestée + tick de release inactif sans job
#   ./sonde-c3-v21.sh actif       après activation : N7 + minuteries L2 actives et armées, tick sans
#        --no-staging, soumission posée, sur les deux hôtes
#   ./sonde-c3-v21.sh apres --sha SHA --depuis EPOCH --canon CANON
#        N7 rejoué sur les deux hôtes (état activé) + preuve rattachée à la demande C3 (evaluer-c3.py)
set -uo pipefail
ICI=$(cd "$(dirname "$0")" && pwd)
declare -A IP=([AG1]=78.232.56.22 [AG2]=78.232.60.1 [AG3]=51.15.137.245)
H=/home/ameesh-staging U=$H/.config/systemd/user C=$H/.config/ameesh
RD=/home/ameesh/.config/systemd/user/ameesh-release.service.d
SUBMIT=/etc/ameesh/staging-submit.json SOUMISSION=/home/ameesh/.config/ameesh/staging-executor.json
GUARD_SHA=4bde802e931ab7017934f0ee2f42f07b33b2408905a8aabe3a98063ebeeffdd9
DROPIN_SHA=ce3ed09c7a816b1f02358bd6301a1d8bdde0ee3110228a2fe10ea5e0a5a334cc
SERVICE_SHA=f8add4eb6e5cecdb4c79471a1558f7a3ad30e689ad013456af6e305bf13d8bec
ENV_SHA=1223707fe4a80f5627744f969f4bbbc1662ac859dfb0aab9f2ef34e08c919fa1   # PUBLIC_ENV de la garde 4bde802e
TIMER_144=d59f9a50f6f7572ea95b17d7f21c3e9ac7c12301987cb9e546bf7afa0e3563d6
ARGS="['--sans-poste', '--absent', 'manaty-mesh-1=central runtime deferred; follow up separately', '--rapporteurs', 'bureau mesh-design auditeur']"
PY=/home/ameesh/.local/share/ameesh/venv-courant/bin/python
KO=0
# lire <hôte> <commande> : sortie de la commande, ou « ERREUR-LECTURE(rc) » si ssh ou la commande échoue.
lire() { local h=$1 out rc; shift; out=$(ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@${IP[$h]}" "$@" 2>/dev/null); rc=$?
  [ $rc -eq 0 ] && printf '%s' "$out" || printf 'ERREUR-LECTURE(%s)' $rc; }
case_() { if [ "$2" = "$3" ]; then echo "OK  $1 ($2)"; else echo "KO  $1 : lu « $2 », attendu « $3 »"; KO=$((KO+1)); fi; }
lecteur() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@${IP[AG2]}" \
  "runuser -u ameesh -- env HOME=/home/ameesh $PY - $*" < "$ICI/lecteur-c3.py" 2>/dev/null; }

n7() {  # contrôles d'octets et d'unités, identiques avant et après C3 (avis impl-11 B2)
  local h=$1
  case_ "$h garde sha256" "$(lire $h "sha256sum $H/.local/libexec/staging-startup-guard.py | cut -c1-64")" $GUARD_SHA
  case_ "$h DropInPaths (un seul)" "$(lire $h "systemctl --user -M ameesh-staging@ show ameesh-staging.service -p DropInPaths --value")" "$U/ameesh-staging.service.d/10-native-actor.conf"
  case_ "$h drop-in sha256" "$(lire $h "sha256sum $U/ameesh-staging.service.d/10-native-actor.conf | cut -c1-64")" $DROPIN_SHA
  case_ "$h dossier drop-in" "$(lire $h "ls -A $U/ameesh-staging.service.d | tr '\n' ' '")" "10-native-actor.conf "
  case_ "$h FragmentPath" "$(lire $h "systemctl --user -M ameesh-staging@ show ameesh-staging.service -p FragmentPath --value")" "$U/ameesh-staging.service"
  case_ "$h service sha256" "$(lire $h "sha256sum $U/ameesh-staging.service | cut -c1-64")" $SERVICE_SHA
  case_ "$h minuterie sha256" "$(lire $h "sha256sum $U/ameesh-staging.timer | cut -c1-64")" "$TIMER_ATTENDU"
  case_ "$h unités ameesh-staging* (service, timer, .d)" "$(lire $h "ls -A $U | grep -c '^ameesh-staging'")" 3
  case_ "$h bascule.env 980:0600 = PUBLIC_ENV" "$(lire $h "stat -c '%u:%a' $C/bascule.env; sha256sum $C/bascule.env | cut -c1-64" | paste -sd' ')" "980:600 $ENV_SHA"
  case_ "$h garde (lecture) sous 980" "$(lire $h "runuser -u ameesh-staging -- env -i AMEESH_OPERATEUR=ameesh:staging /usr/bin/python3.12 -I -B $H/.local/libexec/staging-startup-guard.py && echo 0")" 0
}

config_canon_acces() {
  local h=$1 c980 cref cag vexp
  case_ "$h deux fichiers de config présents" "$(lire $h "test -f $C/staging-executor.json && test -f $SUBMIT && echo 2")" 2
  case_ "$h config exécuteur = staging-submit (2 empreintes égales)" "$(lire $h "a=\$(sha256sum < $C/staging-executor.json); b=\$(sha256sum < $SUBMIT); [ \"\$a\" = \"\$b\" ] && echo egales")" egales
  case_ "$h arguments = ceux de BG" "$(lire $h "python3 -I -c 'import json;print(json.load(open(\"$C/staging-executor.json\"))[\"arguments\"])'")" "$ARGS"
  # venv attendu : celui que nomme expected_sha256 (5c1 ou kit P6), jamais une valeur figée ici.
  vexp=$(lire $h "python3 -I -c 'import json,re;d=json.load(open(\"$C/staging-executor.json\"))[\"expected_sha256\"];print(sorted({m.group(1) for k in d for m in [re.search(\"/(venv-[0-9a-f]+)/\",k)] if m})[0])'")
  case_ "$h venv-courant = venv des empreintes attendues" "$(lire $h "readlink $H/.local/share/ameesh/venv-courant")" "$vexp"
  case_ "$h empreintes attendues (fichiers ET arbres) = réelles" "$(lire $h "runuser -u ameesh-staging -- $H/.local/share/ameesh/venv/bin/python -I -c '
import json, os
from ameesh.staging_executor import _sha256, _tree_sha256
d = json.load(open(\"$C/staging-executor.json\"))[\"expected_sha256\"]
bad = [p for p, v in d.items() if (_tree_sha256(p) if os.path.isdir(p) else _sha256(p)) != v]
print(\"conformes %d\" % len(d) if not bad else \"écarts %s\" % bad)'")" "conformes 3"
  c980=$(lire $h "runuser -u ameesh-staging -- git -C $H/staging/canon rev-parse HEAD")
  cref=$(lire $h "python3 -I -c 'import json;print(json.load(open(\"$C/config.json\"))[\"canon_ref\"])'")
  cag=$(lire $h "runuser -u ameesh -- git -C /home/ameesh/canon rev-parse HEAD")
  case_ "$h canon 980 = canon_ref" "$c980" "$cref"
  case_ "$h canon_ref = canon des agents" "$cref" "$cag"
  case_ "$h canon 980 propre (rc git vérifié)" "$(lire $h "s=\$(runuser -u ameesh-staging -- git -C $H/staging/canon status --porcelain) && [ -z \"\$s\" ] && echo propre")" propre
  for t in AG1 AG2 AG3; do
    case_ "$h→$t ssh root (uid 980)" "$(lire $h "runuser -u ameesh-staging -- ssh -F $H/.ssh/config root@${IP[$t]} id -u")" 0
  done
  case_ "$h runuser ameesh opérant (témoin positif)" "$(lire $h "runuser -u ameesh -- id -u")" 1001
  case_ "$h clé privée illisible par ameesh" "$(lire $h "if runuser -u ameesh -- test -r $H/.ssh/id_ed25519; then echo lisible; else echo refus; fi")" refus
}

etat() {  # $1 hôte, $2 avant|active
  local h=$1
  if [ "$2" = avant ]; then
    case_ "$h minuterie L2 is-enabled" "$(lire $h "systemctl --user -M ameesh-staging@ is-enabled ameesh-staging.timer; true")" disabled
    case_ "$h minuterie L2 is-active" "$(lire $h "systemctl --user -M ameesh-staging@ show ameesh-staging.timer -p ActiveState --value")" inactive
    case_ "$h service L2 inactif" "$(lire $h "systemctl --user -M ameesh-staging@ show ameesh-staging.service -p ActiveState --value")" inactive
    case_ "$h drop-in --no-staging présent" "$(lire $h "test -f $RD/10-sans-staging.conf && echo present")" present
    case_ "$h tick de release avec --no-staging" "$(lire $h "systemctl --user -M ameesh@ show ameesh-release.service -p ExecStart --value | grep -c -- --no-staging")" 1
    case_ "$h pas de config de soumission" "$(lire $h "if [ -e $SOUMISSION ]; then echo present; else echo absent; fi")" absent
  else
    case_ "$h minuterie L2 is-enabled" "$(lire $h "systemctl --user -M ameesh-staging@ is-enabled ameesh-staging.timer")" enabled
    case_ "$h minuterie L2 active" "$(lire $h "systemctl --user -M ameesh-staging@ show ameesh-staging.timer -p ActiveState --value")" active
    case_ "$h minuterie L2 armée" "$(lire $h "systemctl --user -M ameesh-staging@ show ameesh-staging.timer -p NextElapseUSecMonotonic --value | grep -vcx '0\|infinity\|'")" 1
    case_ "$h drop-in --no-staging absent" "$(lire $h "if [ -e $RD/10-sans-staging.conf ]; then echo present; else echo absent; fi")" absent
    case_ "$h ExecStart sans --no-staging" "$(lire $h "if systemctl --user -M ameesh@ show ameesh-release.service -p ExecStart --value | grep -q -- --no-staging; then echo avec; else echo sans; fi")" sans
    case_ "$h config de soumission = staging-submit" "$(lire $h "a=\$(sha256sum < $SOUMISSION); b=\$(sha256sum < $SUBMIT); [ \"\$a\" = \"\$b\" ] && stat -c '%U:%a' $SOUMISSION")" "ameesh:600"
  fi
}

bg_sans_sequence() {
  local n
  n=$(XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus \
      systemctl --user list-units --type=service --state=active --no-legend 2>/dev/null) || n="ERREUR-LECTURE(bus)"
  case "$n" in ERREUR-LECTURE*) case_ "BG : bus utilisateur lisible" "$n" lisible;;
    *) case_ "BG : aucune séquence staging active" "$(printf '%s\n' "$n" | grep -ci staging)" 0;; esac
}

[ "${SONDE_BIBLIO:-}" = 1 ] && return 0 2>/dev/null
mode=${1:-prealables}; shift || true
TIMER_ATTENDU=$TIMER_144 HOTES="AG1 AG2" SHA= DEPUIS= CANON=
while [ $# -gt 0 ]; do case $1 in
  --timer-attendu) TIMER_ATTENDU=$2; shift 2 ;;
  --hotes) HOTES=$2; shift 2 ;;
  --sans-detenteur) shift ;;
  --sha) SHA=$2; shift 2 ;;
  --depuis) DEPUIS=$2; shift 2 ;;
  --canon) CANON=$2; shift 2 ;;
  *) echo "argument inconnu : $1"; exit 2 ;; esac; done
echo "sonde C3 v2.1 #5495 mode=$mode hôtes=$HOTES $(date -u +%FT%TZ)"

case $mode in
prealables|go)
  for h in $HOTES; do n7 $h; config_canon_acces $h; etat $h avant; done
  bg_sans_sequence ;;&
go)
  [ "$HOTES" = "AG1 AG2" ] || { echo "KO  go exige les deux hôtes"; KO=$((KO+1)); }
  # Dernière release ameesh taguée (rapports des deux candidats), puis son attestation sans effet de bord.
  rel=$( { for h in AG1 AG2; do lire $h "python3 -I -c '
import glob, json
for f in glob.glob(\"/home/ameesh/.local/state/ameesh/release/reports/*.json\"):
    d = json.load(open(f))
    if d.get(\"project\") == \"ameesh\" and d.get(\"release_sha\") and d.get(\"tag\"):
        print(d.get(\"completed_at\") or 0, d[\"release_sha\"], d[\"tag\"])'"; echo; done; } | sort -n | tail -1)
  rsha=$(echo "$rel" | awk '{print $2}')
  [[ $rsha =~ ^[0-9a-f]{40}$ ]] && echo "OK  dernière release ameesh taguée : $(echo "$rel" | awk '{print $3}') $rsha" \
    || { echo "KO  dernière release ameesh illisible : « $rel »"; KO=$((KO+1)); rsha=0000000000000000000000000000000000000000; }
  j=$(lecteur go "$rsha") || j=
  val() { printf '%s' "$j" | python3 -I -c "import json,sys; d=json.load(sys.stdin); print($1)" 2>/dev/null || echo ERREUR-LECTURE; }
  holder=$(val 'd["holder"]')
  case "$holder" in ameesh-agents-1|ameesh-agents-2) echo "OK  détenteur mesh-design = $holder (porteur)";;
    *) echo "KO  détenteur hors candidats ou illisible : « $holder »"; KO=$((KO+1));; esac
  case_ "staging_watch.in_flight vide" "$(val 'len(d["in_flight"])')" 0
  case_ "historique de séquences non tronqué" "$(val 'd["history_truncated"]')" False
  case_ "aucune demande mesh-staging en cours" "$(val 'd["request_state"] in ("pending","running")')" False
  case_ "release $rsha attestée (staging.attested)" "$(val 'bool(d["release_attested"])')" True
  case_ "actif-${rsha:0:12}.json sur AG1" "$(lire AG1 "test -f /home/ameesh/.local/state/ameesh/mise-a-jour/actif-${rsha:0:12}.json && echo present")" present
  for h in AG1 AG2; do
    # Migrations non additives inconnues du runtime L2 : borne = plus haute migration du venv 980 (5c1 ou P6).
    max=$(lire $h "ls $H/.local/share/ameesh/venv-courant/lib/python3.12/site-packages/ameesh/migrations | grep -o '^[0-9]\{4\}' | sort -n | tail -1")
    [[ $max =~ ^[0-9]{4}$ ]] || max=ERREUR
    case_ "$h migrations non additives > $max (inconnues du runtime L2)" "$(lire AG2 "runuser -u ameesh -- env HOME=/home/ameesh $PY -c 'import psycopg;c=psycopg.connect(host=\"10.77.0.1\",user=\"ameesh\",dbname=\"ameesh\");print(c.execute(\"select count(*) from schema_non_additive_migrations where version>%s\",(int(\"$max\"),)).fetchone()[0])'")" 0
    case_ "$h tick de release inactif" "$(lire $h "systemctl --user -M ameesh@ show ameesh-release.service -p ActiveState --value")" inactive
    case_ "$h aucun job de release en file" "$(lire $h "systemctl --user -M ameesh@ list-jobs --no-legend | grep -c ameesh-release; true")" 0
  done ;;
actif)
  for h in $HOTES; do n7 $h; etat $h active; done ;;
apres)
  [ -n "$SHA" ] && [ -n "$DEPUIS" ] && [ -n "$CANON" ] || { echo "apres exige --sha --depuis --canon"; exit 2; }
  for h in $HOTES; do n7 $h; etat $h active; done
  emp=$(lire AG1 "python3 -I -c 'import json;print(json.dumps(json.load(open(\"$C/staging-executor.json\"))[\"expected_sha256\"]))'")
  j=$(lecteur apres "$SHA" "$DEPUIS") || j='{}'
  printf '%s' "$j" | python3 -I "$ICI/evaluer-c3.py" --sha "$SHA" --depuis "$DEPUIS" --canon "$CANON" \
    --candidats ameesh-agents-1,ameesh-agents-2 --empreintes "$emp" || KO=$((KO+1)) ;;
*) [[ $mode =~ ^(prealables|go)$ ]] || { echo "mode inconnu $mode"; exit 2; } ;;
esac
echo "verdict : $([ $KO -eq 0 ] && echo OK || echo "KO ($KO)")"
[ $KO -eq 0 ]
