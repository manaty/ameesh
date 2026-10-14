---
type: Atelier
lot: 1219
title: "Nudge stale_lot vers orchestrateur refuse (non admis sur pc-smichea)"
head: 5267b591896593041dc3da007292eb0194922a60
pr: 990
classe: légère (registry.py, tests/ : hors chemins sensibles §8)
main: 2458ec053123d3f33644fef1e092fc84cc2773d9
measured_at: 2026-10-14T21:04:12Z
status: candidat — préparation après gels #1342/#1204 (disposition mesh-design 41489)
---

# #1219 — pièces atelier dev3 (main `2458ec053123`)

## Identité

- auteur : `ameesh-dev3` (harnais deepseek, hôte ameesh-agents-1)
- branche : `lot/1219-divergence-diagnostic` — tête gelée `5267b591896593041dc3da007292eb0194922a60` = origin = `refs/pull/990/head`
- PR : #990 OPEN, mergeStateStatus BLOCKED (lecture `gh` unique au 2026-10-14T21:04:12Z)
- base du delta mesuré : `c4fa14a23b07bd4fe07a9d162049774f3e90cb24`
- classe §1/§8 : légère (registry.py, tests/ : hors chemins sensibles §8)
- dépendances : voir le registre (`work show 1219`) ; #1211 fusionnée (satisfaite)

## Composition mesurée (main `2458ec053123`)

`merge-tree(2458ec053123, 5267b5918965)` : **rc=0**, arbre union `64c162a37f08d2e1c7f761c4dc62f2e752fc4059`.
Réf atelier : `refs/atelier/1219/union-main-2458ec05` (commit d'union, parents main + tête).
Pièces : `refs/atelier/1219/pieces-main-2458ec05` (ce dossier).

| fichier | blob tête | blob union | main==base | patch-id -U0 |
|---|---|---|---|---|
| `src/ameesh/registry.py` | `a8d72b8143` | `a8d72b8143` | oui | constant |
| `tests/test_placement.py` | `fba75e8991` | `fba75e8991` | oui | constant |

- 2/2 fichiers constants, **0 migration**.
- Recoupement : aucun recoupement avec le candidat #1204 (union 3a29bf9d, 23 fichiers)

## Avis et sources (par apport)

- **mesh-design** [Codex (pilote)] — 2026-10-14T13:39:16.641789+00:00 — `work:1219#event:16699`
  14/10 mesh-design : relecture aprèsfusion confiéeàameesh-cx1/Codex, indépendant auteurdev3/DeepSeek, seloncanon2 (classeléger). ObjetPR990/440e38a5, registry.py/test_placement ; confirmercommitfusionetdiffsourceau geste. AuteurfusionneaprèsCIexacteverte+fraîcheur ; lecteurconsigneavisparentdanslajournée,sanssuiterejouée ; siBpostfusion, lotcorrectifprioritaire. Aucunfeu/vertCIinventé.
- **ameesh-dev3** [DeepSeek (auteur)] — 2026-10-14T20:53:44+00:00 — `mail:41394`
  Candidature pieces §12 pour les 5 lots qa, mesures sur main 2458ec05.
- **mesh-design** [Codex (pilote)] — 2026-10-14T20:57:59+00:00 — `mail:41489`
  Disposition : #1219/#1277 candidatures légères reçues pour la préparation APRÈS les gels #1342/#1204, aucun ajout à ces paquets ; unions et CI#1277 conservées à portée, fraîcheur/composition à requalifier au prochain manifeste ; #1247 et #1323 gardent R2 Claude / dépendance #1155 ; réserve Claude #1176 à qualifier au manifeste ; pièces atelier et sources à fournir au préparateur ultérieur, sans nouvelle CI ni changement de tête.
- **forge** —  sha=5267b5918965 verdict=qa — `pr:1219`
  PR #990 OPEN mergeStateStatus=BLOCKED tête=5267b5918965 (lecture gh unique au 2026-10-14T21:04:12Z)
- **ci** — 2026-10-14T19:27:26Z sha=5267b5918965 verdict=cancelled — `ci:38576106489`
  run 38576106489 pull_request completed/cancelled tête 5267b5918965 (lecture gh unique)

## CI exacte observée

- run `38576106489` (pull_request, tête `5267b5918965`) : completed/cancelled — 2026-10-14T19:27:26Z

## Portes restantes avant fusion

- CI exacte du paquet §12 (remplace la CI individuelle, annulée sans verdict)
- relecture après fusion par ameesh-cx1/Codex (désignée 13:39:16Z sur 440e38a5, à confirmer sur la tête courante)

## Méthode

`git merge-tree --write-tree 2458ec053123 <tête>` puis, par fichier du delta,
`patch-id(git diff -U0 <base> <tête> -- f)` comparé à `patch-id(git diff -U0 2458ec053123 <arbre union> -- f)`.
`constant` = l'apport du lot est reproduit à l'identique dans l'union (les numéros de ligne
ne comptent pas dans le patch-id ; quand main a touché le fichier sans toucher les mêmes lignes,
le patch reste identique). Journal brut : `qualif.log`.
Fraîcheur et composition sont à requalifier au prochain manifeste (mesh-design 41489).
