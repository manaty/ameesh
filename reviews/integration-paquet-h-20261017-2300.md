# Paquet integration/paquet-h-20261017-2300 — composition

Base : `main` **`734a0825b`** (fusion de la PR #1690, paquet h-2000 ; parents
`f8c8a4a08` × `f48593d8f`). La branche est composée directement sur cette base ; le diff
de la PR ne porte que les 9 unités ci-dessous et ce manifeste.

Tête de composition (9 unités) : **`854ea1e2d40b`** ; la tête de branche au gel, qui
porte ce manifeste, est le SHA exact communiqué au pilote et couvert par l'unique suite
complète du paquet.

Demandes : **pilote mesh-design** — mail du 17/10 23:23Z : h-2000 fusionné sur
`734a0825b`, composer le paquet suivant sur cette base comme exécuteur unique ; unités
prêtes #2261 (`3c78163f2697`, deux familles Codex cx1 + Claude mesh-design) et #1448
(`e833dbe6a23c`, OK cx2 reporté par range-diff `=` + avis Claude de l'auditeur) ;
ajouter les autres lots dont les verdicts sont complets et vérifiés sur tête gelée ;
ne pas inclure #2265/#2263 avant son feu sur la tête retenue. #2253 est admis au
prochain paquet par le pilote depuis 22:34Z.

## Admissions (9 lots)

- #2261
- #1448
- #2253
- #2122
- #2252
- #2279
- #2240
- #1702
- #2182

## Composition (9 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2261 | `3c78163f269707e6773d1c8605ddce02f260ab09` (`lot/2261-sauvegarde-serveur`) | `403545587e6d` | avis Codex `ameesh-cx1` + Claude `mesh-design` sur la tête ; classe prod (`deploy/mise-a-jour/**`) ; composition propre |
| 2 | #1448 | `e833dbe6a23c39c986faeadfbc3efeb28e8a76a0` (`lot/1448-catch-up-borne`, PR #1646) | `a15751f339f0` | avis Codex `ameesh-cx2` transposé par range-diff `=` (008e4b30d=234c79f3b, 47efe3e41=e833dbe6a) + Claude auditeur sur `e833dbe6a` ; classe SQL/droits (`deploy/sql/role-executor.sql`, migration `0461_review_catch_up_acl.sql`) ; composition propre |
| 3 | #2253 | `6e263131e63fee056f9572043dcef2c8355341b4` (`lot/2253-same-repository`, PR #1632) | `592ba3bbbc50` | avis DeepSeek `mty-amh-impl-03` sur la tête ; classe light (`src/ameesh/housekeeping.py`) ; admis au paquet par le pilote 22:34Z ; composition propre |
| 4 | #2122 | `b3d41ae2bf889984b3a32d4f83564facae5c5e1e` (`lot/2122-alerts-follow`, PR #1579) | `4718d39c72e6` | deux avis indépendants Codex `ameesh-cx1` + Claude `ameesh-dev1` sur la tête ; classe prod (`src/ameesh/exploitation.py`) ; composition propre |
| 5 | #2252 | `26a35ea610d4ed95f92a77780c8ef627cd1bfe41` (`lot/2252-l165-dependances-fusionnees`, PR #1633) | `3445a7bbc761` | avis Claude `ameesh-dev1` sur la tête ; classe §8 storage, sans droit SQL, un avis suffit (qualification pilote) ; composition propre |
| 6 | #2279 | `3788c16803955412e6fe3d96b63264d3ae6ac99c` (`lot/2279-radar-contained-migration`, PR #1678) | `f879dcaeb1f9` | avis DeepSeek `mty-amh-impl-07` sur la tête ; classe light (`src/ameesh/radar.py`) ; composition propre |
| 7 | #2240 | `173d7fff125208f60e621efeb8fe3f2c15eac204` (`lot/2240-avis-porteur-ci`, PR #1622) | `1f80061ea13c` | avis Claude `ameesh-dev1` + Codex `ameesh-cx1` sur la tête (delta cx1 exclu, relu par dev1) ; classe porte de fusion = sécurité ; composition propre |
| 8 | #1702 | `5908aadb94911394d227c9732ede9e31ab62287d` (`lot/1702-jauges-n-1132`) | `4f8d002de7bd` | avis Claude `ameesh-dev4` sur la tête (auteur DeepSeek) ; classe argent/jauges, un avis d'une autre famille suffit ; composition propre |
| 9 | #2182 | `b1e5a72d880351ba1c438b0c17e78fafb18d4a47` (`lot/2182-menage-copie-etrangere-chiffre`) | `854ea1e2d40b` | avis Codex `ameesh-cx1` sur la tête ; classe light (`src/ameesh/housekeeping.py`) ; composition propre |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot et il
porte le trailer `ameesh-work: #<lot>`. Aucun contenu nouveau : total vs M2
**35 fichiers, +2033/−321**, dont la migration `0461_review_catch_up_acl.sql` et
`deploy/sql/role-executor.sql` (unité 2, SQL revisé deux familles), aucun `.github/**`.

## Conflits et résolution

Aucun conflit : les 9 merges `--no-ff` passent rc=0 sur `734a0825b`. Seul chevauchement
de fichiers : #2253 × #2182 sur `src/ameesh/housekeeping.py` (hunks distincts), union
automatique propre, sans commit de résolution. Aucune adaptation de contenu relu.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2265 / #2263 | tenus par le pilote : attendre sa convergence cx1/cx3 et son feu sur la tête retenue |
| #2292 | verdict Codex encore dû ; correctif du plafond psycopg (6 parts / 45 min) |
| #2296 | second avis Codex encore dû (palliatif #2297 en place) |
| #2212 | second avis Codex encore dû |
| #2273 | second avis cx2 encore dû |
| #1280 | second avis SQL encore dû |
| #1515 | second avis sécurité Codex indisponible avant le 22/10 |
| #2249 | admission liée à la règle canon §8 portée par le pilote |
| #2282 | branche en conflit avec main (`merge_queue.py`), à rafraîchir par son lot |
| #2260 | route pilote = CI de lot + feu, hors paquet |
| #1768 | portée Nexlink |
| autres lots qa | verdicts incomplets ou hors admission |

## Preuves

- `git diff --check` sur `734a0825b..HEAD` : rc 0, aucun marqueur de conflit.
- Seconds parents et trailers `ameesh-work` : 9/9 vérifiés.
- `scripts/paquet-inclusion.py --manifest <ce fichier> --head <tête> --expected
  2261,1448,2253,2122,2252,2279,2240,1702,2182` : à rejouer sur la tête finale avant le
  feu (résultat consigné au fil du lot).
- Chaque tête source est la tête distante admise (`ls-remote` = sources listées) ;
  aucun rebase de lot, aucune CI de lot.

## CI et fusion

- **Une seule** suite complète sur la tête exacte du paquet : la branche
  `integration/paquet-h-20261017-2300` est poussée et la PR ouverte immédiatement pour
  que le portier `paquet.yml` constate la couverture ; s'il la constate, l'étiquette
  `ci-full-<sha>` déclenche l'unique suite `tests.yml`. Aucune CI de lot, aucun second
  lancement, aucun rebase.
- Le check requis `tests` doit être `success` avec des tests réellement exécutés (pas
  `skipped`/absent, #2271). Si la part psycopg est coupée par le plafond de 30 min
  (défaut #2292 encore ouvert), relancer les jobs en échec du même run, même tête.
- Feu du pilote sur cette tête après `tests` vert (`gh pr merge --auto
  --match-head-commit <sha>`). Pendant la CI, `main` est réservée (§12) : aucune autre
  fusion n'y entre.
- Après fusion : rattachement des unités au merge réel et clôture, preuves aux auteurs
  et au pilote. Aucune opération hôte ici.
