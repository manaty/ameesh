# Paquet integration/paquet-h-20261026-32 — composition (§12), sur main issu de h-31

Base : `main` **`7280d0267ae9a78bab18e55412bf1723c379f68e`** (fusion du paquet
h-31, PR #2913, tête `6d89c0ae`, posé sur la release 1.24.0 `04cf3474`).
Branche `integration/paquet-h-20261026-32`, cible `main`. Porteur : **#3806**.
Composeur : mty-amh-impl-01 (deepseek, agents-1).

Déclencheur : lot #3806 (mesh-design, 26/10 20:15Z ; go 22:41Z sur
`7280d0267`). Méthode de h-31 : liste calculée `scripts/paquet-candidats.py`
depuis la tête exacte de `main` (`--base` = tête de composition), tests
locaux **avant** le gel (liste `tests/parts/toujours.txt`, `test_l161f_plan`,
`test_l107_executor_contract`, `test_l224_session_harnais`, `test_cluster` et
modules des unités), manifeste, contrôle d'inclusion, `freeze-checks`, puis
une seule `ci run --full --avis 0` sur la tête gelée.

## Admissions (11 lots, 9 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3779 | `d6dab7d12402` | gel #20875 : #20876 auditeur ok, #20877 ameesh-cx2 ok ; « verdict non requis » pilote | Codex : suivre la fenêtre courte du fournisseur (429 en bloc) — migration 0491 additive |
| #3755 | `bced7be1b88e` | gel #20859 : #20864 ameesh-cx1 ok, #20872 auditeur ok | accounts use : forçage avec garde et échéance, overage partagé — migration 0490 additive |
| #3800 | `2021a7832889` | gel #20730 : #20739 ameesh-cx1 ok, #20741 ameesh-cx2 ok, #20742 auditeur ok | Prévalidation au gel : jamais « rien », lancer au moins les contrats |
| #3754 | `970780fdfdd4` | gel #20753 : #20757 ameesh-cx2 ok, #20765 auditeur ok | #3749 étape 4 : mesure et rééquilibrage des parts, parallélisme du job |
| #3781 | `6bdaa808954b` | gel #20708 : #20713 auditeur ok, #20724 ameesh-dev3 ok | Provenance : copies rebasées dont le porteur cible a ensuite été réécrit |
| #3805 | `04df3608b882` | gel #20834 : #20836 ameesh-cx1 ok, #20839 auditeur ok | Porte §2 : un commit rebasé par un autre agent perd sa famille d'auteur |
| #3807 | `da1786106a5c` | gel #20865 : #20867 ameesh-cx2 ok, #20871 auditeur ok | Paquet horaire sans agent : `scripts/paquet-auto.py` compose sans jugement |
| #3802 | `30776f353cea` | gel #20853 : #20860 ameesh-cx1 ok, #20863 ameesh-cx2 ok, #20869 auditeur ok | Ménage : borner la passe et nommer le détenteur de `host-pass.lock` |
| #3801 | `c86977d6c983` | gel #20774 : #20777 ameesh-cx2 ok, #20778 auditeur ok | Paquet rouge : nommer l'unité fautive probable dans `ci status` et le courrier |
| #3777 | `cefa70c03c05` | gel #20686 : #20699 auditeur ok | Panier « #3753 » du 2026-10-26 : défauts non bloquants (N, P2, P3) |
| #3818 | `b4721acd5385` | gel #20889 : #20891 auditeur ok | Débit : un avis reste « dû » après « verdict not required » du pilote |

Deux unités portent deux lots via les résolutions préparées et relues :
`refs/ameesh/resolutions/3755/3779` = `f3c896b8` (parents `bced7be1` +
`d6dab7d1`, compteurs du doc 449/521) et `refs/ameesh/resolutions/3800/3754`
= `5a7306f3` (parents `2021a783` + `970780fd`).

