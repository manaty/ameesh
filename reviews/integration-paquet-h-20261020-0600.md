# Paquet integration/paquet-h-20261020-0600 — composition (§12)

Base : `main` **`6248cc7ce0fb`** (fusion du paquet h-0500, PR #2148, 20/10
04:22:37Z). Branche `integration/paquet-h-20261020-0600`, cible `main`. Une
seule suite complète est demandée sur la tête finale.

Demandes : bureau 04:35Z — « h-0500 fusionné (06:20). 5 lots ameesh
« waiting: merge » : compose le paquet h-0600 maintenant ; je le fusionne au
vert. » Les cinq lots sont exactement la liste calculée
`scripts/paquet-candidats.py` à 04:39Z sur `6248cc7ce0fb` : **5 prêts,
7 en attente, 0 erreur** (`rc 0`, `--ready-out` écrit).

## Admissions (5 unités, 5 lots portés)

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #2792 | `e886568eccf1` (tête gelée) | sensitive | auditeur (Claude) + cx2 (Codex) | panier #2766 : `tests/parts/` reste un delta relu, le plan de CI n'est pas une adaptation de test ; deux avis dus (`merge_queue.py`, `packet_candidates.py` critiques) ; auteur impl-07 |
| #2803 | `0bbe5369aa80` (tête gelée) | sensitive | cx2 (Codex) + auditeur (Claude) | porte de fusion : sur un véhicule, les auteurs sont ceux de son delta relu, pas le composeur ; deux avis dus (`merge_queue.py` critique) ; auteur dev3 |
| #2665 | `25e2116648b2` (tête gelée) | sensitive | cx1 (Codex) + cx2 (Codex) | prévalidation : découverte des canons et attente locale bornées par l'échéance, docstring `_prevalidation_red` corrigée ; un avis requis (sensible hors SQL/prod), deux acquis ; auteur impl-06 |
| #2837 | `23ae9546f20f` (tête gelée) | light | auditeur (Claude) | SLO review GO → merge : la composition d'un lot dans un véhicule compte comme atterrissage ; un avis ; auteur dev5 |
| #2801 | `08186ef942af` (tête gelée) | light | auditeur (Claude) | panier #381 : routage du fil lu côté serveur, pluriel `chat.unread` aligné dans `en.json`, bundle reconstruit ; un avis ; auteur impl-08 |

## Composition (5 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2792 | `e886568eccf1` | `d656654f1cdb` | sensitive ; 0 conflit |
| 2 | #2803 | `0bbe5369aa80` | `62fc9d06bc47` | sensitive ; 0 conflit |
| 3 | #2665 | `25e2116648b2` | `e85d31027153` | sensitive ; 0 conflit |
| 4 | #2837 | `23ae9546f20f` | `7d422202d68b` | light ; 0 conflit |
| 5 | #2801 | `08186ef942af` | `2ce1999a49fe` | light ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`
(`scripts/paquet-compose.py --units --base origin/main` : 5 unités de fusion,
5 lots portés).

## Résolutions de composition

Aucune : les cinq fusions sont propres (`git status` sans conflit, aucun
marqueur de conflit). Le seul contenu hors fusions est ce manifeste.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #114 | `verdicts-missing` : un seul avis (impl-07, DeepSeek) sur le gel `89bdc6b50e93`, deux avis de familles distinctes requis (sensible) |
| #1515 | `conflict` avec `main` sur `src/ameesh/fire.py`, `src/ameesh/merge_queue.py`, `tests/test_1003_merge_pr.py` ; aucune remise sur `6248cc7ce0fb` |
| #2614 | `no-verdict` : gel effectif refusé |
| #2771 | `no-verdict` : gel effectif refusé |
| #2786 | `conflict` avec `main` sur `src/ameesh/plan_git.py` ; remise `main-797779c2` (`018799cd`) refusée : un seul avis (auditeur, Claude) sur le delta, non mécanique — un second avis d'une autre famille est requis |
| #2813 | `blocked-verdict` : gel effectif refusé |
| #2814 | `verdicts-missing` : un seul avis (auditeur, Claude) sur le gel `31739fe46624`, deux avis de familles distinctes requis (`0469_host_cgroup.sql` critique) |

Note 05:08Z : #2813 est depuis relu sur son gel `e5f62e692ff4` (avis cx3, cx2
et impl-04, courrier mesh-design) ; il n'entre pas dans ce véhicule, composé à
04:46 sur la liste de 04:39, et passe au paquet suivant.

## Contrôles

- Liste calculée `scripts/paquet-candidats.py`, base `6248cc7ce0fb` :
  **5 prêts, 7 en attente, 0 erreur** (`rc 0`) ; les 5 prêts sont composés,
  aucun écarté. Premier calcul à 04:38Z en `rc 1` : blob `d5a61e41` absent du
  clone jetable (dépôt agent en clone partiel), matérialisé localement ;
  second calcul complet, aucun lot perdu.
- Composition : **5 unités, 5 lots** ; 34 fichiers, +2049/−265 hors manifeste ;
  aucun conflit, aucune résolution ; `git diff --check` ne relève que les
  blancs du bundle minifié déjà signalés (faux positif de la ligne minifiée).
- `scripts/paquet-inclusion.py` (manifeste, tête de composition, attendus et
  prêts) : **aucun défaut** — 5 compositions, 0 exclusion, contenus à la tête :
  5 présents, 0 partiel.
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée.
- `scripts/doc-links.py` : 172 fichiers, **0 lien cassé**.
- `scripts/freeze-checks.py` : **6 contrôles verts**.
- UI sur l'arbre composé : `npm test` **67/67**, `npm run check` vert
  (bundle reproductible, budget 23 991 / 81 920 octets).
- Migrations : **aucune** au diff (`git diff --name-only` sans `.sql` ni
  `migrations/`).
- Tests ciblés Python : **454 tests, 0 échec** sur le pilote psql local
  (`#2792`, `#2803`, `#1770`, `#2644`, L159b, L1616, `#789`, L368,
  `test_1003_merge_pr`, `test_l88_ui`) ; le pilote psycopg n'est pas rejoué
  localement, la suite complète le couvre en CI.
- Prévalidation : indisponible (carte périmée) ; sélection non sûre, la suite
  complète est demandée (`--full`), motif journalisé.
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
