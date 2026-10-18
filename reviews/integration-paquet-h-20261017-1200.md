# Paquet integration/paquet-h-20261017-1200 — composition

Base : `main` **`598deccaafb46586fedd3f0a8ec02d7aef581f08`** (#2217/PR1613 atterri à
12:24:42Z, hors paquet : 2 fichiers, `src/ameesh/slo.py` et `tests/test_789_slo.py`). La branche est
recomposée directement sur cette base après le refus au plan de la première demande CI
(merge-ref base `598deccaa` contre payload base `eba7b77e0`) : le diff de la PR ne porte que les
16 unités ci-dessous, ce manifeste et le registre.

Demandes : **bureau** 11:37Z (composer le paquet suivant immédiatement, **#2229** P1 en priorité,
puis les lots prêts à verdicts complets) ; **pilote mesh-design** 11:38Z (#920 après #2229, remise
fraîche `e91274f9`). #2155, #2156 et #2105, initialement prévus pour le véhicule de 23:00 (pilote
11:03Z), sont repris dans ce paquet : #2155 et #2156 ont acquis leur seconde famille (Claude
auditeur, 10:11/10:30Z) et #2105 est regelé sur `5eb76d9d` (GO R1). Leur inscription reste soumise
au contrôle unitaire du pilote.

**Pilote** 12:36Z : nouvelle tête #2229 `255d7bc71a41` (correction B1 de cx2/Codex, +22/−2),
à relire par deux familles sur cette tête. **Deux verdicts SQL `ok` acquis à 12:40Z** :
mty-amh-impl-05 (DeepSeek, lot entier avec correction) et ameesh-dev4 (Claude, delta
`e59b5a71..255d7bc7`) ; cx2/Codex, auteur de la correction, ne donne pas de GO à sa propre
correction. **Bureau** 12:45Z (urgent) : intégrer la tête corrigée, lancer l'unique suite complète
et demander le feu ; #2229 débloque le passage du poste en 1.11.5.

**Bureau** 12:56Z (urgent) : la première demande CI est refusée au plan (merge-ref base
`598deccaa`, payload base `eba7b77e0` : « the composition moved, refreeze »). Recomposition sur
`origin/main` `598deccaa`, regel de la nouvelle tête et nouvelle demande `ci-full-<sha>` ; les
verdicts unité par unité restent valables, les têtes des lots ne changent pas.

**Pilote** 11:48/11:49Z : ajouter #1186 (tête `d227973bd833`, re-gelée, light), #1534 (tête
`343f9676`, light, PR1612, auto-fusion désarmée) et la résolution `6716d380` (1534×1104, vérifiée
par impl-07) ; #2105 confirmé. **#1104 n'est pas dans ce paquet** : l'auteur de #1534 (impl-02,
11:59Z) demande de rejouer la résolution **après l'atterrissage de #1534, pas avant** ; elle est
conservée pour le paquet suivant. #1186 et #1534 arrivent après #920 et #2105.

Exécuteur : **mty-amh-impl-01** (composition, unique CI de paquet, fusion). Aucune CI de lot, aucun
push de branche de lot, aucune fusion parallèle.

