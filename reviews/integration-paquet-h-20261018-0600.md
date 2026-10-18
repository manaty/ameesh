# Paquet integration/paquet-h-20261018-0600 — composition

Base : `main` **`30a1fb9727`** (fusion de la PR #1746, paquet h-0500, 04:01Z). Aucun paquet ouvert ; `main` n'a pas bougé pendant la composition.

Demandes : **pilote mesh-design**, mail du 18/10 04:11Z : feu pour composer sur `30a1fb97`, admissions par priorité, coupe à la taille/conflit avec motif pour h-0700 ; feu sur #2263. **auditeur** : SLO N3 #2265 et #1747, inclusion ou motif nommé. **claude2** : #2355 prêt après #2340 (h-0500 fusionné). **ameesh-dev3** : #2345 prêt, merge-tree rc 0 sur `30a1fb97`.

## Admissions (15 lots, 11 unités)

- #2364, #2265, #2263, #1747, #2355, #2345, #2333, #2337, #2360, #2354, #2358, #2339, #2346, #2204.
- #2249 : déclaré **couvert** par #2265 (commit `2c338eef1`, patch-identique à `3d67a697`) ; sans clôture ni livraison avant la règle canon home#101 (réserve impl-02/cx1 du 17/10, rappelée par le pilote).

## Composition (11 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2364 | `13b62ddfcdb2` (`lot/2364-ci-reservation-run-illisible`) | `889ec2155eef` | avis dev1 sur la tête ; la réservation fraîche prime sur la reprise d'un run illisible |
| 2 | #2265 + #2263 | `efbb01f441d9` (`lot/2265-critical-policy-drift`, PR #1669) | `07dcf4acabe1` | avis dev7 (Claude) sur la tête et impl-04 (DeepSeek) sur #2263 ; `critical_scope` ne saute plus les chemins légers, dérive canon couverte ; #2249 porté (`2c338eef1`), pas de trailer ni de clôture |
| 3 | #1747 | `2f22e038f11f` (`lot/1747-lock-sync-borne`, PR #1498) | `016e5d5d95cb` | avis dev1 sur la tête ; `lock_sync` borné sous psycopg, docstring psql corrigée ; `merge-tree` rc 0 |
| 4 | #2355 | `156bbf2181d0` (`lot/2355-usage-auditeur`) | `83ce08ca8e53` | deux avis (dev2, cx3) ; classe droits SQL ; read-back `has_schema_privilege(r,s,'USAGE')` dans `role-auditeur.sql`, test négatif ; dépend de #2340, fusionnée dans h-0500 |
| 5 | #2345 | `b8b6535a2ea6` (`lot/2345-retry-bound`) | `8c8cb003c658` | porte franchie 2/2 (cx2 + dev4, plus avis mesh-design) ; réessais bornés d'une lecture passagère ; 3 N au backlog #2371 |
| 6 | #2333 | résolution `4005cd3808da` (`511c43346d57` × h-0500 `a57f8c5e`) | `2577c7310386` | avis cx3 + dev1 ; garde mécanique du paquet ; conflit `land.py` résolu |
| 7 | #2337 | `28da57cef61b` (`lot/2337-idle-capacity`) | `40a9c00ed79c` | avis cx1 sur la tête ; capacité inactive, plafond inactif |
| 8 | #2360 | `4088e50c474f` (`lot/2360-manifeste-unites-tete`) | `0e4ea6b71013` | avis cx3 ; le manifeste écrit « #N <tête> » par unité |
| 9 | #2354 | `228fd8ac7fc9` (`lot/2354-admission-sans-psi`) | `234bed5a392d` | tête **gelée** `228fd8ac` (la branche a avancé à `f97ab6c`, non prise : hors gel) ; admission sans PSI, plafond de swap déclaré |
| 10 | #2358 + #2339 | `d1517c1eccdb` (`lot/2358-journal-sans-detail`, parent #2339 `f6526f8f`) | `1f226f6e50d0` | #2358 avis cx2 ; #2339 verdict auditeur ; dépendance portée dans la même unité |
| 11 | #2346 + #2204 | résolution `99f164cda93e` (`9a4a86a3` × h-0500 `a57f8c5e`, contient #2204 `b402c695`) | `bb0686b392b9` | #2346 avis cx2 ; #2204 verdict auditeur ; alias humain/agent et `SET LOCAL` ; conflit `tests/test_l96_roadmap.py` résolu |

Chaque commit de composition a le(s) lot(s) en second(s) parent(s), son sujet nomme le(s) lot(s) et il porte un trailer `ameesh-work: <lot>` par lot porté. Les unités 6 et 11 utilisent une résolution préparée (voir ci-dessous) ; les autres sont des fusions directes de la tête gelée.

## Lots déclarés couverts (sans unité propre)

| row | lot | source | commit | note |
|---|---|---|---|---|
| — | #2249 | `2c338eef1` (porté par l'unité 2) | `07dcf4acabe1` | déclaré couvert : patch-identique au diff relu `3d67a697` ; aucun trailer, aucune clôture ni livraison avant home#101 |

## Conflits et résolutions

- #2333 × h-0500 (h-0500 déjà dans la base) sur `src/ameesh/land.py` : résolution `4005cd3808da`, parents `511c43346d57` × `a57f8c5e34aa`. `main 30a1fb97` a le même arbre que `a57f8c5e` (`git diff --stat` vide), donc la résolution s'applique exactement ; `git merge-tree` rc 0.
- #2346 × h-0500 sur `tests/test_l96_roadmap.py` : résolution `99f164cda93e`, parents `9a4a86a36235` × `a57f8c5e34aa`, qui contient #2204 `b402c69563bb` ; `main 30a1fb97` a le même arbre que `a57f8c5e`, `git merge-tree` rc 0.
- Aucun autre conflit entre les 11 unités : les 11 fusions ont été simulées puis composées rc 0.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2298 | SLO N3, mais sa branche est empilée sur #1770 `44fbd1e5` (qa, en attente du verdict dev1 sur `4801bba3`) : conflits `docs/EXECUTEUR-MEDIEE.md`, `src/ameesh/mechanics.py`, `src/ameesh/mediated_executor/contract.json`. À h-0700 avec #1770, dépendance non prête. |
| #2212 | conflit `src/ameesh/config.py` avec la base ; aucune résolution contre h-0500 (`30a1fb97`). Résolution à préparer, h-0700. |
| #2318 | exclu par le pilote : verdicts dev2/cx2 manquants, dépend de #2278. |
| #2338 | déjà fusionné. |
| #2340 | déjà fusionné (h-0500, `18c62c07` dans `main`) ; rien à porter. |
| #2249 | couvert par #2265 (`2c338eef1`, patch-identique au diff relu `3d67a697`) : déclaré ici, sans unité, sans clôture ni livraison avant home#101. |

## Preuves

- `git diff --stat 30a1fb97..HEAD` : **52 fichiers, +2778/−248**, dont `src/ameesh/lot_class.py`, `merge_queue.py`, `review.py`, `storage/postgres/packages.py`, `deploy/sql/role-auditeur.sql`, `tests/test_2265_critical_policy_drift.py`.
- 11 unités en premier parent, 14 trailers `ameesh-work` (11 unités ; les unités 2, 10 et 11 portent deux lots).
- `git diff --check 30a1fb97..HEAD` : rc 0, aucun marqueur de conflit.
- Têtes sources : têtes gelées (ou résolutions documentées) ; #2354 utilise bien `228fd8ac` et non la tête de branche `f97ab6c`.
- `scripts/paquet-inclusion.py` : rc 0 (voir sortie de composition).

## Adaptations de test après la CI rouge (04:31Z)

La suite `39096950211` a rougi sur un seul test, dans les deux parts qui le
portent (`psql 1/9`, `psycopg 4/6`) :
`tests.test_2273_merge_pr_required.AdmissionTest.test_a_green_required_check_goes_through_and_the_queue_sees_it`.
Le faux `fake_readiness` d'`AdmissionTest` a une signature stricte ; l'unité
#2333 ajoute `packet_exception` à `merge_queue.readiness` et
`merge_pr._admission` le transmet désormais. Le faux accepte le paramètre
(ignoré : ce test porte sur l'admission d'une tête verte, pas sur l'exception
de paquet) ; aucun code de production n'est modifié et aucun avis n'est requis
(§11 bis, même régime que les adaptations `01435d13a4` et `a57f8c5e3` de
h-0500).

La composition des 11 unités, leurs têtes et leurs verdicts ne changent pas ;
la tête de la branche après ce commit est celle de l'unique suite complète.
