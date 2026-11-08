#!/bin/bash
# #6002 v4.6 — mise à jour de l'adaptateur DÉJÀ posé (v4.5, M = 97285a07) vers M' = 242ced700d6e (#6329 :
# vm-remote.py n'accède plus à agent_mailbox, il appelle vm_probe_receipt et vm_probe_send). Lancé
# depuis BG. Ordre (#201411) : CETTE mise à jour, puis pose-DB-vm_probe-v46.sh, puis tests B (v46).
#   ./maj-adaptateur-AG3-v46.sh                 à blanc : vérification distante + pip --dry-run ./src-<M'>
#   ./maj-adaptateur-AG3-v46.sh --apply         reçu avant, code et venv de M', bascule, reçu après
#   ./maj-adaptateur-AG3-v46.sh --apply --retour  lien et fichiers de M remis (M' conservé)
set -euo pipefail
ICI=$(cd "$(dirname "$0")" && pwd)
AG3=51.15.137.245
M=97285a076dee285097c412d3bd0f2649438a3257; N=242ced700d6ec1a63fc9d72643bc9faad175f72f
SRC=/home/agent/development/manaty/ameesh; DIST="$ICI/maj-adaptateur-distant-v46.sh"
APPLY=0 RETOUR=0
while [ $# -gt 0 ]; do case $1 in --apply) APPLY=1;; --retour) RETOUR=1;; *) echo "argument $1"; exit 2;; esac; shift; done
stop() { echo "STOP: $*" >&2; exit 2; }
D() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$AG3" "bash -s -- $*" < "$DIST"; }
git -C "$SRC" cat-file -e "$N^{commit}" && git -C "$SRC" merge-base --is-ancestor "$N" origin/main || stop "M' absent du clone ou hors de main"
[ "$(sha256sum < "$DIST" | cut -c1-64)" = "$(awk '$2 ~ /maj-adaptateur-distant-v46.sh$/ {print $1}' "$ICI/SHA256SUMS-v46")" ] || stop "partie distante ≠ SHA256SUMS-v46"
TS=$(date -u +%Y%m%dT%H%M%SZ)-$$; OPS=/root/ameesh-operator-6002-AG3-maj-$TS
if [ $RETOUR = 1 ]; then
  [ $APPLY = 1 ] || { echo "(retour à blanc : rien)"; exit 0; }
  ssh -F none -o BatchMode=yes "root@$AG3" "install -d -m 700 $OPS"
  D retour "$M" "$N" "$OPS"; exit 0
fi
D verifier "$M" "$N" /nonexistent
if [ $APPLY = 0 ]; then
  # Essai à blanc de l'étape pip, comme v4.5 : venv jetable sous /tmp, retiré, rien sous /opt.
  TARB=$(mktemp "${TMPDIR:-/tmp}/6002-maj-blanc.XXXXXX"); trap 'rm -f "$TARB"' EXIT
  git -C "$SRC" archive --format=tar --prefix="src-${N:0:12}/" "$N" > "$TARB"
  BLANC='set -euo pipefail; S12=${1:0:12}; T=$(mktemp -d /tmp/6002-blanc.XXXXXX); trap "rm -rf \"$T\"" EXIT
tar -C "$T" -xf -; cd "$T"; python3.12 -m venv venv
venv/bin/pip install -q --dry-run --no-deps --no-compile "./src-$S12"
echo "à blanc : pip accepte ./src-$S12 (python3.12, venv jetable retiré)"'
  ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$AG3" bash -c "$(printf %q "$BLANC")" _ "$N" < "$TARB" || stop "pip --dry-run refusé"
  echo "(contrôle seul, aucun effet hors du répertoire temporaire)"; exit 0
fi
TAR=$(mktemp "${TMPDIR:-/tmp}/6002-maj.XXXXXX"); trap 'rm -f "$TAR"' EXIT
git -C "$SRC" archive --format=tar --prefix="src-${N:0:12}/" "$N" > "$TAR"
ssh -F none -o BatchMode=yes "root@$AG3" "install -d -m 700 $OPS && umask 077 && cat > $OPS/src.tar" < "$TAR"
D appliquer "$M" "$N" "$OPS"
echo "TS=$TS (reçus $OPS ; retour : ./maj-adaptateur-AG3-v46.sh --apply --retour). Suite : pose-DB-vm_probe-v46.sh --apply 20261108T141112Z-2455349"
