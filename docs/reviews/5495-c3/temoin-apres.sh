#!/usr/bin/env bash
# #5495 C3 v2.6 — témoins locaux (sans SSH ni base) du jugement après C3 (evaluer-c3.py).
# Reproduit le témoin d'impl-11 (#200726 B1) qui passait OK à tort en v2 : il doit être KO.
# v2.6 (#6763) : la prise se mesure sur le lien d'exécution (#6520) avant la préparation, pas
# sur `running` ; la borne par défaut suit la période réelle du tick L2 (60 s).
set -u
ICI=$(cd "$(dirname "$0")" && pwd)
SHA=97285a076dee285097c412d3bd0f2649438a3257; CANON=b8c4ea12ca5fcac799329dd5ac54386384cbe223
EMP='{"/x/quiescence-staging.sh":"f988","/x/python":"e50d"}'
T0=1794200000
juger() { python3 -I "$ICI/evaluer-c3.py" --sha $SHA --depuis $T0 --canon $CANON \
  --candidats ameesh-agents-1,ameesh-agents-2 --empreintes "$EMP"; }
ECHEC=0
attendu() {  # attendu rc_voulu nom json
  local out rc; out=$(printf '%s' "$3" | juger); rc=$?
  if [ $rc = "$1" ]; then echo "OK   témoin « $2 » rc=$rc"; else echo "ECHEC témoin « $2 » rc=$rc (voulu $1)"; echo "$out" | sed 's/^/     /'; ECHEC=1; fi
}
attendu_nm() {  # nom json : doit être non concluant (rc 0) mais dire « prise non mesurée »
  local out rc; out=$(printf '%s' "$2" | juger); rc=$?
  if [ $rc = 0 ] && printf '%s' "$out" | grep -q "N/M prise L2 non mesurée"; then
    echo "OK   témoin « $1 » rc=0, prise non mesurée"
  else
    echo "ECHEC témoin « $1 » : rc=$rc, ligne non mesurée absente"; echo "$out" | sed 's/^/     /'; ECHEC=1
  fi
}
# 1. Témoin d'impl-11 : state=failed, porteur hors candidats, mauvais SHA, ancien run ; une levée seule,
#    mauvais principal, zéro drain humain. v2 rendait « verdict : OK » ; v2.1 doit refuser.
attendu 1 "impl-11 #200726 B1" '{"owner":"human:smichea",
 "records":[{"sha":"deadbeef","run":"ancien","state":"failed","host":"pc-x","_at":1794100000,"started":1794100000}],
 "attestations":[],
 "drains":[{"agent":"a1","paused":false,"actor":"ameesh:staging","principal":"human:autre","reason":"drain de déploiement #2152","at":1794200100}]}'
# 2. C3 réussi complet : doit passer. La prise (#6520) est à 60 s du submit, `running` à 61 s : les
#    empreintes précèdent la prise, la préparation longue (fetch, CI) est dans `running`.
OKJSON='{"owner":"human:smichea",
 "records":[{"request":"req1","sha":"'$SHA'","run":"r1","state":"running","host":"ameesh-agents-2","_at":1794200061,"started":1794200061},
            {"request":"req1","sha":"'$SHA'","run":"r1","state":"completed","host":"ameesh-agents-2","_at":1794201000,"started":1794200061,
             "canon_ref":"'$CANON'","fingerprints":'$EMP'},
            {"request":"req1","sha":"'$SHA'","state":"pending","_at":1794200000,"deadline":1794200300},
            {"request":"req1","sha":"'$SHA'","state":"pending","_at":1794200060,"execution":"staging-execution:ameesh-agents-2:r1"}],
 "attestations":[{"sha":"'$SHA'","_at":1794200900}],
 "drains":[{"agent":"a1","paused":true,"actor":"ameesh:staging","principal":"human:smichea","reason":"drain de déploiement #2152 (vm.sh)","at":1794200100},
           {"agent":"a1","paused":false,"actor":"ameesh:staging","principal":"human:smichea","reason":"drain de déploiement #2152 (vm.sh)","at":1794200400}]}'
