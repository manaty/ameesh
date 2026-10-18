# Paquet integration/paquet-h-20261018-0700 — composition

Base : `main` **`90ae5e1927`** (fusion de la PR #1761, paquet h-0600, 05:09Z ; arbre identique à `a459bff74`). Aucun paquet ouvert ; `main` n'a pas bougé pendant la composition.

Demandes : **pilote mesh-design**, mails du 18/10 04:37Z et 04:52Z : admissions h-0700 après fusion de h-0600, sur la tête de main qui en résulte ; un lot à verdict manquant est exclu avec motif. **deepseek6** (05:06Z, 05:07Z) : prendre la résolution `0e798c03` comme source de #2207 (la fusion brute e9607c19 × 511c4334 laisse le témoin P2-1 rouge). **mty-nxl-rel-01** (05:04Z, lot #2284) : #2284 empilé sur #2207, gel `a982d7fd`, prêt pour ce paquet ; l'adaptation de test #2207 × #2333 doit être portée. **Correction 05:31/05:32Z** : le 2e avis cx1 de #2212 existe (jalons #11201 du 03:46Z et #11329 du 05:14Z, tous deux sur la résolution `86a6153b`), le pilote retire son objection et l'orchestrateur pose la dispense `--no-verdict` ; #2212 reste au paquet, la tête 54d49d983 n'est pas recomposée pour l'en sortir.

## Admissions (5 lots, 5 unités)

- #2207, #2284, #2335, #1476, #2212.

## Composition (5 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2207 | `e9607c19` (tête gelée) via résolution #2333 `0e798c03013f` | `7d42fe427` | avis dev4 (Claude) sur e9607c19 ; `_class_of` lit la cible fraîche, jamais le `main` local périmé ; la résolution porte l'adaptation de test #2207 × #2333 |
| 2 | #2284 | `a982d7fd` (`lot/2284-repli-commentaire`, empilé sur `e9607c19`) | `a39125a77` | avis dev1 (Claude) sur a982d7fd ; delta = commentaire de repli seul (6+/1−) ; fusion propre sur l'unité 1 (base de fusion = e9607c19) |
| 3 | #2335 | `2ec1b266` (tête gelée) via résolution #2361 `4ce6fc3d6af9` | `43412a744` | avis dev7 (Claude) sur 2ec1b266 ; SLO : paquet en cours visible, cible alignée §11 bis ; conflit `exploitation.py` résolu (voir ci-dessous) |
| 4 | #1476 | `163a3ff8` (`lot/1476-ci-15min`) | `8f1adb25a` | avis dev1 (Claude) sur 163a3ff8 ; CI ameesh sous 15 min ; résolution 41ad7d70 non utilisée (voir ci-dessous) ; correctif de paquet `f67c808a3` sur `test-plan.py` (voir ci-dessous) |
| 5 | #2212 | `b045bfa5` via résolution main `86a6153b5db1` | `af452837f` | avis 1/2 impl-02 (DeepSeek) et 2/2 cx1 (Codex) sur `b045bfa5`/`86a6153b` — jalons #11201 et #11329 ; feu/radar relisent le profil de l'hôte ; résolution avec main `18a932b2` (ancêtre de la base), merge-tree rc 0 ; delta `config.py` couvert par l'avis cx1 |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot et il porte un trailer `ameesh-work: <lot>` (5 trailers). Le correctif de paquet `f67c808a3`, non fusion, porte `ameesh-work: 1476` et ne compte pas comme unité.

## Conflits et résolutions

- #2335 × h-0600 sur `src/ameesh/exploitation.py` : h-0600 a ajouté `agents=agents` à `progress._work_item_milestones` (attribution des jalons) ; #2335 calcule `packet_slot` et le passe à `slo.lot_steps`. Résolution : les deux conservés — `milestones.get(rid), agents=agents` puis le bloc `_packet_slot` / `lot_steps(..., packet_slot=slot)`. Aucune autre modification de l'arbre.
- #2207 × #2333 : conflit textuel `merge_queue.py` seulement, porté par la résolution `0e798c03` (parents `e9607c19` × `511c4334`). La résolution contient l'adaptation de test complète : `packet_exception` sur `test_admission_fetches_the_target_and_reads_the_fresh_class`, `packet_guard.declare` dans `QueueWithoutRadarTest`, mock `check_merge` dans `_Base`, `PacketAdmissionTest`. Vérifié : `tests.test_l160e_merge_queue` 61 OK.
- #2284 : aucune résolution nécessaire sur cette base ; b2af8961 (`a982d7fd` × `511c4334`) n'est **pas** utilisée car elle ne porte pas l'adaptation de test ci-dessus (témoin P2-1 rouge constaté par mty-nxl-rel-01).
- #1476 : la résolution 41ad7d70 (`163a3ff8` × `#2357` `dfb56406`) n'est **pas** utilisée : #2357 est en qa, verdict codex1 manquant, non admis. `163a3ff8` seule se fusionne rc 0 sur la base (aucun conflit) ; aucun contenu de #2357 n'entre dans le paquet.

## Correctif de paquet (après la première suite complète)

La suite complète du paquet, run **39100231995** (`psql 13/16`), a échoué sur
`tests.test_l148_stage6_json_keys.CardKeysTest.test_plan_weights_read_the_english_key` :
`plan.weights(["test_l161e_map"], "psycopg", carte)` rendait `10.0` (la table
statique `tests/parts/durees.json`) au lieu de `3.0` (la carte), alors que le
contrat L148 stage 6 exige que la carte porte la durée du module.

Cause : #1476 fusionnait la table statique **après** la carte
(`complete = carte; complete.update(statique)`), donc la table écrasait le
dernier relevé de la carte. Le correctif `f67c808a3` prend la table statique
comme base puis applique la carte : les trous de la carte restent comblés par
la table (objet de #1476) et un module que la carte connaît garde sa mesure.

Delta sur un contenu relu : 1 fichier (`scripts/test-plan.py`), 8+/5−, dont
3 lignes de commentaire. Tests ciblés sur cette tête : `test_l148_stage6_json_keys`
15 OK ; `test_l161f_plan` 160 OK (2 skips) ; `test_l116_parts` +
`test_l107_executor_contract` 22 OK. La suite complète du paquet est relancée
sur `f67c808a3` ; le gel du lot #2386 suit cette tête.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2298 | admis conditionnellement « si #1770 est prêt » : #1770 est en qa, **verdict dev1 toujours dû sur `4801bba3`** (revue non commencée). Sa branche est empilée sur #1770 ; non prêt. |
| #2357 | non admis par le pilote (verdict codex1 manquant) ; sa résolution avec #1476 n'est pas utilisée. |
| #2374 | verdicts dev1+cx3 acquis sur `c732bf07e` (05:19Z) ; le pilote et docs-01 (05:24Z) le destinent au paquet suivant h-0800, pas à cette tête. |
| #1359 | gel `50cec010`, verdict dev2 ; admis au paquet suivant, pas à cette tête (le bureau 05:31Z ne demande pas de recomposition h-0700). |
| #2252 / #2182 | derrière l'arbitrage #2256 du pilote. |

## Preuves

- `git diff --stat 90ae5e192..HEAD` : **29 fichiers hors ce manifeste, +2258/−239** (30 fichiers avec le manifeste), dont `src/ameesh/merge_queue.py`, `packet_guard.py`, `exploitation.py`, `slo.py`, `config.py`, `runner.py`, `progress.py`, `scripts/test-plan.py`, `scripts/test-parts.py`, `.github/workflows/tests.yml`.
- 5 unités en premier parent, 5 trailers `ameesh-work` ; `scripts/paquet-compose.py --units --base 90ae5e192` : `5 unités : #2207 0e798c03013f, #2284 a982d7fdaee4, #2335 4ce6fc3d6af9, #1476 163a3ff8d9c5, #2212 86a6153b5db1`.
- `git diff --check 90ae5e192..HEAD` : rc 0, aucun marqueur de conflit.
- Têtes sources : têtes gelées ou résolutions documentées ; #2207 utilise la résolution `0e798c03` (pas la fusion brute) et #2284 son gel `a982d7fd`.
- Tests ciblés locaux sur l'arbre composé : `test_l160e_merge_queue` 61 OK (adaptation #2207 × #2333) ; `test_2212_fire_fresh_radar` + `test_789_slo` + `test_789_slo_alerts` + `test_l26_exploitation` 116 OK ; `test_l161f_plan` + `test_l116_parts` + `test_progress` + `test_l232b_release_scripts` 208 OK (2 skips) ; `test_l148_stage6_json_keys` + `test_l116_parts` + `test_l107_executor_contract` 37 OK après le correctif. Aucune CI de lot (§11 bis) : une suite complète unique sur cette tête.
- `scripts/paquet-inclusion.py --expected 2207,2284,2335,1476,2212` : **rc 0**, aucun défaut. `#1476` est `partiel` sur 3 lignes de commentaire de `scripts/test-plan.py` réécrites par le correctif (voir ci-dessus) ; `#2207` reste `partiel` sur une ligne de `merge_queue.py` réécrite par #2284, objet du lot. Les trois autres unités sont `présent`.
