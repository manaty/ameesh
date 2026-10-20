# Paquet integration/paquet-h-20261020-1700 — composition (§12)

Base : `main` **`9baa8e821014`** (fusion du paquet h-1300, PR #2194, 20/10
17:15). Branche `integration/paquet-h-20261020-1700`, cible `main`, lot
porteur #2886. Une seule suite complète est demandée sur la tête finale.

Composeur : mesh-design (pilote ; aucun paquet ouvert après la fusion de
h-1300, bureau 17:26 ; impl-01, composeur désigné, prévenu).

## Admissions (5 unités, 5 lots portés)

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #2848 | `d70d12d3cdc6` (tête gelée, gel 14765) | sensitive | auditeur (Claude) + cx1 (Codex) | panier du lot 2845 : réveil de l'auteur à l'intake, tests du veilleur sous psql ; auteur impl-07 |
| #2863 | `5fe81731646f` (tête gelée, gel 14689) | sensitive | dev3 (DeepSeek) + orchestrateur (Claude) | gel : `--sha HEAD` symbolique refusé, abréviations développées ; auteur cx2 |
| #2879 | `def90d019870` (tête gelée, gel 14718) | sensitive | auditeur (Claude) + cx3 (Codex) | alerts : tendance disque sous-échantillonnée en base ; auteur impl-04 |
| #2675 | `352dacb2ddb1` (tête gelée, gel 14694) | sensitive | auditeur (Claude) + cx3 (Codex) | rythme des familles lu sur l'horizon du plan ; auteur impl-08 |
| #2884 | `ea4f83dc242c` (tête gelée, gel 14741) | sensitive | auditeur (Claude) + dev3 (DeepSeek) | gel revenu sur une tête déjà relue ; auteur cx3 |

## Composition (5 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2848 | `d70d12d3cdc6` | `4f6eeb948` | sensitive ; 0 conflit |
| 2 | #2863 | `5fe81731646f` | `5e7c74c5f` | sensitive ; 0 conflit |
| 3 | #2879 | `def90d019870` | `73dcd73fa` | sensitive ; 0 conflit |
| 4 | #2675 | `352dacb2ddb1` | `be9406e37` | sensitive ; 0 conflit |
| 5 | #2884 | `ea4f83dc242c` | `9d06f53f9` | sensitive ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Résolutions de composition

Aucune : les cinq fusions sont propres (aucun conflit, aucun marqueur).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #114 | `verdicts-missing` : deux avis de familles distinctes requis sur `89bdc6b50e93` |
| #2614 | `no-verdict` : gel effectif refusé |
| #2771 | `no-verdict` : gel effectif refusé |
| #2825 | `blocked-verdict` : gel effectif refusé |
| #2877 | `conflict` avec `main` (docs/EXECUTEUR-MEDIEE.md, comptes) ; remise `5fbd6b0ade1f` : un avis sur deux (dev3) |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py`, base `9baa8e821014` :
  **5 prêts, 5 en attente, 0 erreur** ; les 5 prêts sont composés, aucun
  écarté.
- Composition : **5 unités, 5 lots** ; aucun conflit, aucune résolution.
- 22 fichiers, +1034/−164 hors manifeste ; `git diff --check` propre ;
  aucune migration.
- Ciblés sur l'arbre composé : test_1645, test_2022, test_2542, test_2668,
  test_2845, test_2851, test_2879, test_2884, test_l107 — 175 verts.
