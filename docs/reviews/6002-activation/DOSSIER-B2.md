# #6002 — bloc root B2 v6 : réinstallation de l'adaptateur VM de bureau-goal à 242ced70

Bureau (BG), 09/11. Demandé par mesh-design #205785 et #205796, corrigé selon cx1 #205801 (v2) et
#205810 (v3), puis impl-03 #205820 (v4 : N1 à N6), puis cx1 #205830 (v5), puis impl-03 #205832 N7/N8 (v6). Lancé par l'owner, en root, sur bureau-goal, **après** le reçu de la simulation #6572
et deux avis OK (ameesh-cx1, mty-amh-impl-03). N'active rien : `enrollment.json` reste
`enabled: false`, le timer reste désactivé. Le script ne garantit pas qu'aucune requête VM n'a eu
lieu : il garantit seulement qu'il n'en lance aucune, et ses gardes refusent tout worker vivant.

## Pourquoi
Le venv de BG porte `src-71ec56e3a8ae`, antérieur aux changements du côté contrôleur
(`vm-commande.py`, `cible.example.json`, #6327/#6329/#6334, README). AG3 porte M' = 242ced70.
Les fichiers de l'adaptateur sont identiques entre 242ced70 et main 14017a2a (blobs vérifiés).

## Ordre des gestes (`reinstall-242ced70.sh`)
0. **Gardes, toutes en lecture, avant toute écriture** :
   - root, hôte `bureau-goal` ;
   - `garde_etats` : timer disabled et inactive, service inactive, aucun job en file, aucun worker
     vivant, chaque lecture avec rc contrôlé ;
   - `enrollment.json` à `enabled: false` ; `src-242ced700d6e` et `venv-242ced700d6e` absents ;
   - cibles des liens `src` et `venv` lues avec contrôle, non vides ; commande actuelle présente ;
   - profil scw validé sans écriture : exactement un profil, `manaty`, aucune `secret_key` racine,
     `secret_key` et organisation bien formées ; un `scaleway.json` déjà présent est vérifié
     (régulier, nlink 1, root 0600, JSON `{"secret_key": <uuid>}`). Empreinte du profil gardée.
     L'écart de `default_project_id` est consigné, pas refusé (clé d'organisation, projet passé
     par l'inscription).
1. **Reçu durable** `/root/ameesh-operator-6002-B2-<TS>/` (0700) : cibles des liens, ancien
   `vm-commande.py`, chaque unité sauvegardée ou marquée absente, état initial de `scaleway.json`,
   et `retour.sh`.
2. `git archive` exact de 242ced700d6e, extrait root:root, sans g+w/o+w ; blobs vérifiés.
3. venv dédié `venv-242ced700d6e`, `pip install` de la source, import vérifié sous `agent`.
4. `garde_etats` répétée, puis `vm-commande.py` au blob et bascule des liens.
5. `scaleway.json` créé en O_EXCL, root 0600, seulement si le profil a la même empreinte qu'à
   l'étape 0. Valeur jamais affichée.
6. Unités du SHA (`User=agent` substitué, une ligne), `systemd-analyze verify`, pose,
   `daemon-reload`, `garde_etats` après la pose. Empreintes au reçu.

## Arrêts
`set -euo pipefail` et gardes nommées « STOP : … ». La validation du profil et du secret arrive à l'étape 0,
avant la moindre écriture ; la création de scaleway.json (étape 4) précède la bascule du lien venv. Après l'étape 1, un `trap ERR` rappelle le retour.

## Retour
`sudo bash /root/ameesh-operator-6002-B2-<TS>/retour.sh` restaure les liens, `vm-commande.py` et
les unités d'avant ; il ne retire que les unités et le `scaleway.json` qui étaient absents avant
B2. `src-242ced700d6e` et `venv-242ced700d6e` restent sur place, inertes.

## v4 (impl-03 #205820)
- **Lien src** : `/opt/ameesh-vm-adapter/src` est un dossier (installation historique 33bfa473) que le
  venv n'utilise pas (`ameesh` y est installé depuis `src-71ec56e3a8ae`). Il est laissé intact. Seul
  le lien `venv` change ; sa cible doit correspondre à `venv-<12 hex>` et exister (N3). Le retour la
  relit et la valide avant `ln`, sans sourcer de fichier.
- **N1** : `stop` rappelle `retour.sh` dès que le reçu existe.
- **N2** : les constats (date du profil, booléen de projet, liens, droits) sont écrits dans
  `$RECU/constats`, en plus de la sortie.
- **N4** : préalables du service vérifiés à l'étape 0 : `enrollment.json` et `database.env`
  réguliers, nlink 1, `agent`, 0600 ; `AMEESH_CONFIG` présent et lisible par `agent`.
- **N5** : `/opt/ameesh-vm-adapter`, `commandes/`, `/etc/ameesh-vm-adapter` et `/etc/systemd/system`
  root, sans écriture groupe ni autres, contrôlés à l'étape 0 et de nouveau avant la bascule.
- **N6** : dépendances épinglées par `PIP_CONSTRAINT` (versions du venv actuel : psutil 7.2.2,
  psycopg et psycopg-binary 3.3.8, typing_extensions 4.16.0 ; backend setuptools 80.9.0). Pas de
  hashes : pip reste en root sur PyPI, risque accepté et nommé. Le `freeze` du nouveau venv est gardé au reçu.

## v5 (cx1 #205830)
- La validation du profil et du secret existant est gardée en mémoire (`SCW_VALIDATION=$(scw valider) || stop`) :
  aucun fichier temporaire, en particulier aucun nom prévisible sous `/tmp` ouvert en root.
- La création (`SCW_RESULT=$(scw creer) || stop`), la relecture du nouveau lien venv et la lecture des
  droits finaux ont chacune leur rc contrôlé avant d'être consignées. Un échec arrête avant la pose
  des unités, sans annoncer « fin B2 ».

## v6 (impl-03, N7/N8)
- **N7** : `scaleway.json` est créé au début de l'étape 4, APRÈS les gardes répétées et AVANT la
  bascule du lien venv. Un échec d'écriture ou un profil modifié arrête donc avant la bascule.
- **N8** : si `scaleway.json` existe déjà (vérifié à l'étape 0), le profil n'est plus utilisé ni
  contrôlé ensuite. L'empreinte du profil n'est exigée que s'il faut créer le fichier ; elle est relue
  juste après `scw valider`, ce qui lie la validation à ces octets, puis juste avant la création.
- Les étapes sont renumérotées : 5 = unités.

## Intégrité au lancement (owner)
Ne pas lancer depuis l'arbre de `agent`. Copier en root, vérifier l'empreinte, lancer la copie :
```
sudo install -m 0700 -o root -g root /home/agent/.local/state/ameesh/operators/6002-activation/reinstall-242ced70.sh /root/reinstall-242ced70.sh
sudo sha256sum /root/reinstall-242ced70.sh      # doit égaler SHA256SUMS de la tête relue
sudo bash /root/reinstall-242ced70.sh
```
