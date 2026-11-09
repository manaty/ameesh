#!/usr/bin/env bash
# #6002 — bloc B2 v2 : réinstalle l'adaptateur VM de bureau-goal à 242ced700d6e (= main 14017a2a pour
# les fichiers de l'adaptateur), pose scaleway.json depuis le profil scw existant et les unités.
# N'ACTIVE RIEN : enrollment.json reste enabled:false, le timer reste désactivé.
# v2 (avis cx1 #205801) : B1 garde d'états lue avant toute écriture et répétée avant la bascule ;
# B2 sauvegarde des unités et des liens dans un reçu durable, retour qui restaure ; N : scaleway.json
# existant vérifié, profil scw explicite (« manaty », seul profil) ; écart de default_project_id consigné.
# À lancer par l'owner, en root, sur bureau-goal :  sudo bash reinstall-242ced70.sh
set -euo pipefail
SHA=242ced700d6ec1a63fc9d72643bc9faad175f72f
SHORT=${SHA:0:12}
REPO=/home/agent/development/manaty/ameesh
OPT=/opt/ameesh-vm-adapter
ETC=/etc/ameesh-vm-adapter
UNITDIR=/etc/systemd/system
OP=agent
PROFILE=manaty
RECU=/root/ameesh-operator-6002-B2-$(date -u +%Y%m%dT%H%M%SZ)
stop() { echo "STOP : $*" >&2; exit 1; }

# B1 : états lus avec rc contrôlé ; échec de lecture ou état transitoire = STOP.
garde_etats() {
  local t s jobs
  t=$(systemctl show ameesh-vm-adapter.timer -p LoadState -p UnitFileState -p ActiveState) || stop "lecture timer"
  s=$(systemctl show ameesh-vm-adapter.service -p LoadState -p ActiveState -p SubState) || stop "lecture service"
  jobs=$(systemctl list-jobs --no-legend) || stop "lecture des jobs"
  case "$t" in *ActiveState=inactive*) ;; *) stop "timer non inactive : $t" ;; esac
  case "$t" in *UnitFileState=disabled*|*LoadState=not-found*) ;; *) stop "timer non disabled : $t" ;; esac
  case "$s" in *ActiveState=inactive*) ;; *) stop "service non inactive : $s" ;; esac
  if printf '%s\n' "$jobs" | grep -q 'ameesh-vm-adapter'; then stop "job ameesh-vm-adapter en file"; fi
  pgrep -f 'ameesh\.vm_adapter worker' >/dev/null && stop "worker vm_adapter vivant"
  echo "garde états OK ($1)"
}

[ "$(id -u)" = 0 ] || stop "root requis"
[ "$(hostname)" = bureau-goal ] || stop "bureau-goal seulement"
garde_etats "avant toute écriture"
python3 -I -c 'import json,sys; assert json.load(open(sys.argv[1]))["enabled"] is False' $ETC/enrollment.json \
  || stop "enrollment.json n'est pas enabled:false"
[ ! -e $OPT/src-$SHORT ] && [ ! -e $OPT/venv-$SHORT ] || stop "src-$SHORT ou venv-$SHORT déjà présents : réconcilier"
[ ! -e $RECU ] || stop "reçu $RECU déjà présent"

# B2 : reçu durable AVANT le premier changement : liens, commande, unités (ou leur absence).
install -d -m 0700 -o root -g root $RECU
printf 'src=%s\nvenv=%s\n' "$(readlink $OPT/src)" "$(readlink $OPT/venv)" > $RECU/liens-avant
cp -p $OPT/commandes/vm-commande.py $RECU/vm-commande.py.avant
for u in ameesh-vm-adapter.service ameesh-vm-adapter.timer; do
  if [ -e $UNITDIR/$u ]; then cp -p $UNITDIR/$u $RECU/$u.avant; else : > $RECU/$u.absente; fi
done
[ -e $ETC/scaleway.json ] && : > $RECU/scaleway.json.preexistant || : > $RECU/scaleway.json.absent
cat > $RECU/retour.sh <<EOF
# Retour B2 : restaure l'état d'avant, rien d'autre.
set -eu
. $RECU/liens-avant
ln -sfn "\$src" $OPT/src
ln -sfn "\$venv" $OPT/venv
install -m 0644 -o root -g root $RECU/vm-commande.py.avant $OPT/commandes/vm-commande.py
for u in ameesh-vm-adapter.service ameesh-vm-adapter.timer; do
  if [ -e $RECU/\$u.avant ]; then install -m 0644 -o root -g root $RECU/\$u.avant $UNITDIR/\$u; fi
  if [ -e $RECU/\$u.absente ]; then rm -f $UNITDIR/\$u; fi
done
[ -e $RECU/scaleway.json.absent ] && rm -f $ETC/scaleway.json
systemctl daemon-reload
echo "retour B2 fait (src-$SHORT et venv-$SHORT laissés, inertes)"
EOF
chmod 0600 $RECU/retour.sh
echo "reçu et retour : $RECU (retour : sudo bash $RECU/retour.sh)"
trap 'echo "ÉCHEC : état d avant restaurable par sudo bash '"$RECU"'/retour.sh" >&2' ERR

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

