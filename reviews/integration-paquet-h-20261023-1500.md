# Paquet integration/paquet-h-20261023-1500 — composition (§12)

Base : `main` **`18204348c558`** (tête au 23/10 13:08Z). Branche
`integration/paquet-h-20261023-1500`, cible `main`. Paquet manuel ameesh
(créneau 15:00 (13:00Z)), sur la délégation mesh-design #127546 et l'audit P1 de
13:07Z : trois P1 prêts étaient en attente. Le train ameesh est hors service
(`train.projects = ['nexlink']`), le repli manuel s'applique.

Composeur : mty-amh-impl-01. Porteur : **#3146** (issue
manaty/ameesh#2408), PR **#2409**. Une seule suite complète sur la tête
finale.

## Unités (3 unités, 3 lots portés)

| lot | source | classe | avis | auteur | objet |
|---|---|---|---|---|---|
| #3122 | `0623832ab181` | sensitive | porte acquise sur la tête gelée (auditeur) | ameesh-dev5 | B/P1 : courte saturée masquée par les journaux anciens |
| #3013 | `1f36d5a4e65b` | sensitive | 2/2 ok (auditeur, codex cx2) | mty-amh-impl-03 | EP4 : gestes du pilote fencés par l'époque du bail |
| #3012 | `7aabea08` (résolution `35dd28d5d019`) | sensitive | 2/2 ok (auditeur, codex cx2) | mty-amh-impl-04 | EP3 : exécuteurs en attente, briefing d'élection |

## Admissions

#3122, #3013, #3012.

## Composition (3 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3122 | `0623832ab181` | `0e63a7f5a` | fusion de la tête gelée, 0 conflit |
| 2 | #3013 | `1f36d5a4e65b` | `e57a9f314` | fusion de la tête gelée, 0 conflit |
| 3 | #3012 | `35dd28d5d019` (résolution #3012/#3013) | `29e38ca89` | conflit `docs/HOST-HANDOFFS.md` résolu par la résolution enregistrée `refs/ameesh/resolutions/3012/3013` ; #3013 déjà composé |

## Reports

Les autres lots prêts du calcul `paquet-candidats.py` du 23/10 13:08Z ne sont
pas dans ce paquet d'urgence : ils passent au créneau suivant. Sont reportés
#3126, #3139, #3029, #3098, #3007, #3132, #3131, #3128, #3133, #3099, #3070,
#3115, #3048, #3054, #3103, #3100, #3096, #3057, #2993. Les lectures en erreur
(#3123, #2994, #2914) ne sont pas composées et restent à qualifier.

#3012 est inclus : son hold de 13:03Z peut être levé par impl-04 ; il n'est pas
exclu des candidats.

## CI

Suite complète sur la tête finale : en cours (`scripts/test.sh` via
`ameesh heavy`). Le manifeste est complété avec le verdict CI avant la fusion.