attendu 0 "C3 réussi (prise 60 s)" "$OKJSON"
# 3. Variantes du C3 réussi (mutations JSON valides) qui doivent toutes échouer.
muter() { printf '%s' "$OKJSON" | python3 -I -c "import json,sys; d=json.load(sys.stdin); $1; print(json.dumps(d))"; }
attendu 1 "état partial" "$(muter 'd["records"][1]["state"]="partial"')"
attendu 1 "sans attestation" "$(muter 'd["attestations"]=[]')"
attendu 1 "drain non levé" "$(muter 'd["drains"]=d["drains"][:1]')"
attendu 1 "levée par un autre acteur" "$(muter 'd["drains"][1]["actor"]="human:smichea"')"
attendu 1 "mauvais principal" "$(muter 'd["drains"][0]["principal"]="human:autre"')"
attendu 1 "canon différent" "$(muter 'd["records"][1]["canon_ref"]="ab4ae5b1"')"
attendu 1 "empreinte différente" "$(muter 'd["records"][1]["fingerprints"]["/x/python"]="0000"')"
attendu 1 "porteur hors candidats" "$(muter 'd["records"][1]["host"]="ameesh-agents-3"')"
attendu 1 "deux runs" "$(muter 'd["records"][0]["run"]="r0"')"
attendu 1 "démarrage avant l activation" "$(muter 'd["records"][1]["started"]=1794100000')"
attendu 1 "drain humain dans la fenêtre" "$(muter 'd["drains"].append(dict(d["drains"][0], actor="human:smichea"))')"
attendu 1 "JSON vide" '{}'
# 4. Témoin d'impl-11 sur v2.1 (#200781 B1) : run completed démarré après l'activation, mais la seule
#    attestation du même SHA est ancienne (horodatage 1, run ancien) : doit être KO.
attendu 1 "ancienne attestation même SHA" "$(muter 'd["attestations"]=[{"sha":d["records"][1]["sha"],"_at":1,"run":"ancien"}]')"
attendu 1 "attestation après la fenêtre" "$(muter 'd["attestations"][0]["_at"]=1794201000+121')"
attendu 1 "attestation d un autre run" "$(muter 'd["attestations"][0]["run"]="autre"')"
attendu 0 "attestation du même run" "$(muter 'd["attestations"][0]["run"]="r1"')"
# 5. #6763 : borne de prise. Une prise à 124 s (le chiffre du 10/11 19:13Z) doit être KO ; la même
#    latence portée par `running` après une prise à 60 s reste OK : c'est la forme exacte du 10/11
#    (prise au premier tick, `running` après un `git fetch` d'une minute).
attendu 0 "prise 60 s, running 124 s" "$(muter 'd["records"][0]["_at"]=1794200124; d["records"][0]["started"]=1794200124; d["records"][1]["started"]=1794200124')"
attendu 1 "prise à 124 s" "$(muter 'd["records"][3]["_at"]=1794200124; d["records"][0]["_at"]=1794200129; d["records"][0]["started"]=1794200129; d["records"][1]["started"]=1794200129')"
# Un runtime antérieur à #6520 n'écrit pas le lien d'exécution : non mesurée, jamais un écart.
attendu_nm "prise non mesurée (runtime sans #6520)" "$(muter 'del d["records"][3]')"
attendu_nm "soumission absente des enregistrements" "$(muter 'del d["records"][2]; del d["records"][2]')"
# 6. #6763 : la borne par défaut est dérivée de la période réelle du tick L2. Échoue si la minuterie
#    déployée et la constante du juge divergent.
if ! python3 -I - "$ICI/evaluer-c3.py" "$ICI/../../.." <<'PY'
import importlib.util
import re
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("evaluer_c3", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
unit = Path(sys.argv[2], "deploy/systemd/ameesh-staging.timer")
if not unit.is_file():
    print("N/M garde de dérive de la borne : dépôt absent (%s)" % unit)
    raise SystemExit(0)
match = re.search(r"^OnUnitInactiveSec=(\d+(?:\.\d+)?)s$", unit.read_text(), re.M)
if match is None:
    raise SystemExit("ECHEC garde de dérive : OnUnitInactiveSec absente de %s" % unit)
period = float(match.group(1))
if period != mod.TICK_PERIOD_S:
    raise SystemExit("ECHEC garde de dérive : minuterie %ss, juge %ss"
                     % (period, mod.TICK_PERIOD_S))
if mod.PICKUP_BOUND_S != 1.5 * mod.TICK_PERIOD_S:
    raise SystemExit("ECHEC garde de dérive : borne %ss != 1,5 x %ss"
                     % (mod.PICKUP_BOUND_S, mod.TICK_PERIOD_S))
print("OK   témoin « borne de prise dérivée du tick L2 (%.0f s) »" % mod.TICK_PERIOD_S)
PY
then ECHEC=1; fi
exit $ECHEC