## Composition (16 unités)

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #2229 | `255d7bc71a41` (`lot/2229-overview-runner-seen`, PR1608) | `57bbf48d8` | P1 bureau ; migration **0460** additive (vue `agent_mesh_overview` recréée, `runner_seen_*`) + `test_migrations_vues` ; correction B1 ACL `255d7bc7` (cx2/Codex, +22/−2) ; 2 f. +240/−2 ; **deux verdicts SQL ok sur `255d7bc7`** (impl-05 DeepSeek, dev4 Claude, 12:40Z) |
| 2 | #920 | remise `e91274f9` (`refs/ameesh/resolutions/920/main-20aa67782`) | `68fea3e20` | GO auditeur (Claude) + cx2 (Codex) sur `f2db2cf2` ; remise fraîche parents `20aa67782` + `b78a5e33`, pilot 11:38Z ; 3 f. +18/−10 |
| 3 | #2155 | `d2edaa330842` (`lot/2155-console-scripts-venv-stable`) | `9db6b4c06` | deux familles : Codex mesh-design 04:10 + Claude auditeur 10:11 sur `d2edaa33` ; prod `deploy/mise-a-jour/bascule.py` ; 2 f. +115/−4 |
| 4 | #2156 | `0835fdeb6f6d` (`lot/2156-shebang-wrapper-pip`) | `bb96989f0` | deux familles : Codex 9956 + Claude auditeur 10171 sur `0835fdeb` ; prod `bascule.py` ; 3 f. +71/−7 |
| 5 | #2105 | `5eb76d9d9f72` (`lot/2105-notify-perimetre`) | `7c12e1a11` | GO R1 ameesh-dev4 sur `5eb76d9d` ; classe légère ; merge-clean `b4ac85d9`, 28/28 psql+psycopg ; 3 f. +29/−4 |
| 6 | #1467 | `bf8d2f430857` (`lot/1467-automatic-integration-train`) | `b6d645f09` | source gelée `0234aafa` conservée (`refs/ameesh/1467/source-0234aafa`) ; avis Claude dev4 9144 + DeepSeek deepseek3 9152 ; transport mécanique auditeur 11:16Z (SLO P1) ; migration **0442** libre, 0445 déjà dans main ; 64 f. +11997/−40 |
| 7 | #2186 | `387d46df1661` (`lot/2186-etat-version-controles`) | `334076aac` | verdict mesh-design ; dépendance #2043 fusionnée ; 2 f. +111/−29 |
| 8 | #2008 | `bd6931cb4a7b` (`lot/2008-argv-idempotent`) | `0865967fe` | GO ameesh-dev1 ; 2 f. +35/−3 |
| 9 | #2098 | `4a4380d819d6` (`agent/deepseek7-2098-walrus-nested`) | `409cd673f` | verdict mesh-design ; 2 f. +31/−7 |
| 10 | #1578 | `cb487e2f8c7d` (`lot/1578-tarball-symlink-doc`) | `0bbda4434` | GO deepseek5 (auteur Codex) ; **classe légère** (arbitrage pilote 09:03Z : `deploy/**` doc/commentaires seulement, aucune ligne exécutable) ; 2 f. +11/−1 |
| 11 | #2180 | `731e25fc9b6f` (`lot/2180-housekeeping-discovery`) | `ffd99fda2` | verdict claude1 (DeepSeek ≠ auteur Codex) ; sensible `storage/interface.py` (docstring seule), un avis suffit hors SQL/sécurité/prod ; 5 f. +411/−7 |
| 12 | #1726 | `7965a8700f49` (`lot/1726-relecteur-designe-repos`) | `39d231249` | verdict ameesh-dev7 ; 3 f. +90/−8 |
| 13 | #2185 | `51f81874d05f` (`lot/2185-suite-cli-roadmap`) | `fd3ae9cb4` | verdict deepseek5 (auteur ; lot léger, revue d'après-fusion à prévoir) ; SLO L3 signalé faute de véhicule ; 3 f. +176/−32 |
| 14 | #2173 | `2efb48e8f128` (`lot/2173-housekeeping-skipped`) | `46dd8d8c8` | GO mesh-design 07:56 ; classe légère ; 2 f. +50/−7 |
| 15 | #1186 | `d227973bd833` (`lot/1186-suite-1070-n1-n4`) | `886118958` | light, re-gelée sur `d227973b` (auto-fusion désarmée) ; CI de lot verte run `39030083525` sur cette tête ; suite #1070 (code en anglais, `paused_on_budget`, alias `progress.paused_on_budget`) ; 4 f. +75/−74 |
| 16 | #1534 | `343f9676e442` (`lot/1534-n-relecture-claude`, PR1612) | `84b66f64a` | **R1 ok ameesh-dev4** (Claude, famille ≠ auteur) sur le gel `343f9676`, 0 bloquant, aucune correction, pas de regel ; light, auto-fusion désarmée, aucune CI de lot ; #1465 N de la relecture Claude ; 7 f. +244/−26 ; merge-tree propre sur la tête 1200 |

