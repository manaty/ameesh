#!/usr/bin/env bash
# #6002 — bloc B2 : réinstalle l'adaptateur VM de bureau-goal à 242ced700d6e (= main 14017a2a pour
# les fichiers de l'adaptateur), pose scaleway.json depuis le profil scw existant et les unités.
# N'ACTIVE RIEN : enrollment.json reste enabled:false, le timer reste désactivé.
# À lancer par l'owner, en root, sur bureau-goal :  sudo bash reinstall-242ced70.sh
# Arrêt au premier écart. Aucun secret affiché.
set -euo pipefail
SHA=242ced700d6ec1a63fc9d72643bc9faad175f72f
SHORT=${SHA:0:12}
REPO=/home/agent/development/manaty/ameesh
OPT=/opt/ameesh-vm-adapter
ETC=/etc/ameesh-vm-adapter
UNITS=/home/agent/.local/state/ameesh/operators/6002-activation
OP=agent
stop() { echo "STOP : $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || stop "root requis"
[ "$(hostname)" = bureau-goal ] || stop "bureau-goal seulement"
systemctl is-active --quiet ameesh-vm-adapter.timer 2>/dev/null && stop "timer actif : rien à réinstaller sous un timer actif"
python3 -I -c 'import json,sys; assert json.load(open(sys.argv[1]))["enabled"] is False' $ETC/enrollment.json \
  || stop "enrollment.json n'est pas enabled:false"
[ ! -e $OPT/src-$SHORT ] && [ ! -e $OPT/venv-$SHORT ] || stop "src-$SHORT ou venv-$SHORT déjà présents : réconcilier"

# 1. Sources : archive git exacte, blobs de l'adaptateur vérifiés contre l'arbre du commit.
[ "$(runuser -u $OP -- git -C $REPO rev-parse "$SHA^{commit}")" = $SHA ] || stop "commit $SHA absent du clone"
install -d -m 0755 -o root -g root $OPT/src-$SHORT
runuser -u $OP -- git -C $REPO archive --format=tar $SHA | tar -x -C $OPT/src-$SHORT --no-same-owner
chown -R root:root $OPT/src-$SHORT; chmod -R go-w $OPT/src-$SHORT
for f in deploy/vm-adapter/commandes/vm-commande.py deploy/vm-adapter/ameesh-vm-adapter.service \
         deploy/vm-adapter/ameesh-vm-adapter.timer src/ameesh/vm_adapter.py; do
  want=$(runuser -u $OP -- git -C $REPO rev-parse "$SHA:$f")
  got=$(git hash-object "$OPT/src-$SHORT/$f")
  [ "$want" = "$got" ] || stop "blob $f : $got ≠ $want"
done
[ -z "$(find $OPT/src-$SHORT -perm /022 -print -quit)" ] || stop "fichier g+w/o+w dans les sources"

# 2. venv dédié, source installée, import vérifié sous le compte opérateur.
python3.12 -m venv $OPT/venv-$SHORT
$OPT/venv-$SHORT/bin/pip install --quiet --no-cache-dir $OPT/src-$SHORT
chown -R root:root $OPT/venv-$SHORT; chmod -R go-w $OPT/venv-$SHORT
runuser -u $OP -- $OPT/venv-$SHORT/bin/python -I -c 'import ameesh.vm_adapter, ameesh.host_release; print("import ok")'

# 3. Commande par VM au SHA, puis bascule des liens (anciens gardés pour le retour).
install -m 0644 -o root -g root $OPT/src-$SHORT/deploy/vm-adapter/commandes/vm-commande.py $OPT/commandes/vm-commande.py.$SHORT
[ "$(git hash-object $OPT/commandes/vm-commande.py.$SHORT)" = "$(runuser -u $OP -- git -C $REPO rev-parse "$SHA:deploy/vm-adapter/commandes/vm-commande.py")" ] || stop "vm-commande.py copié ≠ blob"
OLD_SRC=$(readlink $OPT/src); OLD_VENV=$(readlink $OPT/venv)
cp -p $OPT/commandes/vm-commande.py $OPT/commandes/vm-commande.py.avant-$SHORT
mv -f $OPT/commandes/vm-commande.py.$SHORT $OPT/commandes/vm-commande.py
ln -sfn src-$SHORT $OPT/src; ln -sfn venv-$SHORT $OPT/venv
echo "liens : src $OLD_SRC → src-$SHORT ; venv $OLD_VENV → venv-$SHORT (retour : ln -sfn $OLD_SRC $OPT/src ; ln -sfn $OLD_VENV $OPT/venv ; mv $OPT/commandes/vm-commande.py.avant-$SHORT $OPT/commandes/vm-commande.py)"

# 4. scaleway.json dérivé du profil scw existant du détenteur (compte agent), sans affichage.
echo "profil scw source : $(stat -c '%y %U:%G %a' /home/$OP/.config/scw/config.yaml) (date de la copie = référence de péremption si la clé tourne)"
if [ ! -e $ETC/scaleway.json ]; then
  umask 077
  python3 -I - /home/$OP/.config/scw/config.yaml $ETC/scaleway.json <<'PY'
import json, os, re, sys
src, dst = sys.argv[1], sys.argv[2]
m = re.search(r'^\s*secret_key:\s*"?([0-9a-fA-F-]{36})"?\s*$', open(src).read(), re.M)
assert m, 'secret_key absent du profil scw'
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump({'secret_key': m.group(1)}, f); f.write('\n')
print('scaleway.json écrit (valeur non affichée)')
PY
  chown root:root $ETC/scaleway.json; chmod 0600 $ETC/scaleway.json
fi

# 5. Unités : celles du SHA, User=agent substitué, vérifiées, posées. Timer NON activé.
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
sed 's/^User=REPLACE_WITH_EXISTING_OPERATOR_ACCOUNT$/User=agent/' $OPT/src-$SHORT/deploy/vm-adapter/ameesh-vm-adapter.service > $W/ameesh-vm-adapter.service
cp $OPT/src-$SHORT/deploy/vm-adapter/ameesh-vm-adapter.timer $W/
[ "$(grep -c '^User=agent$' $W/ameesh-vm-adapter.service)" = 1 ] || stop "substitution User="
systemd-analyze verify $W/ameesh-vm-adapter.service $W/ameesh-vm-adapter.timer
install -m 0644 -o root -g root $W/ameesh-vm-adapter.service $W/ameesh-vm-adapter.timer /etc/systemd/system/
systemctl daemon-reload
[ "$(systemctl is-enabled ameesh-vm-adapter.timer 2>/dev/null || true)" = disabled ] || stop "timer pas disabled après pose"
sha256sum /etc/systemd/system/ameesh-vm-adapter.service /etc/systemd/system/ameesh-vm-adapter.timer $OPT/commandes/vm-commande.py
stat -c '%a %U:%G %n' $ETC/scaleway.json $ETC/enrollment.json
echo "fin B2 : adaptateur à $SHORT, unités posées, timer DÉSACTIVÉ, enrollment enabled:false"
