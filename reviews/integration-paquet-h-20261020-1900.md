# Paquet integration/paquet-h-20261020-1900 — composition (§12)

Base : `main` **`74db4b425c728`** (fusion du paquet h-1700, PR #2204, 20/10
19:10 Paris). Branche `integration/paquet-h-20261020-1900`, cible `main`, lot
porteur #2887, PR #2207. Une seule suite complète est demandée sur la tête
finale.

Composeur : mty-amh-impl-01 (bureau 17:24Z ; pilote mesh-design informé).

## Admissions (1 unité, 1 lot porté)

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #2810 | `a8b828ba59cc` (tête gelée, gel 14780) | sensitive | cx2 (Codex), verdict 14802 | panier du lot 2729 : le NOTIFY d'un urgent déjà servi par le tour ne préempte plus ce tour ; auteur mty-amh-qa-01 |

## Composition (1 unité)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2810 | `a8b828ba59cc` | `e89901fc6` | sensitive ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: 2810`.

## Résolutions de composition

Aucune : la fusion est propre. La remise `86f04d9f5a6d` (#286 × #2810) reste
hors véhicule : #286 n'est pas prêt (waiting_human).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #114 | `verdicts-missing` : un seul avis (mty-amh-impl-07, deepseek) sur `89bdc6b50e93` ; deux familles requises (sensible) |
| #2614 | `verdicts-missing` : aucun avis ok sur le gel `43b17132` (sensible, deux familles requises) |
| #2771 | `no-verdict` : gel effectif refusé (pas de gel) |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py`, base `74db4b425c728` :
  **1 prêt, 3 en attente, 0 erreur** ; le prêt (#2810) est composé.
- Composition : **1 unité, 1 lot** ; aucun conflit.
- 2 fichiers, +262/−4 hors manifeste ; `git diff --check` propre ; aucune
  migration.
- Ciblés sur l'arbre composé : test_2810_preemption_servi (5/5),
  test_2729_preemption_streak, test_l795_preemption_evenement — **27 verts**.
- Inclusion : `scripts/paquet-inclusion.py` sur la tête `e89901fc6` — 1
  composition, 1 présence, 0 omission, 0 absence ; **aucun défaut**.
