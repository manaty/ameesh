# Paquet integration/paquet-h-20261017-0600 — composition

Base : `main` après atterrissage du véhicule de réparation 0500 (PR1583, #2199), merge réel
**`eadc7bdcfb5b105a4bef23a513f57b2843154615`** (17/10 07:47:40Z, arbre `982ac57c`). La branche
est construite sur `2fafa5755` (tête 0500 avant le correctif test-only), puis reçoit ce merge
réel : le diff de la PR ne porte que les unités ci-dessous (92 fichiers, +8201/−723 hors ce
manifeste).

Exécuteur : **mty-amh-impl-01** (composition, CI et fusion). Admissions du pilote et transports
consignés au registre ; aucune CI de lot, aucune fusion parallèle, 0500 non modifié.

## Composition

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #2172 | `5bde31e8f843` (`lot/2172-bail-dernier-essai`) | `08c2cedbc` | GO Codex 0B du gel `2a8b18bb` porté par patch-id identique ; enfant direct de main `18859e06` ; 2 f. +70/−8 |
| 2 | #2178 | `f4d69337bad1` (`lot/2178-l232d-hardening`) | `902931ce6` | admission 04:58, avis Claude + Codex ; 178 ciblés (3 skipped) ; 14 f. +639/−97 |
| 3 | #2156 | `0835fdeb6f6d` (`lot/2156-shebang-wrapper-pip`) | `e445dfe80` | GO Codex #9944, rebase 05:00 sur main `18859e06` ; 3 f. +71/−7 |
| 4 | #2155 | `d2edaa330842` (`lot/2155-console-scripts-venv-stable`) | `081f326fa` | GO mesh-design 04:10, admission note37901 ; 16 ciblés auteur ; 2 f. +115/−4 |
| 5 | #1465 | remise `4433e5f91` (`lot/1465-preuves-par-commit`) | `c24573c07` | migration 0445 additive, preuve dans la transaction du jalon ; 13 f. +1561/−5 ; fix test-only `fb9a2575` = `f0593b5e6` |
| 6 | #1612 | `70c9afa32073` (`lot/1612-ci-attempt-guard`) | `cf445cd63` | admission renouvelée 16/10, verdict 9266 + 71 ciblés ; 2 f. +70/−5 |
| 7 | #1343 | remise `17976b9f4` (gel `08f384f0`) | `cca6aba58` | admission 04:30, conditions levées ; union `tests.yml`/`ci.py` ; 9 f. +2928/−67 |
| 8 | #2064 | `faf6d999be14` (`agent/deepseek2-2064-ref-red`) | `0f19eaef1` | admission 05:25, GO Codex + DeepSeek sur le delta ; 4 ciblés ; 2 f. +44/−2 |
| 9 | #2158 | `d5eeb0950744` (`agent/deepseek4-2158-login-api-checklist`) | `46ffb865c` | GO Codex 05:12, docs-only ; 2 f. +9/−1 |
| 10 | #2170 | `3b74f822d2a2` (`agent/deepseek2-2170-pgpass`) | `1332147a0` | admission 04:40, deux familles ; résolution #920 `54ab9ee8` disponible ; 10 f. +544/−17 |
| 11 | #2076 | `879cef08fc9b` (`lot/2076-multiple-reviewers`, PR1492) | `6c89c9c79` | admission 05:06, GO DeepSeek deepseek3 + GO Claude orchestrateur (0 B) ; migration 0456 additive ; 15 f. +432/−46 ; rejeu ci-dessous |
| 12 | #2084 | `4813dba027ff` (`lot/2084-reviewers-journal-notes`) | `11a7b7077` | N de #2076, dépendance satisfaite ; 18/18 témoins #2076 ; 5 f. +169/−27 |
| 13 | #1114 | `7c7be26c4782` (`lot/1114-eligibilite-alertes`) | `77e125b94` | gel 9860, verdict 9876, note 37713, `freeze.causes=[]` ; 6 f. +115/−19 |
| 14 | #2097 | `951eb1ed06d8` (`agent/deepseek4-2097-flux-sweep`) | `b9c92199d` | GO Codex 05:12, admission 05:12 ; 9/9 témoins ; 3 f. +289 |
| 15 | #2125 | `a08ad864697a` (`lot/2125-lines-reverse-cost`) | `aabce200b` | GO Codex mesh-design 05:31, R1 ; 10/10 + 56/56, frontières 88/569 ; 2 f. +68/−8 |
| 16 | #2043 | `c63767e9e3e1` (`lot/2043-rebase-lot-sujet-perdu`) | `1bbfe5f7b` | gel 9957, verdict Claude 9835 sur cette tête, `freeze.causes=[]` ; 35 ciblés ; 2 f. +156/−60 |
| 17 | #1152 | résolution `b200a3903126` | `eb37395ce` | GO Codex final 05:43, 0B/0N ; parents `8fd29d790` (#1152×#2190) + `2fafa5755` ; delta #1152 identique au SHA près sur ses 7 fichiers ; branche gelée `74d8fd647f2` ; 175 ciblés auteur |
| 18 | #2034 | `51ecaabd82fb` (`lot/2034-secrets-path-guards`) | `84aa71fac` | deux avis secrets indépendants (Claude 9967 + DeepSeek 9974), admission 05:56 ; 79 ciblés auteur ; 3 f. +107/−9 |
| 19 | #2162 | `b9dcbef44634` (résolution `2e6ef81e1d05`, parents `84aa71fac` + `b9dcbef4`) | `9363dcbd` | admission pilote 06:10 ; 4 f. +104/−23 ; recette 126 OK psql/psycopg ; transport recollé sur H′ ; trailer `ameesh-work: 2162` |

Les commits de fusion portent le lot comme second parent et leur sujet nomme le lot ; les quatre
unités composées après l'entrée en vigueur de #2157 (`#2125`, `#2043`, `#1152`, `#2034`) et
l'unité 19 (`#2162`) portent en plus le trailer `ameesh-work: <lot>`. Les unités antérieures ne
l'ont pas (le second parent et le sujet font foi) et ne sont pas réécrites : le contrat #2157
exclut de refaire un paquet validé pour cette seule métadonnée.

## Reconstruction sur M′ (H′) et unité 19

- M′ vérifié à la forge : `eadc7bdcf`, parents `18859e06` + `ea9ae07`, arbre `982ac57c`
  (identique à la tête PR1583), `refs/heads/main` = M′ au relevé.
- **H′ = `a2ca3078f0c08f470931ec232f0e50cc1a600ce4`**, parents `84aa71fac` + M′, arbre
  **`110c52126`** ; delta vs `84aa71fac` = 3 fichiers, +163/−38 (manifeste 0500,
  `test_events_attach`, `test_l804`), aucun conflit. Même arbre que la répétition d'impl-04
  (`73400a1eb`).
- **Unité 19 = `9363dcbd7d0498b073d2d1600fca1fa3e1866218`**, parents H′ + `2e6ef81e1`, arbre
  **`a0363eb16`** ; delta vs `84aa71fac` = 7 fichiers, +267/−61 (M′ + #2162). Même arbre que la
  répétition d'impl-04 (`eee5df6bc`). `git diff --check` propre, aucun marqueur.
- Aucune réécriture ni rebase des unités : H′ est un merge de M′ dans la tête composée, l'unité
  19 un merge de la résolution préparée. Ne **jamais** merger `b9dcbef4` brut dans H′ : conflit
  `src/ameesh/backlog.py` (trois étages), la résolution `2e6ef81e1` est complète et suffisante.

## Résolutions de composition (preuve)

- Aucun conflit sur les 18 premières unités ; les deltas sont disjoints ou déjà résolus par les
  remises (`#1465`, `#1343`, `#1612`, `#1152`).
- `#1152` : le delta sur `deploy/mise-a-jour/vm.sh`, `poste.sh`, `staging.sh`,
  `tests/test_2152_vm_drain.py`, `tests/test_503_agent_host.py`,
  `tests/test_502_restart_lease.py`, `tests/test_1252_vm_agents_install.py` est **identique au
  SHA près** à celui de la résolution `b200a390` par rapport à `2fafa5755` (7/7 fichiers).
- `#1343` : union `tests.yml`/`ci.py` conservée (gel #1343 + main #1635/#1256/#856/#1483),
  remise auteur `17976b9f4` reprise.
- `#1465` : `main.py` union (aide provenance + route), fix test-only `fb9a2575` inclus.
- `#2125` et `#2043` : deltas identiques aux têtes gelées (`a08ad864`, `c63767e9`).
- `#2076`/`#2199` : résolution `5ffc0e6d29a3` (`refs/ameesh/resolutions/2076/2199`, parents
  `879cef08` + `96da5b62`) préparée par impl-04 ; son blob `src/ameesh/alert_briefs.py`
  `434666cc` est identique à l'union #917+#2076 déjà portée par l'unité `6c89c9c79` (le
  `84aa71fac` actuel ajoute seulement la docstring #1114, `e75c0b22`). **Non consommée** : le
  merge de M′ dans `84aa71fac` est sans conflit sur `alert_briefs.py` ; conservée comme
  préparée / non consommée, utilisable si le merge réel de M′ diffère.
- `#2187` **non retenu** : son delta (`53a8b628`) instrumente l'attente de drain (`horloge()`,
  `ATTENTE_MAX`, borne de tours) que #1152 final supprime (arrêt prouvé, `DRAIN_ATTENTE`
  interrupteur de pause immédiate, défaut 0). L'inclure reviendrait à supprimer tout son
  contenu ; contenu du lot inchangé, exclusion signalée au pilote/auditeur.

## Rejeu de #2076 (source `879cef08`, PR1492)

- Delta du lot contre sa base `dffe30eaa` : **15 fichiers, +431/−46** ; unité composée
  `6c89c9c79` contre son parent : **15 fichiers, +432/−46**. Les `--numstat` par fichier sont
  identiques pour 14/15 ; seul `src/ameesh/alert_briefs.py` gagne **une ligne** : le commentaire
  `# #2076` de l'union 3-way #917 + boucle multi-relecteurs (aucun changement de comportement).
- Pièces propres au lot dans la tête finale : migration `0456` = `236c6561` (identique à la
  source), `src/ameesh/work_reviewers.py` = `a94b15c4` (identique), `tests/test_2076…` =
  `1aa7359a` (= source `5a077343` + **68 lignes** #2084, attendu), `alert_briefs.py` =
  `e75c0b22` (union #917/#2076 + docstring #1114).
- L'unité 19 ne change que `storage/interface.py` et `storage/postgres/work.py` parmi les
  fichiers du lot, par le delta #2162 (`events_noted(..., ids=…)` / `_DirNotes`) : hunks
  additifs et disjoints de la boucle relecteurs. Aucun autre fichier de #2076 ne bouge.
- Avis conservés : GO DeepSeek deepseek3 (16/10 20:17, 0 B / 3 N) et GO Claude orchestrateur
  (17/10 07:02, 0 B) sur `879cef08` ; admission pilote 05:06 ; aucun nouveau verdict ni rejeu de
  suite. Migration 0456 additive, à appliquer avant reprise des exécuteurs en staging.

## Preuves locales (arbre composé)

- `tests.test_2096_runner_memoire` + `tests.test_2043_rebase_trace` : **37 OK**.
- `tests.test_1252_vm_agents_install` + `test_2152_vm_drain` + `test_502_restart_lease` +
  `test_503_agent_host` : **118 OK** (430 s).
- `tests.test_l232c_cablage` + `test_l232c_coffre` (#2034) : **79 OK** (10 s).
- `tests.test_2097_flux_orphelins_run` + `test_l26_exploitation` + `test_l94_underuse` +
  `test_2076_multiple_reviewers` + `test_518_dsn_argv` + `test_l161c_failures_realtime` :
  **215 OK** (350 s).
- `tests.test_l2170_pgpass_banc` + `test_1270_rate_limit_retry` + `test_1969_bascule` +
  `test_1465_provenance` + `test_cluster` + `test_l161g_ci_gel` + `test_l161f_plan` +
  `test_1410_paquet_ci_unique` + `test_l161a_ci_sans_code` + `test_705_reviewer_designated` :
  **455 OK** (2 skipped, 415 s).
- Total ciblé sur l'arbre composé : **904 OK** (2 skipped). Recette #2162 sur la résolution
  `2e6ef81e1` : **126 OK** psql/psycopg (auteur + dev5). #2199 sur l'arbre de réparation :
  **51 OK psql + 51 OK psycopg** (auteur et transporteur).
- `bash -n` sur `deploy/mise-a-jour/{vm,poste,staging}.sh` ; `git diff --check` propre.
- Aucune CI de lot, aucune suite complète locale : une seule CI complète tourne sur la tête de
  la PR du paquet (§12).

## CI et fusion

- Une seule CI complète sur la tête gelée de la branche (PR, suite psql+psycopg) ; aucune CI de
  lot, aucune relance.
- Feu du pilote sur le SHA exact de la tête (`--match-head-commit`) ; fusion automatique au vert,
  ne pas rebaser. Toute nouvelle tête annule le feu.
- Après fusion : rattachement des 19 lots au merge réel et aux runs, confirmation au pilote et
  aux porteurs ; activation staging de #2155 et #2190 coordonnée par bureau, migration 0456
  avant reprise des exécuteurs. Aucune opération hôte ici.

## Hors périmètre

- **#2187** : exclu, cause ci-dessus (attente de drain supprimée par #1152).
- **#2186** : dépend de #2043 (désormais dans 0600) ; non attribué, à ordonner au véhicule
  suivant s'il est transporté.
- **#2199** : correctif d'isolation CI du paquet 0500, porté par le véhicule de réparation 0500,
  **hors 0600** (le merge réel `M′` l'apporte par la base).
- **#2008** : `bd6931cb4a7b` (GO8962) ; merge-tree rc 0 contre la tête 0600 (aucun conflit),
  mais aucune admission à ce véhicule gelé — à rattacher au véhicule suivant.
- **#377** : transport `ba7dcae45` (source `7f279512`) ; merge-tree contre la tête 0600 = **57
  conflits** (`.github/workflows/tests.yml`, `deploy/mise-a-jour/*`, `deploy/sql/*`, docs,
  `src/ameesh/{runner,ci,main,mesh_cli,cost,backlog,storage/…}`), aucune résolution mécanique —
  reporté, admission et véhicule dédié à désigner.
- **#800** : porté en qa, non admis à ce paquet.
- **#2184** : second avis orchestrateur encore dû.
- **#2098** (dev3, admission pilote) : véhicule à préciser au prochain montage compatible.
- Ancienne résolution #1152 `89e24186b` (base `8ca9`) : périmée, non utilisée.
