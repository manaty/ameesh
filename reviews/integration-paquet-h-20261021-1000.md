# Paquet integration/paquet-h-20261021-1000 — composition (§12)

Base : `main` **`60a3ac972cf5`** (fusion du paquet h-0900, PR manaty/ameesh#2291,
tête gelée `c689c00eb4ee`, 21/10 09:11 UTC). Branche
`integration/paquet-h-20261021-1000`, cible `main`, lot porteur #2998. Une
seule suite complète sur la tête finale.

Composeur : mty-amh-impl-01, pour le pilote mesh-design (courrier #123224 du
21/10 09:32, lot #2998 créé à 09:38) ; aucun autre paquet composé en
parallèle. Composition faite directement sur `main` `60a3ac972` après la
fusion de h-0900.

## Admissions (7 unités, 7 lots portés)

| lot | source | classe | avis (familles) | auteur | objet |
|---|---|---|---|---|---|
| #2916 | `c4d845e669f4` (gel #15357) | sensitive | 2 avis ok (claude + codex) | ameesh-dev3 | SLO intake_started : le délai court pendant l'attente d'une décision ou d'un hold |
| #2968 | `31fe44b65b8b` (gel #15335) | sensitive | 2 avis ok (claude + codex) | mty-amh-impl-03 | Relecture après fusion : le dépôt de l'assigné prime même s'il n'est pas l'auteur |
| #2977 | `a9cd62903036` (gel #15475) | sensitive | 2 avis ok + avis deepseek supplémentaire | ameesh-dev4 | Porte #2924 : la preuve de contenu ne tient qu'à une occurrence de plus au même chemin |
| #2973 | `898b36ccd0ce` (gel #15334) | sensitive | 2 avis ok (claude + codex) | ameesh-dev4 | Canon : chemins Process relatifs au scope (carte TPK sous ameesh/processus) |
| #2988 | `e435d81a253b` | light | avis d'une autre famille | mty-amh-impl-06 | Intake depuis une revue : sortir la logique d'intake de work.py vers la CLI |
| #288 | `a21a52f13ac6` (gel #15421) | sensitive | ameesh-cx2 (codex) + ameesh-dev2 (claude) | mty-amh-impl-03 | L80b : cost per resident message — contrat 1.7 `turn_costs.inputs`, compatibilité 1.6, migration 0472 additive |
| #2895 | `31b40fc899db` | light | avis d'une autre famille | ameesh-dev3 | TPK #2202 : tours relecteur bornés en bac à sable externe (conception) |

## Composition (7 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2916 | `c4d845e669f4` | `66d6bf6e0` | fusion de la tête gelée, 0 conflit |
| 2 | #2968 | `31fe44b65b8b` | `4c044d4ef` | fusion de la tête gelée, 0 conflit |
| 3 | #2977 | `a9cd62903036` | `6319a1d5a` | fusion de la tête gelée, 0 conflit |
| 4 | #2973 | `898b36ccd0ce` | `8df6762ec` | fusion de la tête gelée, 0 conflit |
| 5 | #2988 | `e435d81a253b` | `a0c3cecc7` | fusion de la tête gelée, 0 conflit |
| 6 | #288 | `a21a52f13ac6` | `b59b237b9` | fusion de la tête gelée, 0 conflit |
| 7 | #2895 | `31b40fc899db` | `f8079fb25` | fusion de la tête gelée, 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Résolutions de composition

Aucune : les sept fusions sont propres.

## Écartés (ce véhicule)

| lot | source | motif |
|---|---|---|
| #2914 | `2df95bee4d8b` | **retiré** : gel antérieur #14991 fantôme `66a06cf09d5c`, la porte de fusion refuse la lecture de la plage gelée (« author provenance unknown ») ; défaut suivi en **#2989** (P1). Repassera dans un paquet après correctif/regel. |
| #2962 | `71ada71d1396` | **postposé avec #2989** (pilote, courrier #123224 09:32) : sa résolution avec #2989 vit dans la tête de #2989 (`820137b5e`) ; composé sans #2989 il romprait la paire. Inclus avec #2989 dès que ses deux avis sont posés sur `d090b398`. |
| #2989 | `d090b3984933` | **postposé** : avis dev1 (claude) et cx2 (codex) attendus sur le gel #15484 `d090b398` à l'heure de composition. Inclus avec #2962 dans ce véhicule dès que les deux avis sont posés, sans re-résoudre. |

## En attente au calcul (non admis)

| lot | motif |
|---|---|
| #114 | `verdicts-missing` (un avis deepseek) ; conflit sur `src/ameesh/exploitation.py` |
| #480 | `verdicts-missing` (aucun avis) |
| #2614 | `verdicts-missing` (un avis codex) |
| #2771 | `verdicts-missing` (un avis deepseek) |
| #2938 | `verdicts-missing` (un avis codex) |
| #2940 | `verdicts-missing` (aucun avis) |
| #2954 | `verdicts-missing` (aucun avis) |
| #2955 | `blocked-verdict` |
| #2957 | `verdicts-missing` (un avis codex) |
| #2965 | `verdicts-missing` (aucun avis) |
| #2971 | `verdicts-missing` (aucun avis) |
| #2994 | `verdicts-missing` (aucun avis) |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py --repo . --app ameesh --target
  main` (21/10 09:41) : 8 lots prêts (#2895, #2916, #2962, #2968, #2973,
  #2977, #2988, #288), 13 en attente, 1 erreur (#2914, gel fantôme, écarté
  ci-dessus). Aucun lot prêt omis : 7 composés, #2962 écarté avec motif (paire
  #2989) ; #2989 n'était pas prêt (verdicts-missing/blocked-verdict).
- Contrôle d'inclusion : `scripts/paquet-inclusion.py` sur la tête composée.
- Tests ciblés sur l'arbre composé (local, sans CI).
- CI complète : à demander une fois sur la tête finale gelée (une seule
  suite).
- Prévalidation indisponible (carte 89 h) : écart noté, non bloquant.
