#!/usr/bin/env bash
# #6002 v4.5 — témoin qui EXERCE l'étape 2 de pose-AG3-vm-operator-v45.sh en conteneur jetable : le bloc
# distant de l'étape 2 est extrait À L'OCTET du script et lancé en root dans l'image du constructeur
# (python3.12 = e50d468e, comme AG3), avec l'archive git de M. Reproduit aussi le défaut v4.4 (« src-… »).
set -u
ICI=$(cd "$(dirname "$0")" && pwd); W=$(mktemp -d); trap 'docker run --rm -v "$W":/w ubuntu@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852 rm -rf /w/ops >/dev/null 2>&1; rm -rf "$W"' EXIT
M=97285a076dee285097c412d3bd0f2649438a3257; IMG=ameesh-l2-builder:p6-97285a0
mkdir -p "$W/ops"; git -C /home/agent/development/manaty/ameesh archive --format=tar --prefix="src-${M:0:12}/" "$M" > "$W/ops/src.tar"
bloc() { awk -v n="$2" '/^R "\$M" "\$OPS" <<.EOS.$/{k++; if(k==n){on=1; next}} on&&/^EOS$/{on=0} on' "$1"; }
bloc "$ICI/pose-AG3-vm-operator-v45.sh" 1 > "$W/etape2-v45.sh"; bloc "$ICI/pose-AG3-vm-operator-v44.sh" 1 > "$W/etape2-v44.sh"
grep -q 'pip" install -q --no-compile "./src-' "$W/etape2-v45.sh" || { echo "ECHEC extraction du bloc v4.5"; exit 1; }
ECHEC=0
docker run --rm --user 0 -v "$W":/w:ro "$IMG" bash -c 'mkdir /ops && cp /w/ops/src.tar /ops/ && bash /w/etape2-v44.sh '$M' /ops' > "$W/v44.out" 2>&1; rc=$?
[ $rc != 0 ] && echo "OK   défaut v4.4 reproduit (rc $rc) : $(grep -o 'No matching distribution[^)]*' "$W/v44.out" | head -1)" || { echo "ECHEC v4.4 n'échoue pas"; ECHEC=1; }
docker run --rm --user 0 -v "$W":/w:ro "$IMG" bash -c 'mkdir /ops && cp /w/ops/src.tar /ops/ && bash /w/etape2-v45.sh '$M' /ops && O=/opt/ameesh-vm-adapter && stat -c "%a %U:%G %n" $O/src-'${M:0:12}' $O/remote/vm-remote.py $O/checks/health.py && readlink $O/venv && $O/venv/bin/python -I -c "import ameesh,sys; print(\"ameesh importé :\", ameesh.__file__.startswith(\"/opt/ameesh-vm-adapter/venv\"), ameesh.__file__)"' > "$W/v45.out" 2>&1; rc=$?
cat "$W/v45.out" | grep -v -E "^\s*$" | tail -8
{ [ $rc = 0 ] && grep -q "ameesh importé : True" "$W/v45.out" && grep -q "^755 root:root /opt/ameesh-vm-adapter/src-" "$W/v45.out"; } \
  && echo "OK   étape 2 v4.5 exercée : venv + pip install ./src-…, ameesh importable, src en 755 root:root, contrôles find passés" || { echo "ECHEC étape 2 v4.5 (rc $rc)"; ECHEC=1; }
exit $ECHEC
