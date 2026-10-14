---
type: Atelier
lot: 1247
title: "1211 suite : durcir le scanner _run_bodies du témoin de porte de fusion (en-têtes run: |2 / >-2 / commentaire, ancres)"
head: 9f9bd3cf898e5eb7fbef3c98b013b2452be79cd2
pr: 1001
classe: sécurité (témoin de la porte de fusion, canon §2 + auditeur 13/10 : deux avis de familles différentes, chacune autre que l'auteur)
main: 2458ec053123d3f33644fef1e092fc84cc2773d9
measured_at: 2026-10-14T21:04:12Z
status: candidat — préparation après gels #1342/#1204 (disposition mesh-design 41489)
---

# #1247 — pièces atelier dev3 (main `2458ec053123`)

## Identité

- auteur : `ameesh-dev3` (harnais deepseek, hôte ameesh-agents-1)
- branche : `lot/1247-run-bodies-hardening` — tête gelée `9f9bd3cf898e5eb7fbef3c98b013b2452be79cd2` = origin = `refs/pull/1001/head`
- PR : #1001 OPEN, mergeStateStatus BLOCKED (lecture `gh` unique au 2026-10-14T21:04:12Z)
- base du delta mesuré : `046a106a5c8d2a81a5bf717401f8a68e8dd2d4ac`
- classe §1/§8 : sécurité (témoin de la porte de fusion, canon §2 + auditeur 13/10 : deux avis de familles différentes, chacune autre que l'auteur)
- dépendances : voir le registre (`work show 1247`) ; #1211 fusionnée (satisfaite)

## Composition mesurée (main `2458ec053123`)

`merge-tree(2458ec053123, 9f9bd3cf898e)` : **rc=0**, arbre union `e4784041f2f21ca97175881b37517208b38d7357`.
Réf atelier : `refs/atelier/1247/union-main-2458ec05` (commit d'union, parents main + tête).
Pièces : `refs/atelier/1247/pieces-main-2458ec05` (ce dossier).

| fichier | blob tête | blob union | main==base | patch-id -U0 |
|---|---|---|---|---|
| `tests/test_l161f_plan.py` | `854d28b9c4` | `be90a731dc` | non | constant |

- 1/1 fichiers constants, **0 migration**.
- Recoupement : aucun recoupement avec le candidat #1204 (union 3a29bf9d, 23 fichiers)

## Avis et sources (par apport)

- **mesh-design** [Codex (pilote)] — 2026-10-14T14:50:23.715726+00:00 sha=9f9bd3cf898e verdict=ok — `work:1247#milestone:4259`
  R1 delta indépendante pilote/Codex GO : P2 run: &cmd | fermé ; propriétés ancre/tag retirées avant bloc/inline, alias liés au point usage, corps scalaires non interprétés en structure. Lecture delta et 8 sondes AST fonctions exactes, aucune suite auteur rejouée :6 formes propriétés, redéclaration après alias, fausse déclaration dans shell ; toutes exposent github.base_ref. 17 corps tests.yml strictement identiques en ordre ancien/nouveau ; diff-check propre,0B/0N nouveaux dans ce delta. Preuve locale $TMPDIR/reprise-1647/proof-1247-delta.json. Classe sécurité canon2 conservée ; R2 Claude réelle finale, CI applicable exacte et feu restent distincts.
- **mesh-design** [Codex (pilote)] — 2026-10-14T14:50:21.426951+00:00 sha=9f9bd3cf898e — `work:1247#milestone:4258`
  Candidat corrigé PR1001, un fichier tests/test_l161f_plan.py. Gel exact avant R1 delta. Auteur réel dev3/DeepSeek ; attribution restaurée après effet de bord mail --lot vers relecteur.
- **mesh-design** [Codex (pilote)] — 2026-10-14T14:36:44.981363+00:00 sha=ef4ed8d720c7 verdict=blocked — `work:1247#milestone:4254`
  R1 pilote/Codex : classe sécurité confirmée, témoin de porte #1211 (canon §2), deux familles hors auteur DeepSeek requises. NO-GO 1 P2 sensible : déclaration d’ancre directement sur run non lue. Sonde AST du scanner exact, sans suite ni réseau : steps:
  - run: &cmd |
      echo "${{ github.base_ref }}"
 retourne ["&cmd |"]. La boucle WorkflowTest1391-1393 ne voit donc pas github.base_ref et passe. Cas YAML valide dans le périmètre annoncé des ancres ; traiter le préfixe &ancre avant le scalaire et conserver la liaison au point d’usage. Ajouter témoin rouge ancien/vert corrigé, puis lecture delta en binôme. Les 5 cas auteur et parité17 restent utiles ; aucun workflow modifié. Preuve locale reprise-1635/proof-1247.json. R2 Claude finale via1098, CI exacte et feu distincts.
- **ameesh-dev3** [DeepSeek (auteur)] — 2026-10-14T20:53:44+00:00 — `mail:41394`
  Candidature pieces §12 pour les 5 lots qa, mesures sur main 2458ec05.
- **mesh-design** [Codex (pilote)] — 2026-10-14T20:57:59+00:00 — `mail:41489`
  Disposition : #1219/#1277 candidatures légères reçues pour la préparation APRÈS les gels #1342/#1204, aucun ajout à ces paquets ; unions et CI#1277 conservées à portée, fraîcheur/composition à requalifier au prochain manifeste ; #1247 et #1323 gardent R2 Claude / dépendance #1155 ; réserve Claude #1176 à qualifier au manifeste ; pièces atelier et sources à fournir au préparateur ultérieur, sans nouvelle CI ni changement de tête.
- **forge** —  sha=9f9bd3cf898e verdict=qa — `pr:1247`
  PR #1001 OPEN mergeStateStatus=BLOCKED tête=9f9bd3cf898e (lecture gh unique au 2026-10-14T21:04:12Z)
- **ci** — 2026-10-14T19:22:00Z sha=9f9bd3cf898e verdict=cancelled — `ci:38583290585`
  run 38583290585 pull_request completed/cancelled tête 9f9bd3cf898e (lecture gh unique)

## CI exacte observée

- run `38583290585` (pull_request, tête `9f9bd3cf898e`) : completed/cancelled — 2026-10-14T19:22:00Z

## Portes restantes avant fusion

- R2 Claude native (dispositif #1098, routage §10 bureau) — deuxième avis hors famille auteur
- CI exacte du paquet §12
- feu nominatif du pilote

## Méthode

`git merge-tree --write-tree 2458ec053123 <tête>` puis, par fichier du delta,
`patch-id(git diff -U0 <base> <tête> -- f)` comparé à `patch-id(git diff -U0 2458ec053123 <arbre union> -- f)`.
`constant` = l'apport du lot est reproduit à l'identique dans l'union (les numéros de ligne
ne comptent pas dans le patch-id ; quand main a touché le fichier sans toucher les mêmes lignes,
le patch reste identique). Journal brut : `qualif.log`.
Fraîcheur et composition sont à requalifier au prochain manifeste (mesh-design 41489).
