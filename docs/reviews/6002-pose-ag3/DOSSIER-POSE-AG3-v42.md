# #6002 — pose AG3 v4.2 : delta minimal de v4.1

Bureau (BG), 08/11 ~13:35Z. Branche `review/bureau-6002-tunnel`. v3, v4 et v4.1 sont conservées.
v4.2 ajoute des fichiers suffixés `-v42`, à differ contre `-v41`.

**AUCUN GESTE sur les VM.** Seuls ont tourné des relevés en lecture, des essais à blanc et un témoin
réel dans un conteneur jetable sur BG.

## 1. B des deux relecteurs : retour des ACL (impl-11 #200824, impl-03 #200830)

**Défaut.**
- `setfacl -x u:vm-operator` laisse `mask::` dans l'ACL.
- Le `cmp` avec l'état d'avant échouait donc, et le retour sortait en 1 **avant** le nettoyage :
  passfile, cartes, clé et compte restaient en place, et un nouveau passage échouait de nouveau.

**v4.2.**
- **`acl-pose-v42.sh`** reprend le bloc ACL de v4.1, à l'identique sur le fond. Il garde en plus les
  états avant et après en **root 0600** dans les reçus.
- **`acl-retour-v42.sh verifier`** contrôle la non-dérive, sans rien écrire.
  - Pour chacun des 4 parents, l'ACL courante ne peut différer du reçu d'avant que par l'entrée
    `u:vm-operator:--x` ajoutée par la pose, et par la ligne `mask::` qu'elle entraîne.
  - Toute autre différence (entrée étrangère, droits, propriétaire, groupe) donne **STOP**.
  - Un reçu absent ou qui n'est pas en root 0600 donne aussi STOP.
- `retour-AG3-vm-operator-v42.sh` lance ce contrôle **avant toute mutation**, y compris à blanc.
  En cas de dérive, aucun retour n'est lancé et rien n'est effacé.
- **`acl-retour-v42.sh restaurer`** revérifie la non-dérive, puis lance
  `setfacl --restore=<reçu avant>` (borné aux 4 chemins du reçu). Il exige ensuite
  `getfacl` = reçu avant, **à l'octet**, et des modes identiques au reçu.
  - Pas de `setfacl -b` global.
  - Le paquet `acl` n'est retiré qu'**après** la restauration, et seulement si la pose l'a installé.
- **Témoin réel** (`temoin-acl-v42.sh`) : ubuntu épinglé, vrai paquet `acl`, mêmes octets que la pose et le retour.
  - Parent 750 sans ACL, pose `u:vm-operator:--x`, `mask::` présent (la cause du B), `ls` refusé à vm-operator.
  - Retour rc 0, `getfacl` avant = après à l'octet, aucun `mask::` résiduel, puis le nettoyage se poursuit.
  - Dérive (`u:autre:r-x` ajouté après la pose) : STOP en rc 1, ACL inchangées.
  - Reçu en 0644 : refusé.
  - Résultat : 10/10 OK.

## 2. N d'impl-03 sur v4.1 (#200830)

| N | v4.2 |
|---|---|
| `porte_ag3` affichée en INFO | C'est désormais une **garde dure** : sans `porte_ag3\|0`, le rc 1 de quiesce/runners n'est pas justifié, donc ÉCHEC |
| L'en-tête des tests annonçait « rc 0 ou 75, jamais 1 » | En-tête corrigé : rc 1 pour quiesce/runners (porte absente, hors vague), 75 pour probe-receipt |
| Le `trap` venait après l'installation du rôle | Il est posé dès que le dossier temporaire de mesh-1 est nommé, **avant** la copie SQL et l'installation. À toute sortie, le vérificateur est détruit sur mesh-1 et sur AG3. Conséquence : après un échec, une nouvelle pose de la base exige un nouveau secret (retour AG3 puis nouvelle pose) |
| `apt-get remove acl` n'était jamais atteint | Résolu par le §1 |
| Espaces en fin de ligne dans le relevé | Le relevé v4.2 est nettoyé (`sed 's/[[:space:]]*$//'`) |
| O4 absent des octets du vérificateur | `verifier-carte-producteur-AG3-v42.sh` imprime la procédure avec le message ROUGE |
| `/etc/ssh/authorized_keys` jamais retiré | La pose note `authorized_keys-dir-cree` si elle crée le dossier ; le retour fait alors `rmdir`, sauf si le dossier n'est pas vide (gardé et signalé) |

## 3. Pièces v4.2 (`SHA256SUMS-v42`)

| Pièce | Rôle |
|---|---|
| `DOSSIER-POSE-AG3-v42.md` | Ce dossier |
| `acl-pose-v42.sh`, `acl-retour-v42.sh` | Pose et retour des ACL |
| `temoin-acl-v42.sh` | Témoin réel en conteneur |
| `pose-AG3-vm-operator-v42.sh`, `retour-AG3-vm-operator-v42.sh`, `tests-AG3-vm-operator-v42.sh`, `pose-DB-vm_probe-v42.sh` | Scripts de pose, de retour et de test |
| `verifier-carte-producteur-AG3-v42.sh` | Vérificateur de la carte producteur |
| `releve-v42-blancs-*.txt` | Blancs et sortie du témoin |

`secret-vm-probe-v41.py` et `retour-DB-vm_probe.sh` sont inchangés.

**Ordre de pose :**
1. `pose-AG3-vm-operator-v42.sh --apply` ;
2. `pose-DB-vm_probe-v42.sh --apply <TS>` ;
3. `tests-AG3-vm-operator-v42.sh B`.

**Retour :** `retour-AG3-vm-operator-v42.sh --apply <TS>`, puis `retour-DB-vm_probe.sh --apply`.
