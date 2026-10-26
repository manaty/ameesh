# Paquet integration/paquet-h-20261026-30 — recomposition 4 (§12), sur main issu de h-29

Base : `main` **`9e5e043d7a76f81516be3b8306eb6ecc152272d5`** (fusion du paquet
h-29, PR #2882, 26/10 15:23Z). Branche `integration/paquet-h-20261026-30`,
cible `main`. Porteur : **#3756**. Composeur : mty-amh-impl-01 (deepseek,
agents-1).

Déclencheur : décision pilote mesh-design du 26/10 17:42Z (lot #3756,
#145597), corrigée à 17:48Z. #3742 **ne sort pas** du paquet : sa tête
re-gelée **`923756e3e43e`** (gel #20581, 2/2 : ameesh-cx2 et auditeur) remplace
`f841c72c66e9` comme source composée ; elle porte sa propre adaptation des
témoins #2638, donc l'adaptation test_2638 du paquet est retirée. #3783 et
#3785, prêts, entrent sur fusion propre. Recomposition 4 : **15 unités** sur
`main` `9e5e043d`, une seule ci-full.

## Changements depuis le manifeste v3

- **#3742** : source `f841c72c66e9` (unité `e09daaeee419`) → **`923756e3e43e`**
  (gel #20581) ; le delta est test-only (témoins #2638/#2888 adaptés au refus
  de gel), relu dans le gel du lot. L'ancienne unité ne porte pas la tête
  gelée courante du lot et aurait retenu l'auto-fusion.
- **Adaptation test_2638 du paquet retirée** : sans elle, ce sont les témoins
  du code de #3742 qui font foi ; les deux autres adaptations (l161f, l87c)
  sont conservées.
- **#3783** (2/2, gel #20540) et **#3785** (1/1, gel #20563) ajoutés : fusions
  propres, aucun conflit, aucune union.

## Admissions (15 lots, 15 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3747 | `d13a8c54a454` | 2/2 (ameesh-dev3 deepseek, ameesh-dev2 claude) | work merged : _declared_merge_commit lit la cible locale (develop restée en retard) au lieu de origin/<cible> ; refus « has not entered target » à tort (#3732 Nexlink) |
| #3743 | `9a17eb86d4e2` | 1/1 (mty-amh-docs-01 deepseek) | Panier « #3733 » du 2026-10-26 : défauts non bloquants (N, P2, P3) |
| #3734 | `eb48d350a7fb` | 1/1 (mty-amh-impl-01 deepseek) | Panier « #3728 » du 2026-10-26 : défauts non bloquants (N, P2, P3) |
| #3742 | `923756e3e43e` | 2/2 (ameesh-cx2 codex, auditeur claude) | Gel : refuser un lot dont les familles d'auteurs (correcteurs compris) laissent moins de familles éligibles que d'avis requis — renvoi à l'auteur (§2) ; témoins #2638/#2888 adaptés au refus |
| #3752 | `560b6e80a020` | 1/1 (ameesh-dev3 deepseek) | #3749 étape 2 : schéma de test migré une fois par job (gabarit), puis cloné par classe au lieu de migrate() à chaque classe (tests/support.py) |
| #3767 | `ee50e13b1f08` | 1/1 (auditeur claude) | Handoff réel H8 (reprise de #3509) : droits Unix des effets locaux du handoff — même tête, gel neuf sans gels antérieurs |
| #3770 | `c08e92d56869` | 1/1 (ameesh-dev4 claude) | Handoff H8 : le dispatcher root laisse la socket client en fd 0 des enfants (reçu forgeable par un hook git) — préalable à l'enrôlement |
| #3753 | `7d140dd724bd` | 1/1 (auditeur claude) | #3749 étape 3 : cache en mémoire des lectures git du canon par (dépôt, tête) — canon._git lancé 2 176 fois par module |
| #3761 | `17d36feafaae` | 2/2 (ameesh-cx2 codex, ameesh-dev4 claude) | Passe alerts : 4 min 40 à 5 min 30 de CPU par passe, battement notify périmé, notify_undeliverable pour human:smichea |
| #3773 | `25b2073382ef` | 1/1 (ameesh-dev7 claude) | Panier « ameesh » du 2026-10-26 : défauts non bloquants (N, P2, P3) |
| #3762 | `fb03aa245f8a` | 2/2 (ameesh-dev2 claude, ameesh-dev3 deepseek) | Porte : un gel antérieur rebasé déjà dans la cible sous d'autres SHA garde ses commits en provenance inconnue — exclure par patch-id comme par ascendance |
| #3656 | `e0fc804726a4` | 1/1 (ameesh-dev4 claude) | Panier « #3648 » du 2026-10-26 : défauts non bloquants (N, P2, P3) |
| #3749 | `5595db3f3914` | 2/2 (mty-amh-docs-01 deepseek, ameesh-cx2 codex) | CI ÷10 : suite complète ameesh ≤ 10 min de mur (psql hors suite complète, schéma gabarit, cache git du canon, parallélisme) |
| #3783 | `a8486777d315` | 2/2 (auditeur claude, ameesh-cx2 codex) | ci status : le lecteur ne voit pas les « not ok » TAP de node (paquet Nexlink 191 : red 0 failure(s), courrier « no failed test » faux) |
| #3785 | `3f749580a8e1` | 1/1 (auditeur claude) | Tests handoff : la fixture `prepared()` de test_3470 est refusée (`reason: release`) sur ameesh-agents-2, en local seulement — test seul |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3747 | 20319 | #20324 ameesh-dev3 (deepseek) ok, #20336 ameesh-dev2 (claude) ok |
| #3743 | 20320 | #20334 mty-amh-docs-01 (deepseek) ok |
| #3734 | 20322 | #20355 mty-amh-impl-01 (deepseek) ok |
| #3742 | 20581 | #20585 ameesh-cx2 (codex) ok, #20586 auditeur (claude) ok |
| #3752 | 20380 | #20410 ameesh-dev3 (deepseek) ok |
| #3767 | 20408 | #20415 auditeur (claude) ok |
| #3770 | 20433 | #20485 ameesh-dev4 (claude) ok |
| #3753 | 20375 | #20471 auditeur (claude) ok |
| #3761 | 20449 | #20456 ameesh-cx2 (codex) ok, #20487 ameesh-dev4 (claude) ok |
| #3773 | 20491 | #20492 ameesh-dev7 (claude) ok |
| #3762 | 20445 | #20470 ameesh-dev2 (claude) ok, #20496 ameesh-dev3 (deepseek) ok |
| #3656 | 20257 | #20500 ameesh-dev4 (claude) ok |
| #3749 | 20428 | #20437 mty-amh-docs-01 (deepseek) ok, #20517 ameesh-cx2 (codex) ok |
| #3783 | 20540 | #20546 auditeur (claude) ok, #20547 ameesh-cx2 (codex) ok |
| #3785 | 20563 | #20565 auditeur (claude) ok |

## Composition (15 unités, 15 lots)

| unité | lot | source | commit |
|---|---|---|---|
| 1 | #3747 | `d13a8c54a454` | `a5015b8c6cf3` |
| 2 | #3743 | `9a17eb86d4e2` | `898b60779b09` |
| 3 | #3734 | `eb48d350a7fb` | `f9e9aa407d9d` |
| 4 | #3742 | `923756e3e43e` | `1c67bb9f3eea` |
| 5 | #3752 | `560b6e80a020` | `e6fb9fde8c34` |
| 6 | #3767 | `ee50e13b1f08` | `4af896a9ca84` |
| 7 | #3770 | `c08e92d56869` | `0195aacdaa90` |
| 8 | #3753 | `7d140dd724bd` | `754de61daf78` |
| 9 | #3761 | `17d36feafaae` | `35e5cc3ddf51` |
| 10 | #3773 | `25b2073382ef` | `5a9f33d25074` |
| 11 | #3762 | `fb03aa245f8a` | `c30558ce70e7` |
| 12 | #3656 | `e0fc804726a4` | `20c1b11ed26e` |
| 13 | #3749 | `5595db3f3914` | `fecd3870bd24` |
| 14 | #3783 | `a8486777d315` | `f57eedc23ccb` |
| 15 | #3785 | `3f749580a8e1` | `2c8c4fbf0dfb` |

15 fusions propres, aucune résolution mécanique, aucune adaptation de
composition. Le contrôle des définitions dupliquées sur `src/ameesh` ne trouve
aucun doublon introduit par le paquet (seul `reprise.recently_modified`
préexistant, hors paquet).

## Adaptations de test (aucun avis, §2/§11 bis)

Deux témoins périmés par les unités relues, corrigés en test seul ; aucun
changement de comportement produit :
- `tests/test_l161f_plan.py` : `test_3752_gabarit` ajouté à la liste « always »
  attendue, à sa place triée (il est dans `tests/parts/toujours.txt` depuis
  #3752).
- `tests/test_l87c_alert_ack.py` : `gate_cache` exclu du périmètre comparé —
  c'est un cache d'observation de #3761, pas un seuil (relu par le pilote,
  17:42Z).

Le test_2638 n'est **pas** adapté par le paquet : l'unité #3742 `923756e3e43e`
porte elle-même ses cinq témoins adaptés, relus et gelés avec le lot.

## Écartés de h-30 (v4)

| lot | motif |
|---|---|
| #3755 | B pilote 17:01Z et CI psql 11/16 : la migration 0490 ajoute `account_active.forced_until`, `quota_gauge_readings.overage_capable` et `quota_gauge_readings.overage_in_use` sans les classer dans le contrat superviseur (`deploy/sql/role-superviseur*.sql`) ni dans `EXCLUES` ; `test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue` rouge, reproduit localement ; retiré, à corriger à part |
| #3735 | B pilote 16:15Z sur l'ancienne tête : fuite d'une connexion par classe (`_drop_schema` après `tearDownClass`, reconnexion `PsycopgDriver._live`) — psycopg 4/8 « too many clients ». Re-gelé `ddd1305f` avec correctif ; liste calculée 16:41Z : conflit avec main (`src/ameesh/cli.py`), résolution non faite — reste dehors |
| #3782 | B cx2 sur `784f8195d`, corrigé et gelé `d206881414a0` (non suivis sous `src`, `code_reference`) ; 1 OK cx2, relecture auditeur en attente — pas encore prêt |
| #3519 | hold pilote (mesh-design, 12:32Z) : chaîne handoff |
| #3526 | aucun avis sur le gel `30a916ad` (sensible) et conflit avec main (`docs/ops/handoff-staging-enrollment.md`, `pyproject.toml`) |
| #3506 | erreur de lecture de la porte (provenance d'auteur inconnue) ; hors paquet par consigne pilote (chaîne handoff) |
| #3756 | véhicule du paquet, jamais une unité |

## Contrôles

- Composition : 15 unités sur `9e5e043d` ; 15 fusions propres, 0 résolution,
  0 union, 2 adaptations de test.
- Tests locaux avant poussée : **22 modules, 668 tests, 0 échec, 10 sautés**.
  Liste `tests/parts/toujours.txt` (11 modules) + `test_l161f_plan` +
  `test_3752_gabarit` + `test_3753_cache_git_canon` + `test_3749_psql_nocturne`
  + `test_940_agregat_bash` + `test_2638_reviewer_fix_family` +
  `test_3742_freeze_author_families` + `test_l87c_alert_ack` +
  `test_l161c_failures_realtime` + `test_3469_handoff_effects` +
  `test_3470_handoff_activation` + `test_3509_local_handoff`. Détail :
  `test_2638` 6 OK (unité #3742), `test_3742` 23 OK, `test_l87c` 28 OK,
  `test_l161c_failures_realtime` 114 OK, `test_3470` 33 OK,
  `test_3509` 25 OK (les deux grâce à #3785).
  **Réserve d'environnement, consignée** : `test_3469` a d'abord rendu 16
  erreurs de `setUp` (`FactoryError: source:pont-handoff.py`) sur le checkout
  local en mode **0664** (umask 0002) — `_trusted_source` refuse un compagnon
  groupe-écriture, ce que la CI (checkout 0644) ne fait pas ; rejeu des mêmes
  74 tests après passage en 0644 : **OK**. Aucun défaut produit, aucun test
  négatif.
- Contrôles statiques du gel : `scripts/freeze-checks.py` verts (8 contrôles :
  garde mécanique, compilation, attribution, lint L63, types d'alerte, lecteurs
  de doc, contrat de train, contrat de la porte).
- Contrôle d'inclusion `scripts/paquet-inclusion.py` sur la tête : **vert — 15
  compositions, 15 contenus présents, aucun défaut** (liste prête étendue à
  #3783 et #3785 sur décision pilote 17:48Z).
- Prévalidation : non bloquante, journalisée par `ameesh ci run` ; sélection
  attendue non sûre (`.github/workflows/psql-nocturne.yml` de #3749, Python
  hors `src` de #3785).
