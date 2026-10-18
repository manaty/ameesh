# Paquet integration/paquet-h-20261018-0900 — composition

Base : `main` **`8f413a8d259f`** (fusion de la PR #1783, paquet h-0800, 18/10 08:02Z ; arbre identique au gel `730bce8714f2`). Aucun paquet ouvert ; `main` n'a pas bougé pendant la composition.

Demandes : **orchestrateur** 18/10 06:44Z (#2256), 06:47Z (#2254), 06:57Z (#2374), 07:21Z (#2334) ; **mesh-design** 07:03Z (#2278) et 07:21Z (#2334, report ok sur `9c0d9086`) ; **auditeur** 07:01Z (#1280, #2274), 07:05Z (#2282), 07:10Z (#2377), 07:13Z (#1272). **#2377**, admis au paquet h-0800 par mesh-design (05:37Z) mais omis du manifeste h-0800 sans motif, est rattrapé ici dans l'unité #1280+#2377.

## Admissions (16 lots, 14 unités)

- #1280, #2377, #2256, #2374, #1359, #2254, #2278, #2282, #2332, #2334, #2274, #1272, #1738, #2378, #2400, #2311.
- #1738 : admis par mesh-design le 15/10 « au prochain paquet compatible » après #1677 ; résolution `164476b54` avec #2379, avis cx1 OK (dev5 18/10 07:3xZ) ; conflit de compteurs avec la chaîne h-0900 résolu ici (unité 11) ; la résolution paire `164476b54` portait #2379 (non admis), remplacée par `0bd629559` (delta 11 fichiers +311/−21, sans #2379).
- #2378 : admis par mesh-design 18/10 (tête `c1aad55d` = `1932c957` + merge main `856e23f8` ; verdict cx2 OK 0B/0N, sensible `exploitation.py`, Codex hors auteur Claude) ; merge-tree rc 0 sur la chaîne.
- #2400 : signalé SLO niveau 1 par l'orchestrateur 18/10 07:48Z ; tête `cb45dfcc` (verdict cx2 OK, B1/B2 corrigés), sensible SQL/concurrence ; merge-tree rc 0.
- #2311 : relecture après fusion #2302, tête `43c56861` (verdict impl-02, famille opposée à l'auteur cx2), notée « prochain paquet » depuis 03:01 par le composeur puis signalée par l'auditeur 07:53Z ; merge-tree rc 0.

## Composition (10 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #1280, #2377 | `c1452cf16` (résolution) | `fae05054e` | relecture après fusion : garde de famille à la prise (#2377) et clôture `done` après verdict (#1280) |
| 2 | #1359 | `304f4dc7f` (résolution h-0700) | `da411afac` | limite d'en-cours : au plus 2 lots `build` par agent |
| 3 | #2256, #2374 | `2fef65266` (résolution) | `ad8b20725` | feu : voie file/direct selon config hôte (#2256) + relecture après fusion : porte de fusion à deux avis (#2374) |
| 4 | #2254 | `f927d4900` (tête gelée) | `0f3ac542f` | radar : rejeu des résolutions stable (prepared/error) |
| 5 | #2278 | `7268c648f` (résolution main) | `66c25893f` | file de fusion : refus d'une cible supprimée dès admission |
| 6 | #2282 | `84a519ef6` (tête gelée) | `8ceaa336b` | porte de fusion : cgroup dans CRITICAL_SCOPE (2 avis) |
| 7 | #2332 | `b98006b10` (tête gelée) | `029a846e1` | docs/veille : mise à jour du veilleur |
| 8 | #2334 | `9c0d90869` (tête gelée) | `b6fae9714` | garde mécanique : refus fusion directe et ci-lot hors paquet |
| 9 | #2274 | `82c42014a` (tête gelée) | `109f853cf` | relecture après fusion : rattrapage des écrivains |
| 10 | #1272 | `d377d8d1b` (tête gelée) | `cfb224c88` | jauges Claude : rejeu non daté après renouvellement des credentials |
| 11 | #1738 | `0bd629559` (résolution h-0900, sans #2379) | `66d0ffb4e` | relecture : propositions et relecteur désigné, compteurs du contrat |
| 12 | #2378 | `c1aad55d4` (tête gelée) | `689da19a6` | alerte SLO d'un lot waiting_human : viser le décideur, jamais réassigner le porteur |
| 13 | #2400 | `cb45dfcc9` (tête gelée) | `eee5f9086` | train : renumérotation des migrations à la composition, réservation avant publication |
| 14 | #2311 | `43c568619` (tête gelée) | `cabf025a0` | relecture après fusion : ignorer les clones illisibles à la découverte des branches |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot et il porte un trailer `ameesh-work: <lot>` (16 trailers de composition).

## Conflits et résolutions

- **#1280+#2377 × #2256+#2374** sur `src/ameesh/review_lot.py` : union des gardes #2377 (`required_family`, `may_take`) et #2374 (`review_families`, `_critical_changed`, `MARK_PREFIX`, `body_of`), docstring fusionnée. Résolution portée par l'unité 3 (`ad8b20725`).
- **#1738 × chaîne h-0900** sur `docs/EXECUTEUR-MEDIEE.md` : compteurs du contrat après union, 65 opérations / 389 refusées / 454 à l'interface (le doc de la résolution #2379 disait 382/447). Résolution portée par l'unité 11 (`66d0ffb4e`, source `0bd629559`) ; `test_l107_executor_contract` 22 OK ×2 et 157 tests impactés verts (dev5). Les autres unités fusionnent proprement.

## Reports d'union (aucun retrait)

- #1359 : les deux lignes de `docs/EXECUTEUR-MEDIEE.md` ajoutées par sa remise (compteurs 387 refusées / 452 opérations) sont réécrites par l'union de l'unité 11 (389 / 454) ; lot bien composé, non retiré.

## Preuves

- `git diff --stat 8f413a8d259f..HEAD` : **76 fichiers, +6442/−378**, dont `src/ameesh/review_lot.py`, `src/ameesh/fire.py`, `src/ameesh/merge_queue.py`, `src/ameesh/radar.py`, `src/ameesh/wip.py`, `src/ameesh/accounts.py`, `scripts/paquet-compose.py`, `deploy/` le cas échéant.
- `git diff --check 8f413a8d259f..HEAD` : rc 0, aucun marqueur de conflit ; arbre de la composition **identique** à celui composé sur `730bce8714f2` (contrôle `git diff` vide).
- Tests ciblés locaux sur l'arbre composé (les deux pilotes psql et psycopg) : **350 tests OK par pilote** pour les 10 premières unités (#1359, #2254, #1374/#2256, #2282, #2334, #l160e, #1003, #l160g, #1272) ; `review_lot` (#1280/#2377/#2374) : 100 tests OK ; **#1738** (résolution propre `0bd629559`) : `test_l107_executor_contract` 22 OK ×2, 157 tests impactés verts (dev5) ; **#2378** : `test_789_slo_alerts` + `test_l368_planned_waits` 67 OK ×2 ; **#2400+#2311** : `test_2400_train_migrations`, `test_l122_numeros_de_migration`, `test_migrations`, `test_migrations_additives`, `test_l160g_migration_reservations`, `test_l118_assignments`, `test_1467_train` 169 OK ×2.
- `scripts/paquet-inclusion.py --manifest reviews/integration-paquet-h-20261018-0900.md --head HEAD --expected 1280,2377,2256,2374,1359,2254,2278,2282,2332,2334,2274,1272,1738,2378,2400,2311` : rc 0, 16/16 présents.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2212 | déjà `merged` |
| #1770 | verdict dev1 en cours sur `4801bba3` ; hors paquet |
| #423 | remise `12823573` obsolète ; remise fraîche + avis delta → h-1000 |
| #2397, #2396 | relecteur à désigner |
