#!/usr/bin/env bash
# #5495 C3 v2.1 — témoins locaux (sans SSH ni base) du jugement après C3 (evaluer-c3.py).
# Reproduit le témoin d'impl-11 (#200726 B1) qui passait OK à tort en v2 : il doit être KO.
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
# 1. Témoin d'impl-11 : state=failed, porteur hors candidats, mauvais SHA, ancien run ; une levée seule,
#    mauvais principal, zéro drain humain. v2 rendait « verdict : OK » ; v2.1 doit refuser.
attendu 1 "impl-11 #200726 B1" '{"owner":"human:smichea",
 "records":[{"sha":"deadbeef","run":"ancien","state":"failed","host":"pc-x","_at":1794100000,"started":1794100000}],
 "attestations":[],
 "drains":[{"agent":"a1","paused":false,"actor":"ameesh:staging","principal":"human:autre","reason":"drain de déploiement #2152","at":1794200100}]}'
# 2. C3 réussi complet : doit passer.
OKJSON='{"owner":"human:smichea",
 "records":[{"sha":"'$SHA'","run":"r1","state":"running","host":"ameesh-agents-2","_at":1794200010,"started":1794200010},
            {"sha":"'$SHA'","run":"r1","state":"completed","host":"ameesh-agents-2","_at":1794201000,"started":1794200010,
             "canon_ref":"'$CANON'","fingerprints":'$EMP'}],
 "attestations":[{"sha":"'$SHA'"}],
 "drains":[{"agent":"a1","paused":true,"actor":"ameesh:staging","principal":"human:smichea","reason":"drain de déploiement #2152 (vm.sh)","at":1794200100},
           {"agent":"a1","paused":false,"actor":"ameesh:staging","principal":"human:smichea","reason":"drain de déploiement #2152 (vm.sh)","at":1794200400}]}'
attendu 0 "C3 réussi" "$OKJSON"
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
exit $ECHEC
