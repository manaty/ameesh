#!/usr/bin/env bash
# #5495 C3 — prépare, SUR bureau-goal seulement, la copie qualifiée du canon
# (manaty/home) que l'exécuteur L2 lira : même forme que canon-ab4 de la phase 2A
# (clone complet, aucun hook, origin = forge publique), archive + reçu.
# Aucun geste d'hôte. Usage : ./preparer-canon-c3.sh <commit 40 hex> <dossier de sortie>
set -euo pipefail
REF=${1:?commit du canon (40 hex) requis}; OUT=${2:?dossier de sortie requis}
[[ $REF =~ ^[0-9a-f]{40}$ ]] || { echo "commit exact requis"; exit 2; }
SRC=/home/agent/development/manaty/home
git -C "$SRC" fetch -q origin
git -C "$SRC" merge-base --is-ancestor "$REF" origin/main || { echo "$REF hors de origin/main"; exit 1; }
mkdir -p "$OUT"; W=$(mktemp -d "$OUT/.travail.XXXXXX")
git clone -q --no-local --no-hardlinks "$SRC" "$W/canon"
git -C "$W/canon" fetch -q "$SRC" "+refs/remotes/origin/main:refs/remotes/origin/main"
git -C "$W/canon" -c advice.detachedHead=false checkout -q --detach "$REF"
git -C "$W/canon" remote set-url origin https://github.com/manaty/home.git
find "$W/canon/.git/hooks" -mindepth 1 -delete
[ -z "$(git -C "$W/canon" status --porcelain)" ] || { echo "clone sale"; exit 1; }
[ -z "$(find "$W/canon" -type l)" ] || { echo "lien symbolique dans le canon"; exit 1; }
A="$OUT/canon-${REF:0:8}.tar.gz"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='2026-01-01 00:00Z' \
    -C "$W" -czf "$A" canon
python3 -I - "$W/canon" "$REF" "$A" > "$OUT/canon-${REF:0:8}-receipt.json" <<'PY'
import hashlib, json, subprocess, sys
clone, ref, archive = sys.argv[1:4]
def digest(p):
    return hashlib.sha256(open(p, 'rb').read()).hexdigest()
git = lambda *a: subprocess.run(['git', '-C', clone, *a], check=True,
                                capture_output=True, text=True).stdout.strip()
files = [p for p in git('ls-files', '-z').split('\0') if p]
print(json.dumps({
    'schema': 'ameesh-5495-c3-canon/1', 'ref': ref, 'tree': git('rev-parse', 'HEAD^{tree}'),
    'head': git('rev-parse', 'HEAD'), 'archive_sha256': digest(archive),
    'git_config_sha256': digest(clone + '/.git/config'),
    'tracked': [{'path': p, 'sha256': digest(clone + '/' + p)} for p in sorted(files)],
}, indent=1, sort_keys=True))
PY
rm -rf "$W"
sha256sum "$A" "$OUT/canon-${REF:0:8}-receipt.json"