# 3. Garde répétée, puis commande au blob et bascule des liens.
garde_etats "avant la bascule"
install -m 0644 -o root -g root $OPT/src-$SHORT/deploy/vm-adapter/commandes/vm-commande.py $OPT/commandes/.vm-commande.py.$SHORT
[ "$(git hash-object $OPT/commandes/.vm-commande.py.$SHORT)" = "$(runuser -u $OP -- git -C $REPO rev-parse "$SHA:deploy/vm-adapter/commandes/vm-commande.py")" ] || stop "vm-commande.py copié ≠ blob"
mv -f $OPT/commandes/.vm-commande.py.$SHORT $OPT/commandes/vm-commande.py
ln -sfn src-$SHORT $OPT/src
ln -sfn venv-$SHORT $OPT/venv
echo "liens : src → $(readlink $OPT/src) ; venv → $(readlink $OPT/venv)"

# 4. scaleway.json : profil scw « manaty » explicite et unique, sans affichage ; la clé couvre l'organisation,
#    le projet est passé explicitement par l'inscription (écart de default_project_id seulement consigné).
echo "profil scw source : $(stat -c '%y %U:%G %a' /home/$OP/.config/scw/config.yaml) (date de la copie = référence de péremption si la clé tourne)"
python3 -I - /home/$OP/.config/scw/config.yaml $ETC/scaleway.json $ETC/enrollment.json "$PROFILE" <<'PY'
import json, os, re, stat, sys
src, dst, enr, profile = sys.argv[1:5]
UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
projects = {t['project_id'] for t in json.load(open(enr))['targets'].values()}
if os.path.lexists(dst):
    st = os.lstat(dst)
    assert stat.S_ISREG(st.st_mode) and st.st_nlink == 1, 'scaleway.json existant : pas un fichier régulier'
    assert st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o600, 'scaleway.json existant : pas root 0600'
    d = json.load(open(dst))
    assert set(d) == {'secret_key'} and re.fullmatch(UUID, d['secret_key']), 'scaleway.json existant : JSON inattendu'
    print('scaleway.json existant vérifié (régulier, root 0600, JSON valide ; valeur non affichée)')
    sys.exit(0)
lines = open(src).read().splitlines()
assert not any(re.match(r'^secret_key:', l) for l in lines), 'secret_key au niveau racine : profil ambigu'
names = [m.group(1) for l in lines if (m := re.match(r'^  ([A-Za-z0-9_-]+):\s*$', l))]
assert names == [profile], 'profils scw %r : exactement un profil %r attendu' % (names, profile)
block = {}
inside = False
for l in lines:
    if re.match(r'^  ' + re.escape(profile) + r':\s*$', l):
        inside = True
        continue
    if inside:
        m = re.match(r'^    ([a-z_]+):\s*"?([^"]*)"?\s*$', l)
        if not m:
            break
        block[m.group(1)] = m.group(2)
assert re.fullmatch(UUID, block.get('secret_key', '')), 'secret_key absente ou malformée dans le profil'
assert re.fullmatch(UUID, block.get('default_organization_id', '')), 'organisation du profil absente'
same_project = block.get('default_project_id') in projects
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    json.dump({'secret_key': block['secret_key']}, f)
    f.write('\n')
os.chown(dst, 0, 0)
print('scaleway.json écrit depuis le profil %s (valeur non affichée) ; default_project_id du profil = projet inscrit : %s' % (profile, same_project))
PY

# 5. Unités : celles du SHA, User=agent substitué, vérifiées, posées. Timer NON activé.
W=$(mktemp -d)
sed 's/^User=REPLACE_WITH_EXISTING_OPERATOR_ACCOUNT$/User=agent/' $OPT/src-$SHORT/deploy/vm-adapter/ameesh-vm-adapter.service > $W/ameesh-vm-adapter.service
cp $OPT/src-$SHORT/deploy/vm-adapter/ameesh-vm-adapter.timer $W/
[ "$(grep -c '^User=agent$' $W/ameesh-vm-adapter.service)" = 1 ] || stop "substitution User="
systemd-analyze verify $W/ameesh-vm-adapter.service $W/ameesh-vm-adapter.timer
install -m 0644 -o root -g root $W/ameesh-vm-adapter.service $W/ameesh-vm-adapter.timer $UNITDIR/
rm -rf "$W"
systemctl daemon-reload
garde_etats "après la pose"
sha256sum $UNITDIR/ameesh-vm-adapter.service $UNITDIR/ameesh-vm-adapter.timer $OPT/commandes/vm-commande.py | tee $RECU/empreintes-apres
stat -c '%a %U:%G %n' $ETC/scaleway.json $ETC/enrollment.json
echo "fin B2 : adaptateur à $SHORT, unités posées, timer DÉSACTIVÉ, enrollment enabled:false ; reçu $RECU"
