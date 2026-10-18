# Paquet integration/paquet-h-20261018-0800 — composition

Base : `main` **`856e23f87e9a`** (fusion de la PR #1769, paquet h-0700, 18/10 06:32Z ; arbre identique au gel `0f2d407dc`). Aucun paquet ouvert ; `main` n'a pas bougé pendant la composition.

Demandes : **orchestrateur** 18/10 06:31Z (candidats h-0800 : #2357, #2393, #2211) puis 06:35Z (#2387, SLO 08:35, verdict acquis) ; **#2388 ajouté** sur admission vérifiée (gel `be2019620`, verdicts cx3 + mesh-design, prêt, absent de la liste). **Correction de gel #2211** : la liste citait `ae49b7d1`, mais `ameesh-cx1` a poussé la correction `35ed6195` après un verdict blocked sur `ae49b7d1`, re-gel sur `35ed6195` et verdict mesh-design ok sur cette tête.

## Admissions (5 lots, 5 unités)

- #2357, #2393, #2211, #2387, #2388.

## Composition (5 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2357 | `2ee1c5456d47` (tête gelée) | `86bcfb266` | verdict codex1 ok sur 2ee1c545 ; deploy/poste.sh + staging.sh, BASCULE.md ; sensible déploiement, un avis d'une autre famille (hors prod réelle) |
| 2 | #2393 | `55087ad1ff41` (tête gelée) | `18b185d65` | verdict deepseek4 ok sur 55087ad1 ; roadmap.py : les dates de livraison manuelles gardent l'alerte overdue ; relecture après fusion #2342, lot léger |
| 3 | #2211 | `35ed6195cbf9` (re-gel cx1 après blocked) | `886fb1d81` | verdict mesh-design ok sur 35ed6195 ; N1-N3 + mode du temporaire d'export ; lot léger |
| 4 | #2388 | `be20196207de` (tête gelée) | `6a3f9cd26` | verdicts ameesh-cx3 (Codex) et mesh-design (Claude) ok sur be2019620 ; radar : un seul survivant par groupe de migrations héritées ; sensible concurrence/CI |
| 5 | #2387 | `0f2c6d75cd6d` (tête gelée) | `73dc987b1` | verdict mesh-design ok sur 0f2c6d75 ; composeur `--units` : unités de fusion et lots portés ; outil et docs, lot léger |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot et il porte un trailer `ameesh-work: <lot>` (5 trailers de composition).

## Conflits et résolutions

- Aucun : `git merge-tree --write-tree` rc 0 pour chaque tête sur la base `856e23f87`, et les cinq compositions (`scripts/paquet-compose.py`) ont fusionné proprement. Aucune résolution n'est utilisée.

## Preuves

- `git diff --stat 856e23f87..HEAD` : **13 fichiers hors ce manifeste, +923/−91** (14 fichiers avec le manifeste), dont `src/ameesh/radar.py`, `src/ameesh/roadmap.py`, `src/ameesh/roadmap_edit.py`, `deploy/mise-a-jour/poste.sh`, `deploy/mise-a-jour/staging.sh`, `docs/BASCULE.md`, `scripts/paquet-compose.py`, `docs/PAQUET-INCLUSION.md`.
- 5 unités en premier parent, 5 trailers `ameesh-work` ; têtes sources : `2ee1c5456d47`, `55087ad1ff41`, `35ed6195cbf9` (et non `ae49b7d1`), `be20196207de`, `0f2c6d75cd6d`.
- `git diff --check 856e23f87..HEAD` : rc 0, aucun marqueur de conflit.
- Tests ciblés locaux sur l'arbre composé : #2388 8 OK ; radar #2279/#1333/l160b 43 OK ; #2357 `test_l232b_release_scripts` 15 OK ; roadmap #l96/#2211/#2179 47 OK ; #2387 `test_2157_paquet_compose` 8 OK.
- `scripts/paquet-inclusion.py --manifest reviews/integration-paquet-h-20261018-0800.md --head HEAD --expected 2357,2393,2211,2387,2388` : rc 0, 5/5 présents.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2374 | verdicts anciens sur `c732bf07e`, destiné au paquet suivant par le pilote et docs-01 (05:24Z) |
| #1359 | gel `50cec010`, verdict dev2 ; admis au paquet suivant, pas à cette tête |
| #2267 | empilé, relecture R2 dev1 due (orchestrateur 06:31Z) |