## Composition (9 unités, 11 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3779, #3755 | `f3c896b8fad2` | `963f1e76c201` | résolution : migrations 0491 + 0490 |
| 2 | #3800, #3754 | `5a7306f3d2df` | `5aaab1b0dc19` | résolution |
| 3 | #3781 | `6bdaa808954b` | `4d582e97b449` | composition |
| 4 | #3805 | `04df3608b882` | `b0da2a773489` | composition |
| 5 | #3807 | `da1786106a5c` | `bd96cea9d89e` | composition |
| 6 | #3802 | `30776f353cea` | `3542901a763c` | composition |
| 7 | #3801 | `c86977d6c983` | `543c6cb32b00` | composition |
| 8 | #3777 | `cefa70c03c05` | `728eb374745c` | composition |
| 9 | #3818 | `b4721acd5385` | `17b630f5537b` | composition |

9 fusions propres, aucune résolution mécanique par le composeur (les deux
unions sont les résolutions relues, portées par leurs commits), aucune
adaptation de test. Le contrôle des définitions dupliquées sur `src/ameesh`
ne trouve aucun doublon introduit par le paquet.

## Migrations et exploitation

- **0490 (#3755)** : additive (deux colonnes nullables `account_active.forced_until`,
  `quota_gauge_readings.overage_capable/overage_in_use`) ; elle borne à une
  heure les forçages de compte **en cours**. Au déploiement staging qui la
  porte, le bureau **relève les forçages actifs** (`ameesh accounts list`
  sur les hôtes qui les voient) **avant** d'appliquer la migration, puis les
  repose ou les déclare levés (note pilote 15:58Z, lot #3755).
- **0491 (#3779)** : additive (journal partagé `account_rate_events`,
  `account_turn_starts`), aucune donnée réécrite.
- Migrations appliquées **avant** de relancer les exécuteurs (§11 bis, §14).

## Adaptations de test

Aucune : les résolutions #3755/#3779 et #3800/#3754 portent déjà leurs
adaptations, relues avec leurs gels ; `test_l107_executor_contract` et
`test_l224_session_harnais` sont rejoués sur la tête composée (voir
Contrôles) pour vérifier que les compteurs du doc donnent bien 449/521.

## Écartés de h-32

| lot | motif |
|---|---|
| #3506 | en **erreur** dans la liste calculée (`author provenance unknown` sur cinq commits d'anciens gels : `790265d8`, `37d1dea1`, `9bc2b727`, `0a1a17d5`, `bd7ab9f8`) — provenance illisible, cas #3781 ; écarté sur autorisation du pilote (22:41Z). |
| #3519 | hors paquet : attend la fusion de #3781 puis son staging (corps du lot #3806) |
| #3756 | véhicule du paquet h-30, jamais une unité |

## Contrôles

- Composition : 9 unités sur `7280d0267`, 9 fusions propres, 0 résolution,
  0 union par le composeur, 0 adaptation.
- Tests locaux avant gel : ****1089 tests, 0 échec, 10 sautés** (36 modules, 18 min 31 s, banc 55432 partagé) — liste `tests/parts/toujours.txt` (11 modules), `test_l161f_plan`, `test_l107_executor_contract`, `test_l224_session_harnais` et les modules des unités (`test_1770_prevalidation`, `test_1883_overage_boucle`, `test_2180_housekeeping_discovery`, `test_2265_critical_policy_drift`, `test_2644_prevalidation_calls`, `test_2743_housekeeping_clones`, `test_3292_debit`, `test_3396_housekeeping_unreadable`, `test_3753_cache_git_canon`, `test_3754_parallel_parts`, `test_3755_forcage_garde_echeance`, `test_3779_codex_short_window`, `test_3781_target_landing_provenance`, `test_3801_packet_red`, `test_3802_host_pass_lock`, `test_3805_rebased_author_family`, `test_3807_paquet_auto`, `test_l148_stage6_json_keys`, `test_l161c_failures_realtime`, `test_l231_deps`, `test_l26_exploitation`, `test_l30_accounts`)**.
- Contrôles statiques du gel : `scripts/freeze-checks.py` **8/8 verts**.
- Contrôle d'inclusion `scripts/paquet-inclusion.py` sur la tête : **vert — 9 compositions, 11 contenus présents, aucun défaut**
  (liste prête calculée sur `7280d0267` : 11 lots, #3506 en erreur).
- Prévalidation : non bloquante, journalisée par `ameesh ci run` ; sélection
  attendue non sûre (Python hors `src/ameesh/` : `scripts/paquet-candidats.py`,
  `scripts/paquet-auto.py`, `deploy/mise-a-jour/bascule.py`).
