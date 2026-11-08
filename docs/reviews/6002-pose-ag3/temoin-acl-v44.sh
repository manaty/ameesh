#!/usr/bin/env bash
# #6002 v4.4 — acl-retour-v44.sh (lectures getfacl exigées réussies). v4.3 — + cas dérive de MODE (N1) et reçu absent (N2). v4.2 — témoin RÉEL en conteneur jetable (ubuntu épinglé, paquet acl réel) des octets
# acl-pose-v42.sh / acl-retour-v44.sh : parent 750 sans ACL, pose u:vm-operator:--x, retour,
# getfacl avant = après à l'octet, puis la suite du nettoyage continue ; cas de dérive : STOP sans effacer.
set -u
ICI=$(cd "$(dirname "$0")" && pwd)
IMG=ubuntu@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
docker run --rm -v "$ICI":/t:ro "$IMG" bash -c '
set -u; ECHEC=0; ok() { echo "OK   $*"; }; ko() { echo "ECHEC $*"; ECHEC=1; }
apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends acl python3 >/dev/null || { echo "installation acl impossible"; exit 3; }
useradd -m -d /home/ameesh ameesh; useradd --system vm-operator; useradd --system autre
B=/home/ameesh; mkdir -p $B/.local/share/ameesh; chown -R ameesh:ameesh $B; chmod 750 $B; chmod 775 $B/.local $B/.local/share $B/.local/share/ameesh
P="$B $B/.local $B/.local/share $B/.local/share/ameesh"
# --- cas nominal
OPS=/root/ops1; install -d -m 700 $OPS; getfacl -p $P > /root/reference.avant
bash /t/acl-pose-v42.sh $OPS vm-operator >/dev/null && ok "pose u:vm-operator:--x"
getfacl -p $B | grep -q "^mask::" && ok "mask:: présent après pose (cause du B v4.1)"
su -s /bin/sh vm-operator -c "ls $B" >/dev/null 2>&1 && ko "vm-operator liste $B" || ok "vm-operator ne liste pas $B"
bash /t/acl-retour-v44.sh restaurer $OPS vm-operator; rc=$?
[ $rc = 0 ] && ok "retour rc 0" || ko "retour rc $rc"
getfacl -p $P | cmp -s - /root/reference.avant && ok "getfacl avant = après, à l octet" || { ko "getfacl différent"; getfacl -p $B; }
getfacl -p $B | grep -q "^mask::" && ko "mask:: résiduel" || ok "aucun mask:: résiduel"
echo "suite du nettoyage atteinte" > /root/suite && ok "nettoyage poursuivi après le retour ACL"
# --- cas de dérive : entrée étrangère ajoutée après la pose → STOP, rien effacé
OPS=/root/ops2; install -d -m 700 $OPS
bash /t/acl-pose-v42.sh $OPS vm-operator >/dev/null; setfacl -m u:autre:r-x $B/.local
avant=$(getfacl -p $P)
bash /t/acl-retour-v44.sh restaurer $OPS vm-operator >/dev/null; rc=$?
[ $rc != 0 ] && ok "dérive : STOP (rc $rc)" || ko "dérive non détectée"
[ "$(getfacl -p $P)" = "$avant" ] && ok "dérive : rien effacé (ACL inchangées)" || ko "dérive : ACL modifiées"
# --- reçu non protégé → STOP
chmod 644 $OPS/acl-parents.avant; bash /t/acl-retour-v44.sh verifier $OPS vm-operator >/dev/null 2>&1 && ko "reçu 0644 accepté" || ok "reçu non 0600 refusé"
# Remise à zéro du conteneur entre les cas (ACL du cas « dérive » retirées) ; jamais fait sur AG3.
propre() { setfacl -b $P; chmod 750 $B; chmod 775 $B/.local $B/.local/share $B/.local/share/ameesh; }
# --- N1 v4.3 : dérive de MODE après la pose → STOP avant restauration, ACL et modes inchangés
propre; OPS=/root/ops3; install -d -m 700 $OPS
bash /t/acl-pose-v42.sh $OPS vm-operator >/dev/null && ok "pose (cas mode)" || ko "pose (cas mode)"
chmod 755 $B
avant=$(getfacl -p $P; stat -c "%a %n" $P)
bash /t/acl-retour-v44.sh restaurer $OPS vm-operator >/dev/null; rc=$?
[ $rc != 0 ] && ok "dérive de mode : STOP (rc $rc)" || ko "dérive de mode non détectée"
[ "$(getfacl -p $P; stat -c "%a %n" $P)" = "$avant" ] && ok "dérive de mode : rien restauré ni effacé" || ko "dérive de mode : état modifié"
chmod 750 $B
# --- N2 v4.3 : reçu absent alors que l ACL est posée → STOP, rien touché
mv $OPS/acl-parents.avant $OPS/acl-parents.avant.cache
avant=$(getfacl -p $P)
bash /t/acl-retour-v44.sh verifier $OPS vm-operator >/dev/null; rc=$?
[ $rc != 0 ] && [ "$(getfacl -p $P)" = "$avant" ] && ok "reçu absent + ACL posée : STOP, rien touché" || ko "reçu absent + ACL posée (rc $rc)"
# --- N2 v4.3 : reçu absent et aucune ACL (pose arrêtée avant 6 bis) → STOP, sauf --sans-acl
mv $OPS/acl-parents.avant.cache $OPS/acl-parents.avant; bash /t/acl-retour-v44.sh restaurer $OPS vm-operator >/dev/null && ok "même reçu remis : restauration OK" || ko "restauration après remise du reçu"
propre; OPS=/root/ops4; install -d -m 700 $OPS
bash /t/acl-retour-v44.sh verifier $OPS vm-operator >/dev/null; rc=$?
[ $rc != 0 ] && ok "reçu absent, aucune ACL : STOP par défaut" || ko "reçu absent sans ACL accepté sans --sans-acl"
SANS_ACL=1 bash /t/acl-retour-v44.sh verifier $OPS vm-operator >/dev/null && ok "reçu absent, aucune ACL, --sans-acl : retour sans ACL autorisé" || ko "--sans-acl refusé"
# --- B impl-11 v4.4 : lecture getfacl en échec + --sans-acl → rc non nul (getfacl simulé en rc 1)
mkdir -p /root/faux; printf "#!/bin/sh\nexit 1\n" > /root/faux/getfacl; chmod 755 /root/faux/getfacl
PATH=/root/faux:$PATH SANS_ACL=1 bash /t/acl-retour-v44.sh verifier $OPS vm-operator >/dev/null; rc=$?
[ $rc != 0 ] && ok "lecture getfacl en échec + --sans-acl : STOP (rc $rc)" || ko "lecture en échec acceptée avec --sans-acl"
# --- getfacl absent (paquet retiré) + --sans-acl → rc non nul
DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq acl >/dev/null 2>&1
SANS_ACL=1 bash /t/acl-retour-v44.sh verifier $OPS vm-operator >/dev/null; rc=$?
[ $rc != 0 ] && ok "getfacl absent + --sans-acl : STOP (rc $rc)" || ko "outil absent accepté avec --sans-acl"
exit $ECHEC'
