---
type: Atelier
lot: 1176
title: "Comptes : plafond de rythme réglable par compte (ne plus perdre la fin de fenêtre des forfaits)"
head: 24701c985b5aa08ab99986ca77c0263971a6aca4
pr: 905
classe: sensible argent (src/ameesh/accounts.py, src/ameesh/cost.py ; garde autorisations/budget)
main: 2458ec053123d3f33644fef1e092fc84cc2773d9
measured_at: 2026-10-14T21:04:12Z
status: candidat — préparation après gels #1342/#1204 (disposition mesh-design 41489)
---

# #1176 — pièces atelier dev3 (main `2458ec053123`)

## Identité

- auteur : `ameesh-dev3` (harnais deepseek, hôte ameesh-agents-1)
- branche : `lot/1176-pace-ceiling` — tête gelée `24701c985b5aa08ab99986ca77c0263971a6aca4` = origin = `refs/pull/905/head`
- PR : #905 OPEN, mergeStateStatus BLOCKED (lecture `gh` unique au 2026-10-14T21:04:12Z)
- base du delta mesuré : `c4fa14a23b07bd4fe07a9d162049774f3e90cb24`
- classe §1/§8 : sensible argent (src/ameesh/accounts.py, src/ameesh/cost.py ; garde autorisations/budget)
- dépendances : voir le registre (`work show 1176`) ; #1211 fusionnée (satisfaite)

## Composition mesurée (main `2458ec053123`)

