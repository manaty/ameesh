# Paquet integration/paquet-h-20261019-1100 — composition (§12)

Base : `main` **`fcf4486e594c`** (paquet h-1000 fusionné, PR #2042, commit du
19/10 09:52:27 UTC). Branche `integration/paquet-h-20261019-1100`, cible
`main`. Une seule suite complète est demandée sur la tête finale.

Demandes : composeur, paquet horaire §11 bis. L'auditeur (message du 19/10
10:41 UTC) demande de composer les trois lots prêts **#2582, #2682 et #2556**
sans #2451, laissé à `ameesh-dev2` (brief en note sur le lot). Liste calculée
`paquet-candidats.py` à 10:48 UTC sur `fcf4486e594c` : **27 candidats, 4 prêts
(#2582, #2682, #2698, #2556), 23 en attente, 0 erreur**. Les trois lots demandés
sont composés ; #2698, prêt mécaniquement, est écarté avec motif (voir
« Écartés »).

## Admissions (3 unités, 3 lots portés)

- #2582 — `ameesh alerts --json` : une seule sonde console bornée à 5 s,
  famille illisible reportée
- #2682 — train en ombre : `incident_facts` sans leçon, porteurs en vol
  exclus de la comparaison
- #2556 — ménage `/tmp` : un seul parcours des copies git par entrée

## Composition (3 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2582 | `a200d5e6b2d0` (tête gelée) | `01a3c3dd15a6` | sensible — 2 avis (cx1 Codex, dev4 Claude) sur la tête gelée |
| 2 | #2682 | `986bbb550c4f` (tête gelée) | `4015f2872636` | sensible — 1 avis (cx3 Codex) sur la tête gelée ; la désignation manuelle dev4 n'a pas de verdict sur cette tête (son B portait sur `f3440851e`), véhicule composé sur consigne auditeur du 19/10 10:41 |
| 3 | #2556 | `04ebdb463f2d` (tête gelée) | `c631842bf731` | léger — 0 avis pré-fusion requis (classe light), relecture après fusion |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>` (relu par
`scripts/paquet-compose.py --units --base fcf4486e594c` : 3 unités, 3 lots
portés). Aucun conflit de composition, aucune résolution ; aucune migration SQL.

Un correctif de test seul a été ajouté au-dessus de la composition
(`2f12933d7`) : dans `tests/test_2582_alerts_console_timeout.py`, la borne de
temps du témoin de console muette porte sur la sonde
(`pgbouncer_local.probe`, 0,05 s) et non sur le relevé complet, qui dépend du
pilote (psql : 6,4–7,1 s sur l'hôte composé). Le témoin garde ses assertions
fortes : la lecture de la console reste bloquée 5 s (`release` non armé) et la
famille illisible reste signalée. Adaptation de test, aucun avis (§11 bis).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2698 | prêt mécaniquement sur `915bdc85c1cf` (1 avis mty-amh-impl-06, seuil calculé 1) mais **hold pilote mesh-design 10:45** : 2e verdict codex (`ameesh-cx2`, op-paused) attendu, `distinct_families` ; conflit avec #2451 sur `train_policy.py` (résolution notée le 19/10 10:19) — exclu de ce véhicule |
| #2451 | non prêt (`verdicts-missing` : cx3 seul sur `1687f24e59d5`, 2 requis) et conflit sur `scripts/garde-mecanique.py` et `review_designation*.py` ; laissé à `ameesh-dev2`, brief en note (auditeur 19/10 10:41), pas de composition par le composeur |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 10:48 UTC, base
  `fcf4486e594c` : **27 candidats, 4 prêts, 23 en attente, 0 erreur**
  (`--ready-out` dans `$TMPDIR`). Les 4 prêts sont #2582, #2682, #2698 et
  #2556 ; les 3 premiers composés, #2698 écarté avec motif.
- Composition : **3 unités, 0 conflit** ; 13 fichiers, +963/−97 hors
  manifeste ; tête finale 14 fichiers, +1051/−97 (dont adaptation de test
  +8/−2 et manifeste +82) ; `git diff --check` vert, aucun marqueur de
  conflit, aucune migration SQL.
- `paquet-compose.py --units --base fcf4486e594c` : **3 unités de fusion,
  3 lots portés**.
- `paquet-inclusion` (tête composée, manifeste ajouté au-dessus,
  `--ready-file` recalculée) : **rc 0** — 3 admissions, 3 compositions,
  0 omission, 0 défaut. #2682 et #2556 sont `présents` ; #2582 est `partiel`
  à la tête (l'assertion de temps du témoin est remplacée par l'adaptation
  `2f12933d7`), écart lu et assumé, aucun autre écart ; #2698 seul écarté de
  la liste prête, motif lu.
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée,
  9 exemptions utilisées (toutes au registre).
- `scripts/doc-links.py` : 171 fichiers, **0 lien cassé**.
- Tests ciblés sur l'arbre composé : **267 tests OK dans les deux pilotes**
  (psql 330 s, psycopg 87 s), 0 échec, 0 erreur — modules
  `test_2582_alerts_console_timeout`, `test_2122_alerts_follow`,
  `test_1179_sonde_pgbouncer`, `test_532_pgbouncer_pool`,
  `test_l61_allers_retours`, `test_l38_notify`, `test_1060_alert_types`,
  `test_2446_train_shadow`, `test_2446_train_assembly`, `test_1681_incident`,
  `test_2556_tmp_copies_once`, `test_l73_housekeeping`. Le témoin 2582 adapté
  (commit `2f12933d7`) est vert dans les deux pilotes.
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
