# Paquet integration/paquet-h-20261018-1000 — composition

Base : `main` **`9c30f9622a6f`** (fusion de la PR #1791, paquet h-0900, 18/10 09:40Z ; arbre identique au gel `92e615f1`). Aucun paquet ouvert ; `main` n'a pas bougé pendant la composition.

Demandes : **mesh-design** (pilote) 18/10 09:45Z (candidats h-1000 : #2370, #2379, #1768, #2385, #2395) ; #2371+#2408 reportés au paquet suivant (verdict cx3 dû). Deux candidats sont refusés par la porte des verdicts (voir « Écartés ») : composés ici **#2379, #1768, #2385**.

## Admissions (3 lots, 3 unités)

- #2379, #1768, #2385.

## Composition (3 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2379 | `193646bf1003` (tête gelée) | `6c83582ef` | file de fusion : un QUEUE_REF périmé ne sert plus de cible fraîche hors repli (`base=None`) ; verdicts cx1 (Codex) + qa-01 (DeepSeek) ok sur la tête ; porte 2/2 |
| 2 | #1768 | `01819e44ce0c` (tête de remise) | `ef4069deb` | porte : `required_statuses` par glob de branche (`integration/**` → `lot/`) ; verdicts cx1 (Codex) + impl-07 (DeepSeek) ok sur la tête ; sensible sécurité |
| 3 | #2385 | `8c65e81f1016` (tête gelée) | `68218c47b` | garde de livraison : clôture d'un lot pinné sur workflow couverte ; verdict cx3 (Codex) ok ; lot léger |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot et il porte un trailer `ameesh-work: <lot>` (3 trailers de composition).

## Conflits et résolutions

- Aucun : `git merge-tree --write-tree` rc 0 pour chaque tête sur la base `9c30f9622a6f` et pour les trois paires de têtes ; les compositions (`scripts/paquet-compose.py`) ont fusionné proprement. Aucune résolution n'est utilisée.

## Preuves

- `git diff --stat 9c30f9622a6f..HEAD` : **11 fichiers hors ce manifeste, +438/−37** (12 fichiers avec le manifeste), dont `src/ameesh/merge_queue.py`, `src/ameesh/fire.py`, `src/ameesh/land.py`, `src/ameesh/merge_pr.py`, `src/ameesh/radar.py`, `src/ameesh/process_registry.py`, `docs/LANDING-CONDITIONS.md` et 4 fichiers de tests.
- 3 unités en premier parent, 3 trailers `ameesh-work` ; têtes sources : `193646bf1003`, `01819e44ce0c`, `8c65e81f1016`.
- `git diff --check 9c30f9622a6f..HEAD` : rc 0, aucun marqueur de conflit.
- Tests ciblés locaux sur l'arbre composé, les deux pilotes : **266 tests OK par pilote** (psql 380 s, psycopg 122 s) — `test_l160e_merge_queue`, `test_1748_required_statuses`, `test_l160g_nexlink`, `test_1104_review_lot_hors_file`, `test_1374_fire`.
- `scripts/paquet-inclusion.py --manifest reviews/integration-paquet-h-20261018-1000.md --head HEAD --expected 2379,1768,2385` : rc 0, 3/3 présents, verdict « aucun défaut ».

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2370 | `fire.py` = chemin critique (porte de fusion, classe `sensitive`) ; 2 avis de familles distinctes requis, seul `codex3` ok sur `7ae5343c` (`fire.ready_verdicts` ok=False, 09:57Z). Second avis à obtenir. |
| #2395 | `exploitation.py`/`notify.py` = classe `sensitive` ; 2 avis requis, seul `ameesh-cx2` ok sur `b861fba4` (`fire.ready_verdicts` ok=False, 09:57Z). Second avis à obtenir. |
| #2371+#2408 | pas encore : verdict cx3 dû (pilote 09:45Z), au paquet suivant. |
