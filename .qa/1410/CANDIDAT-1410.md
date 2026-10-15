# #1410 — une tête de paquet ne lance qu'une seule suite complète

Candidat gelé : `b7bbe71902c63b6dcda57408c9d2025a45ddb97d`
(`refs/atelier/1410/candidat-b7bbe7190`)
Base        : `ef4461b54ad2` (main au moment du démarrage, 14/10 23:4xZ)
Auteur      : mty-amh-impl-04 (DeepSeek)
Classe      : porte de fusion / sécurité (canon §2 : `.github/**` limité à la
CI = sensible hors prod, un avis indépendant d'une famille autre que l'auteur ;
la **porte de fusion** relève de la sécurité : deux avis). À trancher par le
pilote, comme pour #1343.

## Le constat

Une poussée sur `integration/**` déclenchait deux suites complètes sur la même
tête : le run `push` de `tests.yml` et le run `pull_request` de la PR ouverte,
tous deux portant le check requis `tests`. Annuler l'un laissait l'agrégat
rouge de l'autre (incident #1342 : run push `38653682693` annulé, agrégat
`tests` FAILURE sur la tête à 23:28:22Z, PR #1050 bloquée).

## Le delta retenu

- `tests.yml` ne se déclenche plus sur `push integration/**` (push = `main`,
  `release/**`) et devient appelable (`workflow_call`).
- Nouveau portier `.github/workflows/paquet.yml`, déclenché par
  `push integration/**` : il lit les PR **ouvertes** dont la tête est
  exactement `github.sha` (base `main`, `release/**` ou `integration/**`) via
  `scripts/test-plan.py couverture` (API GitHub en lecture seule) ;
  - une PR ouverte couvre la tête : le portier n'appelle rien, la suite
    complète reste portée par le run `pull_request` de cette PR ;
  - aucune PR, tête non couverte, base hors périmètre, API illisible ou
    contexte incomplet : la suite complète tourne dans le portier
    (`workflow_call` de `tests.yml`) — le repli est toujours « la suite
    tourne ».
- Le portier ne lit **jamais** un run et ne touche à aucun run existant ;
  son check n'est jamais nommé `tests` (`couverture (PR ouverte sur cette
  tête)`), donc aucune validation ne peut être déduite de l'existence d'un
  autre run. Le job `suite` est en `if: always() && needs.couverture.outputs.
  covered != 'true'` : un portier en panne (sortie vide) fait tourner la suite.
- `docs/EXPLOITATION.md` : la règle est documentée dans la section CI.

## Fichiers (7, +644/-19)

```
 .github/workflows/paquet.yml         |  80 +++++++++++
 .github/workflows/tests.yml          |  40 ++++--
 docs/EXPLOITATION.md                 |  14 ++
 scripts/test-plan.py                 | 166 +++++++++++++++++++++
 tests/test_1410_paquet_ci_unique.py  | 271 +++++++++++++++++++++++++++++++++++
 tests/test_l161f_plan.py             |  13 +-
 tests/test_l755_workflow_timeouts.py |  79 +++++++++-
```

Témoin `tests/test_l161f_plan.py` adapté (il affirmait l'ancien déclencheur
`push integration/**`) ; `tests/test_l755_workflow_timeouts.py` apprend les
jobs `uses:` (GitHub refuse `timeout-minutes` sur un job qui appelle un
workflow réutilisable : la borne est celle des jobs du workflow appelé, et
l'appel doit rester local).

## Preuves auteur sur cette tête exacte

- `preuves/tests.txt` : les 8 modules ciblés, **221/221 OK** ;
  `tests.test_1410_paquet_ci_unique` 20/20, `test_l161f_plan` 117/117,
  `test_l755_workflow_timeouts`, `test_l744_ci_entrypoint`,
  `test_l161a_ci_sans_code`, `test_hermeticity_lint`, `test_plan`,
  `test_899_alerte_plafond`.
- `preuves/lint.txt` : lint d'herméticité 0 constat (H0..H3) sur les 3
  fichiers de test modifiés/créés.
- `preuves/py_compile.txt` : `py_compile` OK.
- `preuves/diffstat.txt`.

Aucun run de CI n'a été déclenché : ce commit est sur une branche de lot
(seuls `main` et `release/**` déclenchent `tests.yml`), et aucune PR n'est
ouverte. Aucune fusion.

## Intersections à connaître

- **#1343** (en relecture, non fusionné) modifie les mêmes déclencheurs de
  `tests.yml` (parcours PR `labeled` au gel). Quand il atterrira, mon delta
  devra être rebasé et la composition des deux parcours tranchée.
- **Paquet 1 #1342** (PR #1050, auto-merge armé) contient **#1279**, qui
  modifie aussi `tests/test_l755_workflow_timeouts.py`. Si main bouge,
  rebasage et delta d'avis sur la nouvelle tête.
- Tant que main est réservée au paquet, aucun push de branche, aucune PR,
  aucun run, aucune fusion de ma part.
