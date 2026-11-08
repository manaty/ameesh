#!/bin/bash
# Retour arrière 2B v2, LIÉ À UNE POSE : retour-2B-AG1.sh <TS>   (TS imprimé par la pose, ex. 20261108T1200Z)
# Par cible : relit /root/ameesh-operator-L2-AG1-key-<TS>/enrollment.json, retire EXACTEMENT sa new_line
# (égalité de ligne complète), vérifie qu'une seule ligne a disparu et que les autres sont intactes,
# écrit de façon atomique (600 root). Cible sans reçu (pose partielle) : rien à retirer, consigné.
# Puis, sur AG1, ne détruit la clé que si sa partie publique est celle de la ligne du reçu.
set -euo pipefail
TS=${1:?usage: retour-2B-AG1.sh <TS de la pose>}
[[ $TS =~ ^[0-9]{8}T[0-9]{4}Z$ ]] || { echo "TS invalide" >&2; exit 2; }
OPS="/root/ameesh-operator-L2-AG1-key-$TS"; K=/home/ameesh-staging/.ssh/id_ed25519; LIGNE_AG1=""
R() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$1" "${@:2}"; }
for c in ameesh-agents-1=78.232.56.22 ameesh-agents-2=78.232.60.1 ameesh-agents-3=51.15.137.245; do h=${c%%=*}; ip=${c#*=}
  out=$(R $ip "python3 -I - '$OPS'" <<'PY'
import json, os, stat, sys, tempfile, datetime
ops = sys.argv[1]; f = '/root/.ssh/authorized_keys'; rec = ops + '/enrollment.json'
if not os.path.isfile(rec):
    print('SANS-RECU'); sys.exit(0)
line = json.load(open(rec))['new_line']
assert line.startswith('restrict,from=') and 'ameesh-staging-L2-AG1-' in line, 'reçu inattendu'
lines = open(f).read().splitlines(); n = lines.count(line)
if n == 0:
    print('ABSENTE', line.split()[-1]); sys.exit(0)
assert n == 1, 'ligne présente %d fois' % n
new = [l for l in lines if l != line]
assert len(new) == len(lines) - 1 and len(new) >= 1, 'retrait incohérent'
st = os.stat(f); assert st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o600
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
open(ops + '/authorized_keys.avant-retour-' + stamp, 'w').write('\n'.join(lines) + '\n')
fd, tmp = tempfile.mkstemp(dir='/root/.ssh'); os.fchmod(fd, 0o600)
with os.fdopen(fd, 'w') as fh: fh.write('\n'.join(new) + '\n')
os.replace(tmp, f)
assert open(f).read().splitlines() == new
print('RETIREE', line.split(' ', 1)[1])
PY
) || { echo "STOP retour $h : $out" >&2; exit 2; }
  echo "$h : ${out%% *}"
  [ "$h" = ameesh-agents-1 ] && LIGNE_AG1=$(R 78.232.56.22 "python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1]))[\"new_line\"])' $OPS/enrollment.json 2>/dev/null" || true)
done
if R 78.232.56.22 "test -e $K"; then
  pub=$(R 78.232.56.22 "awk '{print \$1, \$2}' $K.pub")
  [ -n "$LIGNE_AG1" ] && [ "$(awk '{print $2, $3}' <<<"$LIGNE_AG1")" = "$pub" ] || { echo "STOP : clé AG1 ≠ ligne du reçu $TS, non détruite" >&2; exit 2; }
  R 78.232.56.22 "shred -u $K && rm -f $K.pub"; echo "clé AG1 détruite (liée au reçu $TS)"
else echo "clé AG1 absente"; fi
echo "retour 2B $TS fait"
