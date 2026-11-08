#!/bin/bash
# #5495 phase 2B — clé L2 d'AG1 (compte ameesh-staging, uid 980), lancé depuis bureau-goal (root SSH existant).
# Modèle : pose AG2 du 04/11 16:40Z (#190288). Décision #184823 a. Aucun timer/service L2, aucun restart.
# v2 : contrôles stricts (échec SSH = STOP), retour lié au reçu (retour-2B-AG1.sh <TS>).
# Usage : pose-2B-AG1.sh --apply   (sans --apply : contrôles préalables seuls, aucun effet)
set -euo pipefail
AG1=78.232.56.22; CIBLES=("ameesh-agents-1=78.232.56.22" "ameesh-agents-2=78.232.60.1" "ameesh-agents-3=51.15.137.245")
FROM='78.232.56.22,10.77.0.3'; KH_SHA=60d47dceacbb71c569d4054f610004c427aefc61604bb249d25dc2238fbe36b2
TS=$(date -u +%Y%m%dT%H%MZ); COMMENT="ameesh-staging-L2-AG1-5495-${TS%%T*}"; K=/home/ameesh-staging/.ssh/id_ed25519
OPS="/root/ameesh-operator-L2-AG1-key-$TS"; APPLY=0; [ "${1:-}" = --apply ] && APPLY=1
R() { ssh -F none -o BatchMode=yes -o ConnectTimeout=10 "root@$1" "${@:2}"; }
stop() { echo "STOP: $*" >&2; exit 2; }

# 0. Préalables (lecture seule) — STOP au premier écart
R $AG1 "getent passwd ameesh-staging | grep -q '^ameesh-staging:x:980:980:'" || stop "compte 980 absent sur AG1"
R $AG1 "test ! -e $K && test ! -e $K.pub" || stop "clé déjà présente sur AG1"
[ "$(R $AG1 "sha256sum /home/ameesh-staging/.ssh/known_hosts" | cut -c1-64)" = "$KH_SHA" ] || stop "known_hosts AG1 ≠ approuvé"
droits=$(R $AG1 "stat -c '%a %U' /home/ameesh-staging /home/ameesh-staging/.ssh") || stop "lecture droits home/.ssh AG1 impossible"
[ "$droits" = $'700 ameesh-staging\n700 ameesh-staging' ] || stop "droits home/.ssh AG1 : $droits"
for c in "${CIBLES[@]}"; do ip=${c#*=}
  [ "$(R $ip "stat -c '%a %U' /root/.ssh/authorized_keys")" = "600 root" ] || stop "authorized_keys $c pas 600 root"
  R $ip "! grep -q 'ameesh-staging-L2-AG1-' /root/.ssh/authorized_keys" || stop "ligne AG1 déjà présente sur $c"
done
echo "préalables OK"; [ $APPLY = 1 ] || { echo "(contrôle seul, aucun effet)"; exit 0; }

# 1. Clé générée SUR AG1 par l'uid 980 ; la privée n'est jamais lue ni exportée
R $AG1 "runuser -u ameesh-staging -- ssh-keygen -q -t ed25519 -N '' -C '$COMMENT' -f $K && chmod 600 $K && stat -c '%a %U %n' $K $K.pub"
PUB=$(R $AG1 "cat $K.pub"); FP=$(R $AG1 "ssh-keygen -lf $K.pub")
LINE="restrict,from=\"$FROM\" $PUB"; echo "empreinte : $FP ; TS=$TS"

# 2. Partie publique sur chaque cible : sauvegarde, ajout, relecture (mesh-1 reporté, canon §14)
L64=$(printf '%s' "$LINE" | base64 -w0)
for c in "${CIBLES[@]}"; do h=${c%%=*}; ip=${c#*=}
  R $ip "python3 -I - '$h' '$OPS' '$L64'" <<'PY'
import base64, datetime, json, os, shutil, stat, sys
h, ops, l64 = sys.argv[1:4]; line = base64.b64decode(l64).decode(); f = '/root/.ssh/authorized_keys'
os.makedirs(ops, mode=0o700, exist_ok=False); shutil.copy2(f, ops + '/authorized_keys.before')
before = open(f).read().splitlines()
json.dump({"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "host": h, "new_line": line,
           "backup": ops + "/authorized_keys.before", "old_lines_retained": True, "sshd_restart": False,
           "authority": 184823}, open(ops + '/enrollment.json', 'w'), indent=2)
raw = open(f).read(); sep = '' if (not raw or raw.endswith('\n')) else '\n'
with open(f, 'a') as fh: fh.write(sep + line + '\n')
after = open(f).read().splitlines(); st = os.stat(f)
assert after[:len(before)] == before and after[len(before):] == [line], 'ajout inattendu'
assert stat.S_IMODE(st.st_mode) == 0o600 and st.st_uid == 0, 'droits authorized_keys'
print('ok', h)
PY
  echo "posée : $h"
done

# 3. Preuves : ssh root en lecture seule depuis AG1 (uid 980) vers chaque cible, puis refus de lecture de la privée
for c in "${CIBLES[@]}"; do h=${c%%=*}; ip=${c#*=}
  [ "$(R $AG1 "runuser -u ameesh-staging -- ssh -F /home/ameesh-staging/.ssh/config root@$ip id -u")" = 0 ] || stop "preuve SSH $h (retour arrière à jouer)"
  echo "preuve SSH OK : $h"
done
R $AG1 "! runuser -u ameesh -- test -r $K" || stop "ameesh lit la clé privée"
R $AG1 "! runuser -u nobody -- test -r $K" || stop "nobody lit la clé privée"
echo "refus de lecture OK ; pose 2B terminée ; TS=$TS (argument du retour : retour-2B-AG1.sh $TS) ; reçus $OPS"
