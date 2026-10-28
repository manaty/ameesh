# Paquet integration/paquet-h-20261028-06 — composition (§12), sur main `03dab6e829e6`

Base : `main` **`03dab6e829e679dc63d8eb4e2c52bdeae6c0f3d7`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261028-06`, cible `main`.
Porteur : **#4189**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (7 unités, 7 lots)

| lot | source composée |
|---|---|
| #4168 | `5bf8c243f1fe55524de0a7abecc828770b61b631` |
| #4167 | `2de9472b964295b28583dc38b90dcca015ab9971` |
| #4171 | `a480a0dd1c8e16dc6f3507520d7afe6d4dfd305b` |
| #4180 | `264f778cb8a05cff4cb748569ed6b06d404a2312` |
| #4179 | `f1bb4929028992caec302a2436838cdb0ca93dc2` |
| #4181 | `e283284d362f8c4a038f2b958108c0520dc1b913` |
| #4201 | `6b58f57ec69d4c5cdea6d469f19c05bbd44c6bae` |

## Composition (7 unités, 7 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4168 | `5bf8c243f1fe` | `c1c59594afb3` | composition |
| 2 | #4167 | `2de9472b9642` | `bcfd4bd12346` | composition |
| 3 | #4171 | `a480a0dd1c8e` | `4171d779036a` | composition |
| 4 | #4180 | `264f778cb8a0` | `497ec4ca0bd0` | composition |
| 5 | #4179 | `f1bb49290289` | `236e0faffa2f` | composition |
| 6 | #4181 | `e283284d362f` | `3c08a71b9f63` | composition |
| 7 | #4201 | `6b58f57ec69d` | `2cd97a3cf856` | composition |

## Écartés

| lot | motif |
|---|---|
| #4164 | retiré par l'outil à la passe 4189d (module propre rouge sans attribution sûre) : seul rouge, le tearDownClass de test_l56_role_executor, QueryCanceled sous charge ; vert deux fois seul ; non concluant au sens du §3, correctif d'outil en cours ; en tête du paquet h-07 |
| #4231 | devenu prêt après la liste calculée de la passe 4189d (10:33Z) ; paquet h-07 |
| #4150 | exclu par le pilote à la composition de 10:33Z : avis bloquant test_l64 sur la tête d'alors ; regelé depuis avec deux avis ; paquet h-07 |
| #4173 | conflit de composition (mesh_cli.py, test_cluster.py) ; paquet h-07 |
| #4217 | devenu prêt après la liste calculée de la passe 4189d (10:33Z) ; paquet h-07 |
| #4219 | devenu prêt après la liste calculée de la passe 4189d (10:33Z) ; paquet h-07 |
| #4222 | devenu prêt après la liste calculée de la passe 4189d (10:33Z) ; paquet h-07 |

Liste calculée : 14 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 16.6 s.
- Tests locaux : 0 séries, 0 modules, 0 tests exécutés, 0 échec(s), 0 run(s) rouge(s) sans effet, 0 non concluant(s), 0 sauté(s) : aucun.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).

## Publication (pilote mesh-design, 28/10 12:3xZ) — écart signalé (§7)

La passe 4189d (`paquet-auto.py --force --exclude 4150`, unité systemd sur ameesh-agents-1, lancée à 10:33Z, recomposée sans le lot retiré à 11:21Z) a composé exactement la chaîne ci-dessus, `03dab6e829e6..2cd97a3cf856`. Sur cette chaîne, freeze-checks était vert et les tests locaux aussi : unités, voisins et contrats (par exemple 67 modules et 981 tests pour une unité, 36 modules et 453 tests pour une autre, 12 modules et 330 tests de voisins et contrats). Elle est morte à 11:56:23Z, avant le manifeste, sur `GestureRefused` (PilotGesture) : le bail mesh-design était passé à ameesh-agents-2 (époque 289). Rien n'avait été poussé.

Le pilote a repris la tête composée depuis le worktree conservé sur agents-1, poussée telle quelle sous un nom temporaire. Il a vérifié sur agents-2 la chaîne commit par commit : chaque commit de composition a pour premier parent le précédent et pour second parent la tête gelée enregistrée de son unité (`work packet show`) ; la base est la tête de `main`. Il a ensuite fini les étapes du script, avec ses propres fonctions : freeze-checks rejoué sur agents-2, liste calculée, table de composition, manifeste, inclusion, enregistrement du paquet, poussée, PR et gel, sous le bail vivant. Les tests locaux n'ont pas été rejoués : ce sont ceux d'agents-1, sur les mêmes commits. Comme toujours, la ci-full de cette tête tranche (§12).
