# Paquet integration/paquet-h-20261019-1900 — composition (§12)

Base : `main` **`ab845adb0baa`** (paquet h-1700 fusionné, PR #2088, merge du
19/10 18:54 UTC). Branche `integration/paquet-h-20261019-1900`, cible `main`.
Une seule suite complète est demandée sur la tête finale.

Demandes : composeur, paquet horaire §11 bis. Liste calculée
`scripts/paquet-candidats.py` à 19:05 UTC sur `ab845adb0` (lecture fraîche
d'`origin/main`) : **6 prêts, 15 en attente, 0 erreur**. Les 6 lots prêts sont
composés ; #2618 est devenu prêt après le premier calcul de 19:00 (verdict cx1
reçu) et a été ajouté à l'arbre avant la CI.

## Admissions (6 unités, 6 lots portés)

| lot | source | motif |
|---|---|---|
| #2618 | `bbf1cd26368e` (tête gelée) | prêt mécanique, porte OK (dev1 Claude + cx1 Codex) ; sensible (notify, exploitation), texte seul |
| #2640 | `46db1e365f17` (tête gelée) | prêt mécanique, porte OK (cx3 Codex) ; sensible (alert_briefs, notify, radar) |
| #2670 | `000b927f6a41` (tête gelée) | prêt mécanique, porte OK (cx2 Codex + dev1 Claude) ; sensible (review_designation) |
| #2674 | `651fa99143a3` (tête gelée) | prêt mécanique, porte OK (cx1 Codex) ; sensible (work, mesh_cli, alert_briefs, docs) |
| #2680 | `f7554c4cca02` (tête gelée) | prêt mécanique, porte OK (cx1 Codex + dev7 Claude) ; sensible (resolutions) |
| #2692 | `a90f08c6d1ab` (tête gelée) | prêt mécanique, porte OK (dev1 Claude + cx3 Codex) ; sensible (radar, release, release_cutoff) |

## Composition (6 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2692 | `a90f08c6d1ab` | `f23ff978ffc9` | sensible (radar, release, release_cutoff) ; 0 conflit |
| 2 | #2674 | `651fa99143a3` | `a29d3f4327ba` | sensible (work, mesh_cli, alert_briefs, docs) ; 0 conflit |
| 3 | #2680 | `f7554c4cca02` | `d4c7cd8bbcd4` | sensible (resolutions) ; 0 conflit |
| 4 | #2640 | `46db1e365f17` | `3409d51fa278` | sensible (alert_briefs, notify, radar) ; 0 conflit |
| 5 | #2670 | `000b927f6a41` | `32d9d7250413` | sensible (review_designation) ; 0 conflit |
| 6 | #2618 | `bbf1cd26368e` | `cb44ccc9a9a8` | sensible (notify, exploitation), texte seul ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>` (relu par
`scripts/paquet-compose.py --units --base ab845adb0` : 6 unités, 6 lots portés).
Aucun conflit de composition, aucune résolution, aucune migration SQL.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2739 | **non prêt** : conflit avec main sur `src/ameesh/review_authors.py`, `src/ameesh/review_designation.py`, `src/ameesh/review_designation_activation.py` (#2721 fusionné) ; rebase + regel sur `ab845adb0` et avis delta décidés par mesh-design, en cours chez dev3 |
| #2757 | **non prêt** : conflit avec main (`docs/EXPLOITATION.md`, `src/ameesh/sous_utilisation.py`, `tests/test_1227_suite_1176.py`, `tests/test_l94_underuse.py`) ; résolution `97155ecc6f5d` (`refs/ameesh/resolutions/2757/2756`) préparée par cx2 — avis sur le delta final à statuer, hors véhicule |
| #2443 | **non prêt** : 1 avis manquant (seul dev4) et conflit (`site/docs/reference/cli.md`, `src/ameesh/plan_git.py`) ; résolution `30225ea0431c` préparée par impl-07, reportée |
| #2538 | **non prêt** : 1 avis indépendant manquant sur `e7834ed960b7` (seul cx3 Codex) |
| #2729 | **non prêt** : 1 avis indépendant manquant sur `e21c15cca227` (seul cx1 Codex) |
| #2738 | **non prêt** : 1 avis indépendant manquant sur `018d6d2d462e` (seul dev7 Claude) |
| #2696 | **non prêt** : 1 avis indépendant manquant sur `4e27cf3bbc1f` (seul cx3 Codex) |
| #2702 | **non prêt** : 1 avis indépendant manquant sur `306134a9f100` |
| #2752 | **non prêt** : 1 avis indépendant manquant sur `63e03bae6f42` |
| #2766 | **non prêt** : 1 avis indépendant manquant sur `9dfb83bc1266` |
| #2562 | **non prêt** : 1 avis indépendant manquant sur `2ec7a16ea8a0` |
| #2743 | **non prêt** : gel effectif refusé (`blocked-verdict`) |
| #2763 | **non prêt** : gel effectif refusé (`no-verdict`) |
| #1022, #114 | **non prêt** : gel effectif refusé (`blocked-verdict`) |

Les 15 lots en attente sont exclus par la liste calculée ; aucun n'est admis
sans porte verte. Aucune remise officielle ni exception d'admission dans ce
véhicule.

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 19:05 UTC, base
  `ab845adb0baa` : **6 prêts, 15 en attente, 0 erreur** ; les 6 prêts sont
  composés.
- Composition : **6 unités, 0 conflit** ; 28 fichiers, +1262/−81 ;
  `git diff --check` vert, aucun marqueur de conflit, aucune migration SQL.
- `paquet-compose.py --units --base ab845adb0` : **6 unités de fusion,
  6 lots portés** (#2692 `a90f08c6d1ab`, #2674 `651fa99143a3`, #2680
  `f7554c4cca02`, #2640 `46db1e365f17`, #2670 `000b927f6a41`, #2618
  `bbf1cd26368e`).
- `paquet-inclusion` (tête finale, manifeste ajouté au-dessus, `--ready-file`
  recalculée) : **rc 0** — 6 admissions, 6 compositions, 0 omission,
  0 défaut.
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée.
- `scripts/doc-links.py` : 0 lien cassé.
- Tests ciblés sur l'arbre composé : modules des 6 unités (19 modules,
  psql + psycopg) — à consigner au fil du run.
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