Les commits de composition ont le lot en second parent, leur sujet nomme le lot et ils portent le
trailer `ameesh-work: #<lot>` (#2157). Aucun contenu nouveau : chaque unité est la tête ou la remise
exacte listée ci-dessus.

## Contrôle unitaire du pilote — points ouverts

- **#2229** : correction B1 intégrée (tête `255d7bc7`, composition `57bbf48d8`), deux verdicts SQL
  `ok` sur cette tête (impl-05 DeepSeek, dev4 Claude), cx2 auteur de la correction exclu de son
  propre GO ; tests ciblés `test_migrations_vues` + `test_migrations_additives` = 5 OK psql +
  5 OK psycopg sur la tête composée. Tête recomposée sur `598deccaa` : regeler puis relancer
  `ci-full-<sha>`.
- **#2185** : verdict enregistré par l'auteur (deepseek5) sur un lot léger — revue d'après-fusion par
  une autre famille à prévoir (§2).
- **#2155/#2156** : chemins prod (`deploy/mise-a-jour/bascule.py`), deux familles exigées et
  acquises ; à recontrôler sur les têtes listées.
- **#1467** : transport mécanique d'une source gelée revue (9144 + 9152) ; delta #1467 inchangé au
  patch près, migration 0442 libre ; à confirmer au contrôle.
- **#1186** : light, tête `d227973b` re-gelée (auto-fusion désarmée), gel enregistré par deepseek5,
  CI de lot verte run `39030083525` — confirmé.
- **#1534** : R1 ok ameesh-dev4 (Claude) sur le gel `343f9676`, 0 bloquant, pas de regel ;
  merge-tree propre sur la tête 1200, auto-fusion désarmée, aucune CI de lot ; preuves ciblées
  impl-02 (30 provenance + 84 non-Pg + 93 Pg + 3 lint).

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #1104 | résolution 1534×1104 `6716d380` à rejouer **après** l'atterrissage de #1534, pas dans ce paquet (consigne de l'auteur impl-02, 11:59Z) ; #1534 est inclus |
| #1664 | remise `63f3d4787` en conflit sur la tête du paquet ; résolution `b8e54f39` inclut #2200 (qa) ; à rejouer |
| #1768 | P1 porte de fusion : la remise `1b12ade5` contient `e7ded2a4`, « résolution fonctionnelle à relire » (sécurité) ; aucun avis sur ce delta |
| #1466 | P1 : résolutions `4c37f19c` et `2f40bf1b` en conflit sur la tête ; résolution fraîche requise |
| #2032 | hold planifié `gate:paquet-h-1900` non renouvelé ; #2090 en dépend |
| #1747 | admission pilote du 16/10 pour le paquet h-1900, non renouvelée ; hold |
| #1515 | hold : second avis sécurité Codex impossible avant le 22/10 08:13 |
| #1630 | hold : CI annulée par l'owner, attente de relance/consigne |
| #2160 | hold : seconde famille Codex impossible avant le 22/10 |
| #1272 | verdict attendu (ameesh-dev1) |
| #800 | qa, non admis à ce véhicule ; résolution `5cd0c10` prête pour un paquet compatible |
| #2076, #2084 | décision de l'owner : deux familles de relecture indisponibles ; hors paquet |
| #377 | 57 conflits, résolution mécanique impossible ; véhicule dédié à désigner |

Les autres lots en `qa` non retenus le sont pour conflit sans résolution fraîche sur cette tête ou
pour gel non couvert par un verdict ; ils restent candidats aux paquets suivants.

## Preuves

- `git diff --check` propre, aucun marqueur de conflit.
- Chaque unité est un merge `--no-ff` de la tête/remise exacte listée ; l'arbre de composition est
  celui de la source (aucune réécriture).
- Recomposition sur `598deccaa` : 16 merges `--no-ff` sans conflit, **seconds parents = têtes/remises
  exactes du manifeste (16/16 vérifiés)**, aucun `.github/**` ; le seul delta de cible est #2217
  (2 fichiers), disjoint des 106 fichiers du paquet (les remises #920 et #1467 sont elles-mêmes des
  fusions propres).
- Tests ciblés sur la tête composée : `tests.test_migrations_vues`, `tests.test_migrations_additives`,
  `tests.test_l122_numeros_de_migration`, `tests.test_migrations` (#2229/#920),
  `tests.test_1969_bascule` (#2155/#2156), `tests.test_l87c_alert_ack` (#2105) =
  **124 OK psql + 124 OK psycopg**, 0 échec. Suites #1467 (train, #1680, cluster) : **160 OK psql
  (6 skipped)** ; le pilote psycopg local a été interrompu (job terminé) et reste couvert par
  l'unique CI complète de paquet.
- Après intégration de la correction #2229 `255d7bc7` : `tests.test_migrations_vues` +
  `tests.test_migrations_additives` = **5 OK psql + 5 OK psycopg** sur la tête composée.

## CI et fusion

- **Une seule** CI complète sur la tête exacte du paquet, après le contrôle unitaire du pilote ;
  les deux verdicts #2229 sont acquis et la correction est intégrée. Étiquette `ci-full-<sha>`
  posée par l'exécuteur sur le SHA exact, aucune CI de lot.
- Feu du pilote sur le SHA exact (`gh pr merge --auto --match-head-commit <sha>` ou repli
  `ameesh work merge-pr`), fusion automatique au vert ; ne pas rebaser. Toute nouvelle tête annule
  le feu.
- Après fusion : rattachement des unités au merge réel et aux runs, clôture des lots par le runner,
  confirmation au pilote et au bureau. Aucune opération hôte ici.
