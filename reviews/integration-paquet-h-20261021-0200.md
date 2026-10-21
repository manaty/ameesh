# Paquet integration/paquet-h-20261021-0200 — composition (§12)

Base : `main` **`d413549d1c22`** (fusion du paquet h-2200, PR #2214,
21/10 01:42 Europe/Paris). Branche `integration/paquet-h-20261021-0200`,
cible `main`, lot porteur #2917. Une seule suite complète sur la tête finale.

Composeur : mty-amh-impl-01 (impl-01), désigné par l'alerte de créneau #121067
(21/10 00:16 UTC) ; pilote : mesh-design. Créneau 02:00 Paris manqué, traité
dans la foulée.

## Admissions (4 unités, 4 lots portés)

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #2888 | `e47f3a41c9dd` (tête gelée, gel 14812) | sensitive | cx2 (Codex) + auditeur (Claude réel) | indépendance des avis : toute correction de relecteur est un auteur, sans exception de taille ; désignation, porte et train alignés sur le canon §2 ; auteur impl-01 (DeepSeek) |
| #2908 | `f16782a9b119` (tête gelée, gel 14909) | sensitive | cx2 (Codex) + qa-01 (DeepSeek) | train : un porteur `integration/**` n'est jamais membre d'un paquet (inventaire et admission) ; auteur mesh-design (Claude) |
| #2893 | `3951676045dc` (tête gelée, gel 14918) | sensitive | cx3 (Codex) + dev1 (Claude) | spend_pending exact : clé et montants exacts, migration additive 0471 ; auteur dev5 (DeepSeek) |
| #2899 | `c15806cda0ae` (tête gelée, gel 14866) | sensitive | cx1 (Codex) + dev1 (Claude) | staging : santé sans exécuteur et courrier au profil porte-clé ; auteur impl-06 (DeepSeek) |

## Composition (4 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2888 | `e47f3a41c9dd` | `72368bbd7` | sensitive ; 0 conflit |
| 2 | #2908 | `f16782a9b119` | `3c7f98434` | sensitive ; 0 conflit |
| 3 | #2893 | `3951676045dc` | `b93354d57` | sensitive ; 0 conflit |
| 4 | #2899 | `c15806cda0ae` | `55633f56c` | sensitive ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Résolutions de composition

Aucune : 4 fusions propres, 0 conflit ; `git diff --check` propre.

## Écartés (ce véhicule)

Aucun lot prêt écarté : les 4 lots prêts de la liste calculée sont composés.

## En attente au calcul (non admis)

| lot | motif |
|---|---|
| #382 | `verdicts-missing` : auteurs cx3 (Codex) + impl-04 (DeepSeek), seul dev1 (Claude) accepté ; aucune seconde famille indépendante |
| #286 | `verdicts-missing` : auteurs cx3 (Codex) + dev1 (Claude) + impl-03 (DeepSeek) ; aucune famille indépendante |
| #2906 | `verdicts-missing` : un seul avis dev2 (Claude) |
| #2892 | `verdicts-missing` : un seul avis qa-01 (DeepSeek) |
| #2900 | `verdicts-missing` : aucun avis ok sur le gel |
| #114 | `verdicts-missing` : un seul avis impl-07 (DeepSeek) |
| #2771 | `verdicts-missing` : un seul avis qa-01 (DeepSeek) |
| #2895 | `verdicts-missing` : aucun avis ok sur le gel (léger) |
| #2894 | `blocked-verdict` : gel effectif refusé |
| #2614 | `blocked-verdict` : gel effectif refusé |
| #2352 | `branch-not-published` : `canon/2352-slo-paquet` absente de origin |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py --repo . --target main` sur
  `d413549d1c22` : **4 prêts, 11 en attente, 0 erreur** (rc 0).
- Écart avec l'alerte #121067 : elle listait 6 lots prêts (#2888, #2908, #382,
  #2893, #2899, #286). Le recalcul sur la règle du canon §2 (correction de
  relecteur = auteur, sans exception de taille) refuse #382 et #286 : leurs
  correcteurs cx3/dev1 sont devenus auteurs du gel, ce qui ne laisse aucune
  famille indépendante (motifs ci-dessus). C'est la divergence corrigée par
  #2888, présent dans ce paquet ; les deux lots restent ouverts, hors véhicule.
- Contenu : 30 fichiers, +2340/−331 hors manifeste ; `git diff --check`
  propre ; une seule migration nouvelle (`0471_spend_pending_exact_key.sql`,
  additive).
