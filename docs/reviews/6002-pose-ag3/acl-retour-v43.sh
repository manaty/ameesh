#!/bin/bash
# #6002 v4.3 — N1/N2 d'impl-03 #200878 : reçu absent → STOP (sauf --sans-acl prouvé), dérive de MODE
#   vérifiée avant toute restauration. v4.2 — retour BORNÉ des ACL de traversée (avis B impl-11 #200824, impl-03 #200830), exécuté SUR
# AG3 en root, et tel quel par temoin-acl-v42.sh en conteneur.
#   acl-retour-v42.sh verifier  <reçus> <login> [<racine>]   contrôle de non-dérive, AUCUNE écriture
#   acl-retour-v42.sh restaurer <reçus> <login> [<racine>]   contrôle, puis setfacl --restore du reçu
# Non-dérive : pour chacun des 4 parents, l'ACL courante ne peut différer de l'état avant (reçu root
# 0600) que par l'entrée u:<login>:--x posée et la ligne mask:: qu'elle entraîne. Toute autre
# différence (entrée étrangère, droits, propriétaire) : STOP, rien n'est effacé. Pas de setfacl -b.
# Après restauration : getfacl des 4 parents = reçu avant, à l'octet ; modes = reçu.
set -euo pipefail
MODE=$1; OPS=$2; L=$3; B=${4:-/home/ameesh}
P="$B $B/.local $B/.local/share $B/.local/share/ameesh"
AV="$OPS/acl-parents.avant"; MV="$OPS/acl-parents.mode.avant"
if [ ! -e "$AV" ]; then
  # v4.3 N2 : sans reçu, on ne restaure rien et on s'arrête, en disant pourquoi.
  pose=""; for d in $P; do getfacl -p "$d" 2>/dev/null | grep -q "^user:$L:" && pose="$pose $d"; done
  if [ -n "$pose" ]; then echo "STOP : reçu $AV absent alors que u:$L est posée sur :$pose ; rien n'est touché"; exit 1; fi
  if [ "${SANS_ACL:-0}" = 1 ]; then echo "reçu absent et aucune entrée u:$L sur les 4 parents : retour sans ACL (--sans-acl)"; exit 0; fi
  echo "STOP : reçu $AV absent ; aucune entrée u:$L trouvée. Si la pose s'est arrêtée avant l'étape 6 bis, relancer le retour avec --sans-acl"; exit 1
fi
[ "$(stat -c '%u %a' "$AV")" = "0 600" ] || { echo "STOP : reçu ACL $AV non root 0600"; exit 1; }
# v4.3 N1 : dérive de mode (droits, propriétaire, groupe) vérifiée AVANT toute restauration.
[ -f "$MV" ] && [ "$(stat -c '%u %a' "$MV")" = "0 600" ] || { echo "STOP : reçu de modes $MV absent ou non root 0600"; exit 1; }
stat -c '%a %U:%G %n' $P | cmp -s - "$MV" || { echo "STOP : dérive de mode sur les parents (≠ $MV) ; rien n'est effacé"; stat -c '%a %U:%G %n' $P; exit 1; }
COUR=$(getfacl -p $P)
python3 -I - "$AV" "$L" "$COUR" <<'PY' || { echo "STOP : dérive des ACL, rien n'est effacé"; exit 1; }
import sys
def blocs(texte):
    out, cle = {}, None
    for ligne in texte.splitlines():
        if ligne.startswith('# file: '):
            cle = ligne[8:]; out[cle] = {'tete': [], 'acl': set()}
        elif cle is None or not ligne.strip():
            continue
        elif ligne.startswith('#'):
            out[cle]['tete'].append(ligne)
        else:
            out[cle]['acl'].add(ligne)
    return out
avant, login, courant = blocs(open(sys.argv[1]).read()), sys.argv[2], blocs(sys.argv[3])
ok = set(avant) == set(courant)
for f in avant:
    a, c = avant[f], courant.get(f, {'tete': None, 'acl': set()})
    sans_masque = lambda s: {x for x in s if not x.startswith('mask::')}
    ajout = sans_masque(c['acl']) - sans_masque(a['acl'])
    retrait = sans_masque(a['acl']) - sans_masque(c['acl'])
    if c['tete'] != a['tete'] or retrait or not ajout <= {'user:%s:--x' % login}:
        print('dérive sur %s : en plus %s, en moins %s' % (f, sorted(ajout), sorted(retrait)))
        ok = False
sys.exit(0 if ok else 1)
PY
echo "non-dérive OK : seules u:$L:--x (et mask::) diffèrent de l'état avant"
[ "$MODE" = verifier ] && exit 0
[ "$MODE" = restaurer ] || { echo "mode inconnu"; exit 2; }
( cd / && setfacl --restore="$AV" )
getfacl -p $P | cmp -s - "$AV" || { echo "ACL ≠ reçu avant après restauration"; exit 1; }
stat -c '%a %U:%G %n' $P | cmp -s - "$MV" || { echo "modes ≠ reçu avant après restauration"; exit 1; }
echo "ACL restaurées : getfacl des 4 parents = reçu avant, à l'octet"
