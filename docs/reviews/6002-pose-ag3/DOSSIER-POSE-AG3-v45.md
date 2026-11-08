# #6002 — pose AG3 v4.5 : correctif de l'étape 2 (STOP de la pose v4.4 du 08/11 13:59Z)

Bureau (BG), 08/11 ~14:10Z. **AUCUN GESTE et aucune commande sur une VM** pour ce delta : tous les essais ont tourné dans des conteneurs jetables sur BG. AG3 est propre depuis le retour `--sans-acl` du coordinateur.

## Défaut

À l'étape 2, `"venv-$S12/bin/pip" install -q --no-compile "src-$S12"` passe un **nom de paquet**, pas un chemin. pip cherche alors `src-97285a076dee` sur l'index et échoue : « No matching distribution found ».

- Le défaut existe depuis la v3. Il n'a jamais été vu parce que les essais à blanc s'arrêtaient aux préalables.
- Recherche du même défaut dans la pose et les tests : c'est la seule occurrence. Les autres `src-$S12/…` sont des chemins passés à `install`, qui les interprète correctement.

## v4.5 (`pose-AG3-vm-operator-v45.sh`)

1. `pip install … "./src-$S12"` : la source est désormais passée comme un chemin.
2. `chown -R root:root` puis `chmod -R go-w` sont **bornés à `src-$S12`**, juste après l'extraction.
   - `git archive` produit des répertoires en 775 (drwxrwxr-x root:root).
   - C'était déjà sans effet en v4.4 : le groupe root n'a pas de membre non root. La fin de l'étape 2 faisait aussi `chmod -R go-w /opt/ameesh-vm-adapter` et le contrôle `find` bloquant.
   - v4.5 ferme de toute façon cette fenêtre avant pip.
3. **L'essai à blanc exerce l'étape 2** (demande #200933), sans `--apply` :
   - archive git de M envoyée par flux ;
   - sur AG3, avec `python3.12`, un venv jetable dans `mktemp -d /tmp/6002-blanc.*`, retiré par trap ;
   - `pip install -q --dry-run --no-deps --no-compile ./src-<sha>` ;
   - rien sous `/opt` ;
   - un échec de pip donne un STOP nommé.

## Témoins (conteneurs jetables, image du constructeur : `python3.12` = `e50d468e`, comme AG3)

- **`temoin-etape2-v45.sh`** extrait le bloc distant de l'étape 2 **à l'octet** de chaque script, puis le lance en root avec l'archive de M.
  - **v4.4 : défaut reproduit** (rc 1, « No matching distribution found for src-97285a076dee »).
  - **v4.5 : OK.** Résultat :
    - venv créé et `pip install ./src-…` réussi ;
    - `ameesh` importable depuis `/opt/ameesh-vm-adapter/venv` ;
    - `src-…` en 755 root:root ;
    - `vm-remote.py` et `health.py` en 644 root:root ;
    - contrôles `find` (aucun g+w/o+w, aucun non-root) passés.
- **Script de l'essai à blanc** (`BLANC`, octets du script) lancé tel quel en conteneur : « pip accepte ./src-97285a076dee », venv jetable retiré.
- **`temoin-acl-v44.sh`** (v4.4) : inchangé, aucun échec.

## Diffs explicites

- **`delta-v43-v45.diff`** (demandé par mesh-design #200933 ; c'est sur lui que porte l'avis). Il couvre :
  - la voie ACL de v4.4 : `acl-retour`, `retour`, témoin ;
  - la ligne pip et l'essai à blanc de v4.5 ;
  - le nouveau témoin de l'étape 2.
- `delta-v44-v45.diff` : la pose seule.

## Pièces

- Inchangées depuis v4.4 :
  - `retour-AG3-vm-operator-v44.sh` ;
  - `acl-retour-v44.sh` ;
  - `secret-vm-probe-v41.py`.
- Ordre d'exécution, aussi inchangé :
  1. `pose-AG3-vm-operator-v45.sh --apply` ;
  2. `pose-DB-vm_probe-v42.sh` ;
  3. `tests-AG3-vm-operator-v42.sh` ;
  4. retour : `retour-AG3-vm-operator-v44.sh`.
