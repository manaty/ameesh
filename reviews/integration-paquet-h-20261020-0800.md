# Paquet integration/paquet-h-20261020-0800 — composition (§12)

Base : `main` **`322bc5149447043eb98e55947125cb49efa95c1b`** (fusion du paquet
h-0600, PR #2156, 20/10 05:59:39Z). Branche
`integration/paquet-h-20261020-0800`, cible `main`. Une seule suite complète
est demandée sur la tête finale.

Demandes : bureau 06:36Z — « h-0600 fusionné (07:57). 6 lots ameesh
« waiting: merge » ... compose h-0800 maintenant ; je le fusionne au vert. »
La liste calculée `scripts/paquet-candidats.py` à 06:41Z sur `322bc5149` rend
exactement **6 prêts, 8 en attente, 0 erreur** (`rc 0`, `--ready-out` écrit).
L'auditeur (06:21) demande #2839 dans ce paquet ; mesh-design est en copie.

## Admissions (6 unités, 6 lots portés)

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #2839 | `3aeb90bbbdbb` (tête gelée) | sensitive | impl-04 (DeepSeek) + auditeur (Claude) demandé | comptes codex : une fenêtre courte à 100 % doit rendre le compte au seuil et le faire basculer ; P1 SLO, activation staging = séparation réversible des 2 journaux pro de `~/.codex-secondary/sessions` sur pc-smichea (auditeur 06:21) |
| #2775 | `b3301624e0fb` (tête gelée) | light | impl-04 (DeepSeek) | panier #2766 : un landing déclaré sans gel refuse le véhicule avant `in_vehicle` |
| #2813 | `e5f62e692ff4` (tête gelée) | sensitive | cx3 (Codex) + impl-04 (DeepSeek) | paquets : l'auto-merge est armé à la publication du véhicule ; relu après h-0600, entre ici |
| #2820 | `21610c899bdd` (tête gelée) | sensitive | impl-04 (DeepSeek), un avis requis au gel | runner : rotation de `events.jsonl` au début du tour au-delà de `policy.housekeeping.events_quota` (64 MiB), une génération `.1` gardée |
| #2844 | `2b5e5dd72560` (tête gelée) | light | impl-01 (DeepSeek) | demandes GitHub : commentaires d'étape bornés sur le ticket miroir ; B1/B2/B3 et citation PR corrigés |
| #2759 | `4a30ada72a8e` (tête gelée) | sensitive | auditeur (Claude) + cx3 (Codex) | jauges : historique isolé par lecture et par compte, pas de mélange entre lectures |

## Composition (6 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2839 | `3aeb90bbbdbb` | `8c75135a6db0` | sensitive ; 0 conflit |
| 2 | #2775 | `b3301624e0fb` | `cdac1fd21475` | light ; 0 conflit |
| 3 | #2813 | `e5f62e692ff4` | `fc48773e9bb4` | sensitive ; 0 conflit |
| 4 | #2820 | `21610c899bdd` | `1257517dd4c7` | sensitive ; 0 conflit |
| 5 | #2844 | `2b5e5dd72560` | `4a35085bd972` | light ; 0 conflit |
| 6 | #2759 | `4a30ada72a8e` | `f3cc4d10d01d` | sensitive ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`
(`scripts/paquet-compose.py --units --base origin/main` : 6 unités de fusion,
6 lots portés : #2839 3aeb90bbbdbb, #2775 b3301624e0fb, #2813 e5f62e692ff4,
#2820 21610c899bdd, #2844 2b5e5dd72560, #2759 4a30ada72a8e).

## Résolutions de composition

Aucune : les six fusions sont propres (`git status` sans conflit, aucun
marqueur de conflit). Le seul contenu hors fusions est ce manifeste.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #114 | `verdicts-missing` : un seul avis (impl-07, DeepSeek) sur le gel `89bdc6b50e93`, deux avis de familles distinctes requis (sensible) |
| #1515 | `blocked-verdict` : gel effectif refusé (`blocked-verdict`) |
| #2614 | `no-verdict` : gel effectif refusé (`no-verdict`) |
| #2771 | `no-verdict` : gel effectif refusé (`no-verdict`) |
| #2786 | `conflict` avec `main` sur `src/ameesh/plan_git.py` ; remise `main-797779c2` refusée : un seul avis (auditeur, Claude), non mécanique |
| #2808 | `verdicts-missing` : un seul avis (auditeur, Claude) sur le gel `d4b1a43da94b`, deux avis requis (sensible) |
| #2814 | `verdicts-missing` : un seul avis (auditeur, Claude) sur le gel `31739fe46624`, deux avis requis (`0469_host_cgroup.sql`) |
| #2845 | `blocked-verdict` : verdict bloqué sur `d0f3f0eb67b0` (B1 contenu externe non borné, B2 troncature silencieuse) ; reprise après nouvelle tête gelée |

Note : #2813 était écarté de h-0600 (composé à 04:46 sur la liste de 04:39) ;
il entre dans ce véhicule avec sa tête `e5f62e692ff4`.

## Contrôles

- Liste calculée `scripts/paquet-candidats.py`, base `322bc5149` :
  **6 prêts, 8 en attente, 0 erreur** (`rc 0`) ; les 6 prêts sont composés,
  aucun écarté.
- Composition : **6 unités, 6 lots** ; 34 fichiers, +3473/−121 hors manifeste ;
  aucun conflit, aucune résolution ; `git diff --check` ne relève rien.
- `scripts/paquet-inclusion.py` (manifeste, tête de composition, attendus et
  prêts) : **aucun défaut** — 6 compositions, 0 exclusion, contenus à la tête :
  6 présents, 0 partiel.
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée.
- `scripts/doc-links.py` : 172 fichiers, **0 lien cassé**.
- `scripts/freeze-checks.py` : **6 contrôles verts**.
- Tests ciblés Python sur l'arbre composé (pilote psql local) :
  **317 tests, 0 échec** (`test_2813_packet_auto_merge`, `test_2820_events_rotation`,
  `test_2839_codex_account_gauges`, `test_2759_gauge_history_isolation`,
  `test_2844_github_stage_comments`, `test_2766_vehicle_reviews`,
  `test_1656_auto_merge`, `test_2601_packet_watch`, `test_cost`,
  `test_l126_issues`, `test_2130_d_projection`) ; la suite complète les couvre
  en CI.
- Migrations : **aucune** au diff (`git diff --name-only` sans `.sql` ni
  `migrations/`).
- UI : aucun fichier UI au diff.
- Prévalidation : indisponible (carte périmée) ; sélection non sûre, la suite
  complète est demandée, motif journalisé.
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).

## Correctif de composition (CI 39382671331)

- `test_2022_fleet` (#2022) : le second appel `--json` du test de l'hôte canon
  recevait une ligne datée du journal asynchrone de l'exécuteur
  (`ameesh.runner.log_async`, fil `journal`) encore en file après
  `test_1466_relecture_config` ; `json.loads` la lisait comme du JSON
  (« Extra data: line 1 column 2 (char 1) »). Le captage du test n'accepte
  désormais que les écritures de son propre fil (`_OwnThreadOutput`) et un
  test de régression force une écriture du fil `journal` pendant le captage.
  Vert localement sous les deux pilotes (`test_2022_fleet`, 11 tests). Suite
  complète redemandée sur cette tête.

## Notes d'activation

- #2839 (P1, SLO) : au staging, le geste opérateur réversible prévu avec cx1
  doit accompagner le lot — sortir les 2 journaux pro de
  `~/.codex-secondary/sessions` sur pc-smichea. Sans lui, codex/secondaire est
  refusé jusqu'au 25/10 et le primaire est à 50 % (auditeur 06:21).
- #2813 : l'auto-merge est armé à la publication du véhicule ; le paquet reste
  fusionné par le bureau au vert.
- #2844 : la première passe ne commente que les tickets ouverts/nouveaux ; un
  ticket déjà fermé ne reçoit que sa preuve de livraison (B1).