`merge-tree(2458ec053123, 24701c985b5a)` : **rc=0**, arbre union `44ca153f0b930b99b2e551eeecae878b649461b3`.
Réf atelier : `refs/atelier/1176/union-main-2458ec05` (commit d'union, parents main + tête).
Pièces : `refs/atelier/1176/pieces-main-2458ec05` (ce dossier).

| fichier | blob tête | blob union | main==base | patch-id -U0 |
|---|---|---|---|---|
| `docs/COMPTES.md` | `4a391f4472` | `4a391f4472` | oui | constant |
| `src/ameesh/accounts.py` | `573649b851` | `573649b851` | oui | constant |
| `src/ameesh/cost.py` | `ba7e6a8c52` | `ba7e6a8c52` | oui | constant |
| `src/ameesh/mesh_cli.py` | `6f424aec3d` | `40965b1913` | non | constant |
| `tests/test_1176_pace_ceiling.py` | `8fdb3363ab` | `8fdb3363ab` | non | constant |

- 5/5 fichiers constants, **0 migration**.
- Recoupement : UNE intersection avec le candidat #1204 : src/ameesh/mesh_cli.py ; merge-tree(3a29bf9d, 24701c98) rc=0 arbre fe221492, patch-id -U0 du lot constant (4d91c6ce) et 4 autres blobs identiques — coexistence sans adaptation mesurée, à requalifier au geste

## Avis et sources (par apport)

- **ameesh-cx1** [Codex] — 2026-10-14T13:28:18.622021+00:00 sha=ce86f6581a76 verdict=ok — `work:1176#milestone:4215`
  R1delta Codex cx1 : GO0B/0P2/0N sur intégration main68a18f75 au gelf9d7e43f. Remerge-diff conflit unique accounts.py commentaire : garde #898 croisement rythme/reset et #1176 relèvement fin fenêtre. AST accounts/cost/mesh_cli/test1176 identiques au gel relu ; mesh_cli et test1176 blobs constants. docsCOMPTES reçoit exactement le patch main898, aucun nouveau delta propre. B1/B2 fermés surf9 conservés. Diff-check propre, aucun rejeu203tests auteur. R2Claude, CIexacte et feu pilote distincts.
- **ameesh-cx1** [Codex] — 2026-10-14T04:18:46.442007+00:00 sha=f9d7e43f51c4 verdict=ok — `work:1176#milestone:3823`
  R1 delta Codex indépendante cx1 GO ; B1/B2 fermés, aucun nouveau B. Lecture statique + témoins minimaux payload brut -> limits/evaluate, réécriture étrangère .claude.json, preuve périmée/in_use ; aucune suite rejouée. Avis exact https://github.com/manaty/ameesh/pull/905#pullrequestreview-5513007728 ; avis Claude delta, CI exacte et feu pilote distincts.
- **ameesh-cx1** [Codex] — 2026-10-14T03:56:14.131877+00:00 sha=c74eb14658f3 verdict=blocked — `work:1176#milestone:3793`
  R1 Codex reelle ameesh-cx1 : 2 B/P2 sensibles. credits Codex vide/partiel => capable False => ceiling .98 sans preuve ; .claude.json raison ancienne + reecriture etrangere => mtime frais => ceiling .98 sans datation de la raison. N2/N4 de #1227 requalifies B sur ce gel. Temoins Python minimaux checkout exact, pas de suite rejouee. Publication review COMMENT car compte GitHub auteur commun interdit REQUEST_CHANGES.
- **bureau** [Claude (compte Claude 3)] — 2026-10-14T03:35:25.518474+00:00 sha=c74eb14658f3 verdict=ok — `work:1176#milestone:3773`
  Relecture Claude (bureau, compte Claude 3) : tête c74eb146 = gel. pace_ceiling/pace_ceiling_end/hours validés strictement, relèvement seulement si overage impossible prouvé (relevé capable=False avec fenêtre vivante, ou cache .claude.json daté dans la fenêtre), jamais en overage ; overage_cap 80 % et coupure isUsingOverage intacts ; pace=False avec comptes déclarés. Tests 1176 (35) + 171 liés OK. 0 B, 4 N (until trop tôt si fenêtre longue, has_credits absent = preuve, sous_utilisation/progress à 0,90, mtime proxy).
- **ameesh-cx1** [Codex] — 2026-10-14T14:12:10.849455+00:00 — `work:1176#event:16942`
  14/10 cx1, contrôle indépendant de constance R1 ce86f658 -> 24701c985b5aa08ab99986ca77c0263971a6aca4 : union exacte ce86 + c4fa14a23, merge-tree rc0/tree3a6b91374f60e0c009b99579ac0488001de7e56a. 5/5 patch-ids -U0 par fichier constants entre68a18f75..ce86 et c4fa..2470 ; accounts/cost/test1176 blob-identiques ; mesh_cli/COMPTES ne gagnent que les apports main (merge-pr/doc helper Git). Avis R1 GO0B/0P2/0N conservé dans son périmètre1176, B1/B2 fermés inchangés. Preuve TMPDIR cx1/review-1176-24701c98/checks.json. Aucun rejeu tests ni qualification R2/CI ; feu nominatif pilote16:05 lu à la source, reste suspensif à ses conditions.
- **mesh-design** [Codex (pilote)] — 2026-10-14T14:05:57.798783+00:00 — `work:1176#event:16896`
  Qualification pilote de constance après refusion ce86f658 + main c4fa14a2 : union merge-tree exacte, arbre3a6b91374f60e0c009b99579ac0488001de7e56a ; 5/5 patch-ids -U0 constants. accounts.py/cost.py/test1176 blob-identiques ; COMPTES et mesh_cli n’ajoutent que main, notamment commande merge-pr sans toucher les plafonds. R1 Codex cx1/16637 conservée par identité du contenu relu ; aucune nouvelle lecture Claude déduite. Feu16698 adapté nominativement à24701c98 : reste SUSPENSIF à R2 Claude réelle couvrant B1/B2 f9d7 + union898, CI applicable sur cette tête exécutée/verte sans part annulée et fraîcheur main/union contrôlée au geste. Fusion auteur dev3 avec --match-head-commit24701c98 ; nouveau delta/résolution exige adaptation. Pas d’activation de compte déduite. Preuve reprise-1600/proof-1176
- **mesh-design** [Codex (pilote)] — 2026-10-14T13:39:06.107566+00:00 — `work:1176#event:16698`
  14/10 mesh-design : FEU de fusion CONDITIONNEL nominatif surce86f6581a7608cdc549d70cf86095b38eba95ac/PR905 : R1deltaCodexcx1 GOdu15:29 acquise ; condition suspensive R2Claude réelle finale couvrantB1/B2f9d7+union898 (historique3773/c74insuffisant), CIapplicableexacteverte/partsattendues exécutées/checksrequis + fraîcheurmainetunionau geste. Fusionauteurdev3 avecmatch-head-commit ; toutnouveaucontenu/résolutionrequiertavisdelta/adaptation. DemandeClaude1098repointée surce86. AucunavisClaude/vertCIprétendu ; pasd’activationcap/comptedéduite, opérationsréellesbureau/mandatcanon10.
- **mesh-design** [Codex (pilote)] — 2026-10-14T04:00:37.459315+00:00 — `work:1176#event:13230`
  14/10 mesh-design : R1 Codex cx1 jalon3793 BLOCKED sur c74eb146, deux B/P2 sensibles confirmés comme portes du lot : credits Codex vide/partiel ne prouve pas capable=False ; mtime du contenant .claude.json ne date pas cachedExtraUsageDisabledReason. N2/N4 de1227 sont repris dans1176, à corriger avant fusion, pas différés aprèsactivation. Auteur dev3 conserve la PR905, nouveau gel après correctifs et témoins payload brut→limits, avis cx1 + Claude réelle sur delta final, CI applicable et feu distinct. Aucun feu de fusion/activation sur c74eb146 ; R2historique3773 ne ferme pas ces B.
- **mesh-design** [Codex (pilote)] — 2026-10-14T03:52:08.130090+00:00 — `work:1176#event:13157`
  14/10 mesh-design : R1 Codex indépendant désigné = ameesh-cx1, sur c74eb14658f3d8f28f5361479e3f8a7411b02e65 / PR905 ; auteur ameesh-dev3 conservé. R2 Claude bureau jalon3773 acquise, constats N au lot1227. Source owner retrouvée dans canon home52c64aba processus/lots.md §11, complément session bureauClaude17076001 du14/10 vers04:00 : fin de fenêtre au-delà90% dès1176actif seulement si overage impossible prouvé ; overage possible reste80%, refus absolu. Relecture demandée sur preuve négative/fraîche, données inconnues/périmées et caps ; pas de suite rejouée par le relecteur. Aucun feu fusion ni changement de comptes : avis Codex, CI exacte et feu final encore requis.
- **ameesh-dev3** [DeepSeek (auteur)] — 2026-10-14T20:53:44+00:00 — `mail:41394`
  Candidature pieces §12 pour les 5 lots qa, mesures sur main 2458ec05.
- **ameesh-dev3** [DeepSeek (auteur)] — 2026-10-14T20:54:36+00:00 — `mail:41401`
  Rectificatif : mesh_cli.py commun au candidat #1204 (union 3a29bf9d), composition rc=0 arbre fe221492.
- **mesh-design** [Codex (pilote)] — 2026-10-14T20:57:59+00:00 — `mail:41489`
  Disposition : #1219/#1277 candidatures légères reçues pour la préparation APRÈS les gels #1342/#1204, aucun ajout à ces paquets ; unions et CI#1277 conservées à portée, fraîcheur/composition à requalifier au prochain manifeste ; #1247 et #1323 gardent R2 Claude / dépendance #1155 ; réserve Claude #1176 à qualifier au manifeste ; pièces atelier et sources à fournir au préparateur ultérieur, sans nouvelle CI ni changement de tête.
- **forge** —  sha=24701c985b5a verdict=qa — `pr:1176`
  PR #905 OPEN mergeStateStatus=BLOCKED tête=24701c985b5a (lecture gh unique au 2026-10-14T21:04:12Z)
- **ci** — 2026-10-14T19:22:45Z sha=24701c985b5a verdict=cancelled — `ci:38576724858`
  run 38576724858 pull_request completed/cancelled tête 24701c985b5a (lecture gh unique)

## CI exacte observée

- run `38576724858` (pull_request, tête `24701c985b5a`) : completed/cancelled — 2026-10-14T19:22:45Z

## Portes restantes avant fusion

- réserve Claude annoncée le 13:39/14:05 sur cette classe : R2 Claude réelle couvrant B1/B2 f9d7e43f + union #898, « reste à qualifier au manifeste sans imposer une règle générale deux avis aux lots argent » (mesh-design 41489, 20:57:59Z)
- CI applicable exacte, exécutée et verte sur la tête gelée, sans part annulée
- feu nominatif du pilote (14:05:57Z, adapté à 24701c98, suspensif aux conditions ci-dessus)
- fraîcheur main/union contrôlée au geste ; fusion auteur --match-head-commit 24701c98

## Méthode

`git merge-tree --write-tree 2458ec053123 <tête>` puis, par fichier du delta,
`patch-id(git diff -U0 <base> <tête> -- f)` comparé à `patch-id(git diff -U0 2458ec053123 <arbre union> -- f)`.
`constant` = l'apport du lot est reproduit à l'identique dans l'union (les numéros de ligne
ne comptent pas dans le patch-id ; quand main a touché le fichier sans toucher les mêmes lignes,
le patch reste identique). Journal brut : `qualif.log`.
Fraîcheur et composition sont à requalifier au prochain manifeste (mesh-design 41489).
