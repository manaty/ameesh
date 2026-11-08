# #6002 — AG3 v4.6 : porter l'adaptateur posé à M' et poser le rôle DB de M' (mesh-design #201411)

Bureau (BG), 08/11 ~17:10Z. **AUCUN GESTE et aucune commande sur une VM** pour ce delta : tous les
essais ont tourné dans des conteneurs jetables sur BG. v4.5 reste la pose de référence ; les versions
v4.x sont conservées.

**M' = `242ced700d6ec1a63fc9d72643bc9faad175f72f`** (#6329 fusionné, h-17 ; vérifié ancêtre de
`origin/main`). À M', `vm-remote.py` n'accède plus du tout à `agent_mailbox` : il appelle
`vm_probe_send` et `vm_probe_receipt`.

## Empreintes vérifiées contre M'

| Fichier | Attendu (#201411) | Calculé (`git show M':… \| sha256sum`) |
|---|---|---|
| `deploy/sql/role-vm-probe.sql` | `98cac000079d8949` | `98cac000079d8949` ✔ |
| `deploy/sql/_contrat-vm-probe.sql` | `cf371e77ddc207c7` | `cf371e77ddc207c7` ✔ |

## Ordre (#201411)

1. Mise à jour de l'adaptateur.
2. Rôle DB.
3. Tests de phase B.

## 1. Mise à jour de l'adaptateur

Pièces :
- `maj-adaptateur-AG3-v46.sh` : côté BG, à blanc par défaut ;
- `maj-adaptateur-distant-v46.sh` : côté AG3, envoyé sur l'entrée standard.

C'est une **mise à jour** de la pose v4.5, pas une pose neuve. Le compte, sshd, sudoers, les cartes,
le reçu de santé, l'ACL et le passfile ne changent pas.

- **Ce qui bouge entre M et M'**, d'après le diff git : `vm-remote.py`, `checks/health.py`, l'unité
  `ameesh-vm-health.service` (`StandardError=journal`) et le paquet ameesh du venv.
- **Ce qui ne bouge pas** : `venv-processus.py` (octets identiques), `ameesh-vm-health.timer`, et les
  schémas des cartes `remote.json` et `health-*.json` (clés identiques à M').
- **Les cartes et les unités ne nomment aucun `src-<sha>` ni `venv-<sha>`.** Elles passent par
  `/opt/ameesh-vm-adapter/{venv,remote,checks}`. La partie distante le vérifie et s'arrête sinon. Seul
  le lien `venv` bascule.
- **Mode `verifier`** (lecture) : exige la pose v4.5 à M, c'est-à-dire le lien vers `venv-97285a076dee`,
  les empreintes des 3 fichiers de M et celle de l'unité de M. Il exige aussi que
  `src-`/`venv-242ced700d6e` soient absents.
- **À blanc** (sans `--apply`) : `verifier`, puis `pip install --dry-run --no-deps ./src-<M'>` avec
  `python3.12`, dans un venv jetable sous `/tmp`, retiré ensuite.
- **`--apply`**, dans cet ordre :
  1. reçu avant ;
  2. `src-<M'>` extrait et ramené en root `go-w` ;
  3. `venv-<M'>` créé, puis `pip install ./src-<M'>` ;
  4. contrôle que `ameesh` s'importe **avant** la bascule ;
  5. intention consignée ;
  6. bascule atomique du lien (`ln -sfn` puis `mv -T`) ;
  7. fichiers de M' posés par un temporaire puis un renommage, `daemon-reload` du gestionnaire ameesh ;
  8. empreintes de M' exigées, contrôles `find` (aucun g+w ou o+w, aucun non-root), import ;
  9. reçu après.

  M reste en place pour le retour.
- **`--apply --retour`** : lien et fichiers de M remis, empreintes de M exigées, M' conservé.

Entre la mise à jour et le rôle, l'adaptateur à M' appelle des fonctions qui n'existent pas encore.
C'est sans effet : la pose est inactive (activation après #6321) et aucune vague n'est en cours.

## 2. Rôle DB (`pose-DB-vm_probe-v46.sh`)

Les changements par rapport à v4.2 :
- **SQL pris à M'**, vérifié par `ROLE_SHA=98cac000079d8949` et `CONTRAT_SHA=cf371e77ddc207c7`. Le rôle
  de M' révoque tout SELECT de `vm_probe` sur `agent_mailbox` (table et colonnes) et ajoute
  `vm_probe_receipt`, une fonction definer en lecture.
- **Vérificateur SCRAM réutilisé**, sans en régénérer un : la commande est
  `pose-DB-vm_probe-v46.sh --apply 20261108T141112Z-2455349`. Le script lit
  `/root/ameesh-operator-6002-AG3-20261108T141112Z-2455349/vm_probe.scram` (root 0600). Ce
  vérificateur correspond au passfile vm-operator posé à 14:11Z.
- **Changement délibéré du trap.**
  - La copie sur mesh-1 est détruite à toute sortie.
  - La copie sur AG3 n'est détruite qu'**après** un `ALTER ROLE` réussi.
  - En v4.2, un échec détruisait aussi celle d'AG3, et le passfile ne correspondait alors plus à aucun
    vérificateur : il fallait un nouveau secret (retour AG3, puis nouvelle pose). Garder le
    vérificateur rend la relance possible. Il reste root 0600 dans les reçus.
- **Course du contrat (#6364).**
  - L'**audit seul** est relancé une fois s'il est refusé.
  - Un refus à l'installation annule toute la transaction (aucun rôle créé) ; le script peut alors
    être relancé tel quel.

## 3. Tests de phase B (`tests-AG3-vm-operator-v46.sh`)

Ils reprennent v4.2 :
- `version-mode` et `doctor` réels ;
- `quiesce` et `runners` en rc 1 avec la porte absente ;
- `probe-receipt` en 75 ;
- ACL, nouveau venv et carte producteur.

Pour M' :
- **6** relations lisibles (`agent_mailbox` n'en fait plus partie) ;
- `SELECT * ` refusé partout ;
- **`SELECT` direct d'`agent_mailbox` refusé** à `vm_probe` ;
- **`vm_probe_receipt` exécutable** sous `vm_probe` : lecture par la fonction, 0 ligne hors vague.

`vm_probe_send` n'est pas appelé, car il écrit.

## Témoins

**`temoin-maj-v46.sh`**, en conteneur jetable (image du constructeur, `python3.12` = AG3) : 9/9.
- L'état « pose v4.5 » est construit par le bloc de l'étape 2 extrait à l'octet de la pose v4.5.
- Résultats :
  - `verifier` reconnaît M ;
  - `appliquer` rend rc 0 et le lien pointe vers `venv-242ced700d6e` ;
  - `vm-remote.py` est celui de M' (`vm_probe_receipt`, plus d'accès direct à `agent_mailbox`) ;
  - l'unité est celle de M' (`StandardError=journal`), propriétaire ameesh ;
  - `daemon-reload` est demandé ;
  - les reçus avant et après sont écrits ;
  - le retour remet M et conserve M' ;
  - une carte nommant un `src-<sha>` donne STOP.

**Bloc à blanc pip** pour M', en conteneur : « pip accepte ./src-242ced700d6e ».

**Pas de témoin SQL local** : le rôle et le contrat de M' supposent le schéma du mesh. Leur preuve est
l'audit `_contrat` (« vm_probe contract held ») exigé à l'installation et après le mot de passe.

## Diff et pièces

**Diff explicite : `delta-v45-v46.diff`.** Il couvre :
- `pose-DB` v4.2 → v4.6 ;
- `tests` v4.2 → v4.6 ;
- les 3 nouveaux fichiers (`maj-adaptateur-AG3-v46.sh`, `maj-adaptateur-distant-v46.sh`, `temoin-maj-v46.sh`).

Pièces et empreintes : `SHA256SUMS-v46`.

Relecture : avis delta d'un relecteur (impl-11 ou impl-03). Ensuite, pose sans repasser par mesh-design.
