# Paquet integration/paquet-h-20261018-0700 — composition

Base : `main` **`90ae5e1927`** (fusion de la PR #1761, paquet h-0600, 05:09Z ; arbre identique à `a459bff74`). Aucun paquet ouvert ; `main` n'a pas bougé pendant la composition.

Demandes : **pilote mesh-design**, mails du 18/10 04:37Z et 04:52Z : admissions h-0700 après fusion de h-0600, sur la tête de main qui en résulte ; un lot à verdict manquant est exclu avec motif. **deepseek6** (05:06Z, 05:07Z) : prendre la résolution `0e798c03` comme source de #2207 (la fusion brute e9607c19 × 511c4334 laisse le témoin P2-1 rouge). **mty-nxl-rel-01** (05:04Z, lot #2284) : #2284 empilé sur #2207, gel `a982d7fd`, prêt pour ce paquet ; l'adaptation de test #2207 × #2333 doit être portée.

## Admissions (5 lots, 5 unités)

- #2207, #2284, #2335, #1476, #2212.

## Composition (5 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2207 | `e9607c19` (tête gelée) via résolution #2333 `0e798c03013f` | `7d42fe427` | avis dev4 (Claude) sur e9607c19 ; `_class_of` lit la cible fraîche, jamais le `main` local périmé ; la résolution porte l'adaptation de test #2207 × #2333 |
| 2 | #2284 | `a982d7fd` (`lot/2284-repli-commentaire`, empilé sur `e9607c19`) | `a39125a77` | avis dev1 (Claude) sur a982d7fd ; delta = commentaire de repli seul (6+/1−) ; fusion propre sur l'unité 1 (base de fusion = e9607c19) |
| 3 | #2335 | `2ec1b266` (tête gelée) via résolution #2361 `4ce6fc3d6af9` | `43412a744` | avis dev7 (Claude) sur 2ec1b266 ; SLO : paquet en cours visible, cible alignée §11 bis ; conflit `exploitation.py` résolu (voir ci-dessous) |
| 4 | #1476 | `163a3ff8` (`lot/1476-ci-15min`) | `8f1adb25a` | avis dev1 (Claude) sur 163a3ff8 ; CI ameesh sous 15 min ; résolution 41ad7d70 non utilisée (voir ci-dessous) |
| 5 | #2212 | `b045bfa5` via résolution main `86a6153b5db1` | `af452837f` | avis cx1 sur la tête ; feu/radar relisent le profil de l'hôte ; résolution avec main `18a932b2` (ancêtre de la base), merge-tree rc 0 |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot et il porte un trailer `ameesh-work: <lot>` (5 trailers).

## Conflits et résolutions

- #2335 × h-0600 sur `src/ameesh/exploitation.py` : h-0600 a ajouté `agents=agents` à `progress._work_item_milestones` (attribution des jalons) ; #2335 calcule `packet_slot` et le passe à `slo.lot_steps`. Résolution : les deux conservés — `milestones.get(rid), agents=agents` puis le bloc `_packet_slot` / `lot_steps(..., packet_slot=slot)`. Aucune autre modification de l'arbre.
- #2207 × #2333 : conflit textuel `merge_queue.py` seulement, porté par la résolution `0e798c03` (parents `e9607c19` × `511c4334`). La résolution contient l'adaptation de test complète : `packet_exception` sur `test_admission_fetches_the_target_and_reads_the_fresh_class`, `packet_guard.declare` dans `QueueWithoutRadarTest`, mock `check_merge` dans `_Base`, `PacketAdmissionTest`. Vérifié : `tests.test_l160e_merge_queue` 61 OK.
- #2284 : aucune résolution nécessaire sur cette base ; b2af8961 (`a982d7fd` × `511c4334`) n'est **pas** utilisée car elle ne porte pas l'adaptation de test ci-dessus (témoin P2-1 rouge constaté par mty-nxl-rel-01).
- #1476 : la résolution 41ad7d70 (`163a3ff8` × `#2357` `dfb56406`) n'est **pas** utilisée : #2357 est en qa, verdict codex1 manquant, non admis. `163a3ff8` seule se fusionne rc 0 sur la base (aucun conflit) ; aucun contenu de #2357 n'entre dans le paquet.

Contrôle d'inclusion : `scripts/paquet-inclusion.py` rend **rc 0** (aucun défaut). Il signale #2207 `partiel` sur **une ligne** de `merge_queue.py` (`96/97`) : la ligne de commentaire ajoutée par #2207 est réécrite par l'unité 2 (#2284), c'est l'objet même de ce lot — ce n'est ni une omission ni un retrait. Les quatre autres unités sont `présent`.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2298 | admis conditionnellement « si #1770 est prêt » : #1770 est en qa, **verdict dev1 toujours dû sur `4801bba3`** (revue non commencée). Sa branche est empilée sur #1770 ; non prêt. |
| #2357 | non admis par le pilote (verdict codex1 manquant) ; sa résolution avec #1476 n'est pas utilisée. |
| #2374 | en attente des verdicts cx3+dev1 (pilote). |
| #1359 | delta dev2 sur `50cec010` à venir (pilote). |
| #2252 / #2182 | derrière l'arbitrage #2256 du pilote. |

## Preuves

- `git diff --stat 90ae5e192..HEAD` : **29 fichiers, +2255/−239**, dont `src/ameesh/merge_queue.py`, `packet_guard.py`, `exploitation.py`, `slo.py`, `config.py`, `runner.py`, `progress.py`, `scripts/test-plan.py`, `scripts/test-parts.py`, `.github/workflows/tests.yml`.
- 5 unités en premier parent, 5 trailers `ameesh-work` ; `scripts/paquet-compose.py --units --base 90ae5e192` : `5 unités : #2207 0e798c03013f, #2284 a982d7fdaee4, #2335 4ce6fc3d6af9, #1476 163a3ff8d9c5, #2212 86a6153b5db1`.
- `git diff --check 90ae5e192..HEAD` : rc 0, aucun marqueur de conflit.
- Têtes sources : têtes gelées ou résolutions documentées ; #2207 utilise la résolution `0e798c03` (pas la fusion brute) et #2284 son gel `a982d7fd`.
- Tests ciblés locaux sur l'arbre composé : `test_l160e_merge_queue` 61 OK (adaptation #2207 × #2333) ; `test_2212_fire_fresh_radar` + `test_789_slo` + `test_789_slo_alerts` + `test_l26_exploitation` 116 OK ; `test_l161f_plan` + `test_l116_parts` + `test_progress` + `test_l232b_release_scripts` 208 OK (2 skips). Aucune CI de lot (§11 bis) : une suite complète unique sur cette tête.
- `scripts/paquet-inclusion.py` : rapport ci-dessous, avant le feu.
