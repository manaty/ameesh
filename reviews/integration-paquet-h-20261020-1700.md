# Paquet integration/paquet-h-20261020-1700 — composition (§12)

Base : `main` **`9baa8e821014`** (fusion du paquet h-1300, PR #2194, 20/10
17:15). Branche `integration/paquet-h-20261020-1700`, cible `main`, lot
porteur #2886. Une seule suite complète est demandée sur la tête finale.

Composeur : mesh-design (pilote ; aucun paquet ouvert après la fusion de
h-1300, bureau 17:26 ; impl-01, composeur désigné, prévenu).

## Admissions (7 unités, 7 lots portés)

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #2848 | `d70d12d3cdc6` (tête gelée, gel 14765) | sensitive | auditeur (Claude) + cx1 (Codex) | panier du lot 2845 : réveil de l'auteur à l'intake, tests du veilleur sous psql ; auteur impl-07 |
| #2863 | `5fe81731646f` (tête gelée, gel 14689) | sensitive | dev3 (DeepSeek) + orchestrateur (Claude) | gel : `--sha HEAD` symbolique refusé, abréviations développées ; auteur cx2 |
| #2879 | `def90d019870` (tête gelée, gel 14718) | sensitive | auditeur (Claude) + cx3 (Codex) | alerts : tendance disque sous-échantillonnée en base ; auteur impl-04 |
| #2675 | `352dacb2ddb1` (tête gelée, gel 14694) | sensitive | auditeur (Claude) + cx3 (Codex) | rythme des familles lu sur l'horizon du plan ; auteur impl-08 |
| #2884 | `ea4f83dc242c` (tête gelée, gel 14741) | sensitive | auditeur (Claude) + dev3 (DeepSeek) | gel revenu sur une tête déjà relue ; auteur cx3 |
| #2877 | `5fbd6b0ade1f` (remise `refs/ameesh/resolutions/2877/main-9baa8e821` du gel `1c368857bc0a`) | sensitive | gel : cx3 (Codex) + dev3 (DeepSeek) ; remise : dev3 + cx3 | convoi de verrous du registre : la passe des agents arrêtés ne verrouille que s'il y a un lot à rendre ; remise = comptes du contrat médié 411/481 ; auteur mesh-design |
| #2825 | `885545e78694` (tête gelée) | sensitive | cx2 (Codex) + auditeur (Claude) | panier du lot 2814 : périmètre de chaque hôte lu dans son relevé ; auteur impl-05 |

## Composition (7 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2848 | `d70d12d3cdc6` | `4f6eeb948` | sensitive ; 0 conflit |
| 2 | #2863 | `5fe81731646f` | `5e7c74c5f` | sensitive ; 0 conflit |
| 3 | #2879 | `def90d019870` | `73dcd73fa` | sensitive ; 0 conflit |
| 4 | #2675 | `352dacb2ddb1` | `be9406e37` | sensitive ; 0 conflit |
| 5 | #2884 | `ea4f83dc242c` | `9d06f53f9` | sensitive ; 0 conflit |
| 6 | #2877 | `5fbd6b0ade1f` | `f95862a2d` | sensitive ; 0 conflit (remise relue) |
| 7 | #2825 | `885545e78694` | `4fc6db1a4` | sensitive ; 1 conflit mécanique (ci-dessous) |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Résolutions de composition

| commit | conflit | nature | avis |
|---|---|---|---|
| `4fc6db1a4` | #2825 × #2879 dans `mesh_cli._host_fleet` : #2825 change `limits` (périmètre lu dans le relevé), #2879 la ligne voisine `history` (`disk_history` sous-échantillonné) | hunks adjacents, chaque côté repris tel quel (limits de #2825, history de #2879), aucune ligne nouvelle | aucun (§2, conflit mécanique) ; auteurs prévenus |

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #114 | `verdicts-missing` : deux avis de familles distinctes requis sur `89bdc6b50e93` |
| #2614 | `no-verdict` : gel effectif refusé |
| #2771 | `no-verdict` : gel effectif refusé |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py`, base `9baa8e821014` :
  **7 prêts, 3 en attente, 0 erreur** (relue après les verdicts de #2825 et de
  la remise #2877) ; les 7 prêts sont composés, aucun écarté.
- Composition : **7 unités, 7 lots** ; un conflit mécanique résolu (`4fc6db1a4`).
- 34 fichiers, +2165/−280 hors manifeste ; `git diff --check` propre ;
  aucune migration.
- Ciblés sur l'arbre composé : test_1645, test_2022, test_2542, test_2668,
  test_2845, test_2851, test_2879, test_2884, test_l107 — 175 verts ; après
  les unités 6 et 7 : test_2022, test_2814, test_l43, test_2879, test_2851,
  test_2877, test_1425, test_l107, test_1391, test_743 — 175 verts.
