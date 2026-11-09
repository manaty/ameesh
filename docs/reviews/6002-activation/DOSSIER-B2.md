# #6002 — bloc root B2 : réinstallation de l'adaptateur VM de bureau-goal à 242ced70

Bureau (BG), 09/11. Demandé par mesh-design #205785 et #205796. Lancé par l'owner, en root, sur
bureau-goal, **après** le reçu de la simulation #6572 et deux avis OK (ameesh-cx1, mty-amh-impl-03).
N'active rien : `enrollment.json` reste `enabled: false`, le timer reste désactivé.

## Pourquoi
Le venv de BG porte `src-71ec56e3a8ae`, antérieur aux changements du côté contrôleur
(`vm-commande.py`, `cible.example.json`, #6327/#6329/#6334, README). AG3 porte M' = 242ced70.
Les fichiers de l'adaptateur sont identiques entre 242ced70 et main 14017a2a (blobs vérifiés).

## Ordre des gestes (`reinstall-242ced70.sh`)
0. Garde : root, hôte `bureau-goal`, timer inactif, `enrollment.json` à `enabled: false`,
   `src-242ced700d6e` et `venv-242ced700d6e` absents.
1. `git archive` exact de 242ced700d6e depuis le clone de BG, extrait root:root, sans g+w/o+w ;
   blobs vérifiés : `vm-commande.py`, `ameesh-vm-adapter.service`, `.timer`, `src/ameesh/vm_adapter.py`.
2. venv dédié `venv-242ced700d6e` (python3.12), `pip install` de la source, import
   `ameesh.vm_adapter` et `ameesh.host_release` vérifié sous le compte opérateur `agent`.
3. `commandes/vm-commande.py` au blob (ancien gardé en `.avant-242ced700d6e`), bascule des liens
   `src` et `venv`. La commande de retour est imprimée.
4. `scaleway.json` dérivé du profil scw existant du détenteur (`~agent/.config/scw/config.yaml`),
   seulement s'il est absent : `{"secret_key": …}`, root 0600, valeur jamais affichée. Date de
   modification du profil imprimée au reçu.
5. Unités du SHA, avec `User=agent` substitué (une ligne), `systemd-analyze verify`, pose dans
   `/etc/systemd/system`, `daemon-reload`, timer exigé `disabled`. Empreintes imprimées.

## Arrêts
`set -euo pipefail` : tout échec arrête. Les gardes nommées s'arrêtent avec « STOP : … ».
Un arrêt avant l'étape 3 ne touche pas aux liens actifs : l'adaptateur garde sa version courante.

## Retour
```
ln -sfn <ancien src> /opt/ameesh-vm-adapter/src
ln -sfn <ancien venv> /opt/ameesh-vm-adapter/venv
mv /opt/ameesh-vm-adapter/commandes/vm-commande.py.avant-242ced700d6e /opt/ameesh-vm-adapter/commandes/vm-commande.py
rm /etc/systemd/system/ameesh-vm-adapter.{service,timer} && systemctl daemon-reload
rm /etc/ameesh-vm-adapter/scaleway.json   # seulement si B2 l'a créé
```
Le timer n'ayant jamais été activé, aucune requête VM n'est partie.

## v2 (avis cx1 #205801)
- **B1** : `garde_etats` lit avec rc contrôlé les états du timer (LoadState, UnitFileState, ActiveState)
  et du service, les jobs en file et la présence d'un worker `ameesh.vm_adapter worker`. Lecture en
  échec ou état transitoire = STOP. Jouée avant toute écriture, répétée juste avant la bascule
  (étape 3) et après la pose (étape 5). Le reçu ne déduit plus « aucune requête VM » du seul timer.
- **B2** : reçu durable `/root/ameesh-operator-6002-B2-<TS>/` (0700) écrit AVANT le premier
  changement : cibles des liens, ancien `vm-commande.py`, chaque unité sauvegardée ou marquée
  absente, état initial de `scaleway.json`. `retour.sh` dans ce reçu restaure les liens, la commande
  et les unités d'avant ; il ne retire que les unités et le `scaleway.json` initialement absents. Un
  `trap ERR` rappelle ce retour à tout échec, y compris après une mutation partielle de l'étape 3.
- **N** : un `scaleway.json` déjà présent est vérifié (fichier régulier sans lien, nlink 1, root
  0600, JSON `{"secret_key": <uuid>}`), sans afficher la valeur. Le profil scw est explicite :
  exactement un profil, `manaty`, aucune `secret_key` au niveau racine. Sinon STOP. Constat relu
  sans valeur : le `default_project_id` du profil diffère du projet inscrit. La clé couvre
  l'organisation et le projet est passé explicitement par l'inscription, donc ce n'est pas un
  refus ; le booléen est consigné au reçu.
