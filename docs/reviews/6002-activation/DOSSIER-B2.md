# #6002 — bloc root B2 v3 : réinstallation de l'adaptateur VM de bureau-goal à 242ced70

Bureau (BG), 09/11. Demandé par mesh-design #205785 et #205796, corrigé selon cx1 #205801 (v2) et
#205810 (v3). Lancé par l'owner, en root, sur bureau-goal, **après** le reçu de la simulation #6572
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
`set -euo pipefail` et gardes nommées « STOP : … ». Tout refus portant sur le profil ou le secret
arrive à l'étape 0, avant la moindre écriture. Après l'étape 1, un `trap ERR` rappelle le retour.

## Retour
`sudo bash /root/ameesh-operator-6002-B2-<TS>/retour.sh` restaure les liens, `vm-commande.py` et
les unités d'avant ; il ne retire que les unités et le `scaleway.json` qui étaient absents avant
B2. `src-242ced700d6e` et `venv-242ced700d6e` restent sur place, inertes.
