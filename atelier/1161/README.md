# Atelier #1161 — union `main 229ac4be` × `correctif 034f5ca3c`

Atelier séparé : **aucune PR, aucun push `main`/`release`/branche de lot,
aucun run, aucune relance**. `refs/heads/lot/1161-ci-pending` reste sur
`e2fe31c4a` ; le correctif R1 `034f5ca3c` reste local, SHA et diff gelés
(porte : #1110 puis #1211, selon mesh-design 08:37).

## Objets

| objet | SHA |
|---|---|
| tête de branche gelée (`origin/lot/1161-ci-pending`) | `e2fe31c4a23717d39d396783017097f131d3b694` |
| correctif R1 (2B/P1), parent = tête gelée | `034f5ca3c8e3bfce6d9550affe0bdc6f155d69f5` |
| `main` contrôlée (#780) | `229ac4be17233b1eb0fd63d6322ea8181f891e54` |
| **résolution d'union** (2 parents : `034f5ca3c` + `229ac4be`) | `4734c477124f76d20dc1adfbcd98c305453336c5` |
| arbre de l'union | `c36cc43332096a3b79745891510a051dde751035` |

`4734c4771` n'est poussé que sous `refs/atelier/1161/union-034f5ca3c`,
jamais sur une branche. Aucun amendement, aucun rebase : la tête gelée
`e2fe31c4a` n'est pas réécrite.

Blobs des trois fichiers du correctif dans l'union :

- `docs/EXPLOITATION.md` : `5bcd0260f137926fcda91d5b7ed632cc2842b45d`
- `src/ameesh/ci.py` : `2de3dee129f60454b8d064b4e0695394f03de4b5`
- `tests/test_l161c_failures_realtime.py` : `e41a43a1d74861f71fe1f9ff6a5e6af6f282ebef`

## Récupération

```sh
git fetch origin '+refs/atelier/1161/*:refs/atelier/1161/*'
git show refs/atelier/1161/union-034f5ca3c   # l'union exacte
git ls-tree -r refs/atelier/1161/patchs      # les patchs ci-dessous
```

Refs poussées : `refs/atelier/1161/union-034f5ca3c` (→ `4734c4771`) et
`refs/atelier/1161/patchs` (→ ce commit de patchs, sans parent).

## Patchs

1. `1161-r1-fix-034f5ca3c.patch` — `git show` du correctif R1 : commit exact
   à relire (3 fichiers, +253/−82), message compris.
2. `1161-fix-vs-main-229ac4be.patch` — `git diff main union` limité aux trois
   fichiers du correctif : le correctif tel qu'il se lit sur le candidat main.
3. `1161-union-vs-main-229ac4be.patch` — `git diff main union` complet :
   contenu final de la PR sur main (inclut le contenu #1110 empilé, non
   fusionné, donc plus bruyant).
4. `1161-union-vs-e2fe31c4a.patch` — `git diff e2fe union` : tout ce que
   l'union ajoute à la tête gelée (correctif + avance de main).

## Vérifications

- `git merge-tree --write-tree 034f5ca3c 229ac4be` → rc 0, arbre
  `c36cc43332096a3b79745891510a051dde751035` : l'union est l'auto-fusion
  exacte des deux parents, aucun conflit, aucune résolution manuelle.
- L'union a exactement deux parents (`034f5ca3c`, `229ac4be`) et l'arbre
  annoncé ; les trois blobs ci-dessus sont ceux de l'union.
- La branche de lot, la PR et la CI ne sont pas touchées par cette poussée
  (refs d'atelier seules).
