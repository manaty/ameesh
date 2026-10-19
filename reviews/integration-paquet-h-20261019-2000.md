# Paquet integration/paquet-h-20261019-2000 — composition (§12)

Base : `main` **`b59b96241328`** (paquet h-1900 fusionné, PR #2093, merge du
19/10 19:47 UTC). Branche `integration/paquet-h-20261019-2000`, cible `main`.
Une seule suite complète est demandée sur la tête finale.

Demandes : composeur, paquet horaire §11 bis. Liste calculée
`scripts/paquet-candidats.py` à 20:01 UTC sur `b59b96241328` (lecture fraîche
d'`origin/main`) : **4 prêts, 12 en attente, 0 erreur**. Les 4 lots prêts sont
composés ; deux remises officielles sont ajoutées (#2443 `848efe7a`, #2757
`97155ecc`), soit **6 unités, 6 lots portés**.

## Admissions (6 unités, 6 lots portés)

| lot | source | motif |
|---|---|---|
| #2739 | `8b5c419ce890` (tête gelée) | prêt mécanique, porte OK (cx3 Codex + dev1 Claude) ; sensible (review_*, fire, merge_queue, lot_class) |
| #2752 | `63e03bae6f42` (tête gelée) | prêt mécanique, porte OK (cx1 Codex + dev5 DeepSeek) ; sensible (mailbox PostgreSQL) |
| #1022 | `c0d5a418064c` (tête gelée) | prêt mécanique, porte OK (cx3 Codex) ; léger (témoins de la migration 0420) |
| #2660 | `bdbfab85e4ff` (tête gelée) | prêt mécanique, porte OK (auditeur Claude + cx1 Codex) ; sensible (cible de migration) |
| #2443 | `848efe7a86f7` (remise sur `b59b9624`) | tête `4575e7fd`, verdict **dev4** (Claude) ok le 19/10 17:50 ; **admission manuelle** : le pilote mesh-design a acté le 19/10 09:06 qu'un avis d'une autre famille suffit pour ce lot (migration 0465 sans GRANT) alors que la porte calculée en exige deux et que les trois familles sont autrices du delta ; la remise `848efe7a` est l'arbre de la résolution `30225ea0` + delta main h-1900 (`git diff 30225ea0..7d047462` = `ab845adb..b59b9624`), aucun delta de contenu #2443 non relu |
| #2757 | `97155ecc6f5d` (résolution mécanique) | tête `a605ff17`, porte OK (2 avis) ; résolution **jugée mécanique par mesh-design** (19/10), à reprendre telle quelle au prochain véhicule |

## Composition (6 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2739 | `8b5c419ce890` | `f3c5bdac2afb` | sensible (review_authors/designation, fire, merge_queue, lot_class) ; 0 conflit |
| 2 | #2752 | `63e03bae6f42` | `086225abf1e5` | sensible (storage/postgres/mailbox.py) ; 0 conflit |
| 3 | #1022 | `c0d5a418064c` | `7d0130d34d66` | léger (témoins migration 0420) ; 0 conflit |
| 4 | #2660 | `bdbfab85e4ff` | `b985f3af0e61` | sensible (cible de migration, work/mesh_cli/lot_class) ; 0 conflit |
| 5 | #2443 | `848efe7a86f7` | `3745c167977e` | sensible (migration 0465, plan_git, packet_link, work) ; 0 conflit |
| 6 | #2757 | `97155ecc6f5d` | `5acb54966340` | sensible (sous_utilisation, exploitation, docs) ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>` (relu par
`scripts/paquet-compose.py --units --base b59b9624` : 6 unités, 6 lots portés).
Aucun conflit de composition, aucune résolution improvisée, une seule
migration SQL ajoutée (0465, additive, sans GRANT ; L122).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #114 | non prêt : 1 avis indépendant manquant sur `89bdc6b50e93` (seul mty-amh-impl-07, deepseek) |
| #2538 | non prêt : 1 avis indépendant manquant sur `e7834ed960b7` (seul cx3, codex) |
| #2562 | non prêt : 1 avis indépendant manquant sur `2ec7a16ea8a0` (seul impl-08, deepseek) |
| #2696 | non prêt : gel effectif refusé (`blocked-verdict`) |
| #2702 | non prêt : 1 avis indépendant manquant sur `306134a9f100` (seul cx1, codex) |
| #2729 | non prêt : 1 avis indépendant manquant sur `e21c15cca227` (seul cx1, codex) |
| #2738 | non prêt : 1 avis indépendant manquant sur `018d6d2d462e` (seul dev7, claude) |
| #2743 | non prêt : gel effectif refusé (`blocked-verdict`) |
| #2763 | non prêt : 1 avis indépendant manquant sur `cf57f3de4a7d` (seul impl-06, deepseek) |
| #2766 | non prêt : 1 avis indépendant manquant sur `6fe2f870d06a` (seul dev1, claude) |

Les autres lots en attente sont exclus par la liste calculée ; aucun n'est
admis sans porte verte, hors l'admission manuelle motivée de #2443 ci-dessus et
la résolution mécanique de #2757.

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 20:01 UTC, base
  `b59b96241328` : **4 prêts, 12 en attente, 0 erreur** ; les 4 prêts sont
  composés.
- Composition : **6 unités, 0 conflit** ; 39 fichiers, +2025/−149 ;
  `git diff --check` vert, aucun marqueur de conflit, une seule migration SQL
  ajoutée (0465) et aucune migration modifiée.
- `paquet-compose.py --units --base b59b9624` : **6 unités de fusion,
  6 lots portés** (#2739 `8b5c419ce890`, #2752 `63e03bae6f42`, #1022
  `c0d5a418064c`, #2660 `bdbfab85e4ff`, #2443 `848efe7a86f7`, #2757
  `97155ecc6f5d`).
- `paquet-inclusion` (tête finale, manifeste ajouté au-dessus, `--ready-file`
  recalculée) : **rc 0** — 6 admissions, 6 compositions, 0 omission,
  0 défaut, 6/6 contenus présents à la tête.
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée
  (1 exemption utilisée, #2642).
- `scripts/doc-links.py` : 171 fichiers, **0 lien cassé**.
- Tests ciblés sur l'arbre composé : modules des 6 unités (psql + psycopg) —
  à consigner au fil du run.
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).
