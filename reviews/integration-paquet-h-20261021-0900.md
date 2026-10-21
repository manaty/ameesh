# Paquet integration/paquet-h-20261021-0900 — composition (§12)

Base : `main` **`a1c7b9f64da0`** (fusion du paquet h-0800, PR manaty/ameesh#2279,
tête gelée `9f7d9a007f38`, 21/10 07:37 UTC). Branche
`integration/paquet-h-20261021-0900`, cible `main`, lot porteur #2976. Une
seule suite complète sur la tête finale.

Composeur : mty-amh-impl-01, pour le pilote mesh-design (courrier #122629 du
21/10 07:06, lot #2976 créé à 07:19) ; aucun autre paquet composé en
parallèle. Composition faite directement sur `main` `a1c7b9f64` après la
fusion de h-0800.

## Admissions (4 unités, 4 lots portés)

| lot | source | classe | P | avis (familles) | auteur | objet |
|---|---|---|---|---|---|---|
| #2892 | `81e203002aad` (gel #14831) | sensitive | 1 | mty-amh-qa-01 (deepseek, #14832, 20/10 21:26) + ameesh-dev1 (claude, #15222, 20/10 23:23, re-OK 21/10 04:48) | mesh-design (codex) | TPK #1997 : psycopg>=3.2 requis (`Connection.notifies(timeout=)`) |
| #2942 | `0fdbb3b84166` (gel #15226) | sensitive | 2 | ameesh-cx2 (codex, 05:00) + mty-amh-impl-02 (deepseek, 05:05) | ameesh-dev1 (claude) | Panier provenance-resolutions : `work resolve --done` écrit le reçu exact de provenance |
| #2963 | `b36fd4da44c0` (gel #15262) | sensitive | 2 | ameesh-cx2 (codex, 06:04) + ameesh-dev2 (claude, 06:08) | mty-amh-impl-06 (deepseek) | Train ombre : composition tentée dans le plan, lot en conflit différé (#2916) |
| #2903 | `75fb3826a775` (gel #15298) | sensitive | 3 | ameesh-dev2 (claude, 06:40) + ameesh-cx2 (codex, 06:40) | mty-amh-impl-08 (deepseek) | Panier #2899 : santé courrier, canal humain nominatif et sans double compte |

**#2892, prêt sur la nouvelle base.** Le go de l'auditeur (note 07:20 du lot,
mail #122332) et la note du pilote du 05:18 portaient l'admission au premier
paquet composé après #2961 sur `main`, à la tête exacte `81e20300`. #2961
étant dans h-0800, l'auditeur a vérifié le 21/10 07:50 avec le code de `main`
`a1c7b9f6` : `review_gate_for_item` sur #2892 rend `ok=true`,
`author_family=codex`, avis acceptés qa-01 (deepseek) et dev1 (claude). Le
refus observé au dry-run de 07:49 venait d'un code antérieur à #2961 (venv de
l'hôte 1.16.0, #2972) ; rejoué sur l'arbre composé avec `PYTHONPATH=src`, il
passe (voir Contrôles).

**#2914 retiré du véhicule (motif).** Le pilote l'avait admis à la main
(courrier #122629) sur la tête `2df95bee`, mais son gel antérieur #14991 porte
`66a06cf09d5c`, un commit qu'aucun dépôt ne contient (jamais poussé ; journal
du 21/10 02:59). Depuis #2924, la porte de fusion relit les gels antérieurs :
elle refuse « the frozen range cannot be read (author provenance unknown for
frozen commit(s): 66a06cf09d5c) », reproduit sur `2df95bee` et sur la tête
composée. L'auditeur a demandé de le retirer avant toute CI (messages #122808
07:44 et #122839 07:50) ; le défaut de cause racine est le lot **#2989**
(P1, mty-amh-impl-05). #2914 repassera dans un paquet après correctif ou regel.

## Composition (4 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2892 | `81e203002aad` | `24dae1586` | fusion de la tête gelée, 0 conflit |
| 2 | #2942 | `0fdbb3b84166` | `190a3d608` | fusion de la tête gelée, 0 conflit |
| 3 | #2963 | `b36fd4da44c0` | `a2e1cbf4d` | fusion de la tête gelée, 0 conflit |
| 4 | #2903 | `75fb3826a775` | `2863b5d38` | fusion de la tête gelée, 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Résolutions de composition

Aucune : les quatre fusions sont propres. Pour chaque unité, l'arbre du commit
de composition est exactement celui de `git merge-tree <parent 1> <source>`
(vérifié : `eeb21405`, `6ccecd0b`, `c23342b4`, `b493de40`).

## Écartés (ce véhicule)

| lot | source | motif |
|---|---|---|
| #2914 | `2df95bee4d8b` | **retiré** (auditeur 07:44/07:50) : gel antérieur #14991 fantôme `66a06cf09d5c`, la porte de fusion refuse la lecture de la plage gelée (« author provenance unknown ») ; défaut suivi en **#2989** (P1). Repassera dans un paquet après correctif/regel. |
| #2916 | `c4d845e669f4` | prêt au calcul après la remise de son auteur, mais le pilote a fixé le 21/10 07:25 : remise sur `main`, puis paquet **h-1000** (courrier #122734) ; hors de l'ordre du lot #2976. |
| #2962 | `71ada71d1396` | prêt au calcul du 08:14 (verdicts rendus après l'ordre du lot #2976, créé 07:19) ; prochain paquet. |
| #2968 | `31fe44b65b8b` | prêt au calcul du 08:14 (tête gelée #15335 ; avis ameesh-dev2 et ameesh-cx2 du 07:19, ameesh-dev1 07:26) ; hors de l'ordre du lot #2976 — prochain paquet. |
| #2973 | `898b36ccd0ce` | prêt au calcul du 08:14 (gel #15334 ; avis ameesh-cx3 du 07:19) ; hors de l'ordre du lot #2976 — prochain paquet. |

## En attente au calcul (non admis)

| lot | motif |
|---|---|
| #114 | `verdicts-missing` (un avis deepseek) ; conflit sur `src/ameesh/exploitation.py` |
| #288 | `blocked-verdict` |
| #480 | `verdicts-missing` (aucun avis) |
| #2614 | `verdicts-missing` (un avis codex) |
| #2771 | `verdicts-missing` (un avis deepseek) |
| #2895 | `blocked-verdict` ; conflit sur `docs/design/etudes/index.md` |
| #2938 | `verdicts-missing` (un avis codex) |
| #2940 | `verdicts-missing` (aucun avis) |
| #2955 | `blocked-verdict` |
| #2957 | `verdicts-missing` (un avis codex) |
| #2965 | `verdicts-missing` (aucun avis) |
| #2977 | `blocked-verdict` |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py --repo . --target main --base
  a1c7b9f64` (21/10 08:14) : 8 lots prêts (#2892, #2916, #2942, #2962,
  #2963, #2968, #2973, #2903), 12 en attente, 1 erreur (#2914, gel fantôme,
  écarté ci-dessus). Aucun lot prêt omis : 4 composés, 4 écartés avec motif
  (#2916, #2962, #2968, #2973).
- Porte du véhicule `packet_reviews.need` sur la tête composée (21/10 08:16,
  `PYTHONPATH=src` de l'arbre composé, base `a1c7b9f64`) : `ok=true`,
  `required=0`, 4 constituents relus sur leur tête gelée, aucun delta propre
  au véhicule. Le manifeste est un chemin sans relecture (§11 bis).
- Contrôle d'inclusion `scripts/paquet-inclusion.py --manifest <ce fichier>
  --head <tête> --expected 2892,2942,2963,2903 --ready 2892,2916,2942,2962,2963,2968,2973,2903` :
  vert avant le gel (4 compositions, 4 contenus présents, 0 défaut ; 4
  exclusions consignées).
- Tests ciblés sur l'arbre composé (local, sans CI, base privée
  `ameesh_test_impl01`, `PYTHONPATH=src`) : `test_2892` **1 OK** ;
  `test_l160d` + `test_2446_train_shadow` + `test_1155_staging` **167 OK**
  (145 s). Le run de 189 OK d'avant recomposition portait aussi #2914, retiré
  depuis.
- CI complète : à demander une fois sur la tête finale gelée (une seule
  suite).
- Prévalidation indisponible (carte 87 h) : écart noté, non bloquant.
