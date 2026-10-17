# Paquet integration/paquet-h-20261017-0600 — composition

Base : `main` après atterrissage du véhicule de réparation 0500 (PR1583 + #2199), merge réel
**`eadc7bdcfb5b105a4bef23a513f57b2843154615`** (17/10 07:47:40Z, arbre `982ac57c`). La branche
est construite sur `2fafa5755` (tête 0500 avant le correctif test-only `c8d960af`) puis reçoit ce
merge réel : le diff de la PR ne porte que les unités ci-dessous (hors ce manifeste et le registre
`paquets/retires.tsv`).

**Recomposition du 17/10, en deux temps :**

- **Décision de l'owner du 17/10 ~10:35**, relayée par le bureau (10:54/10:55) : **#2076 et #2084
  sont retirés** (deux familles de relecture indisponibles pour #2076 ; aucune dérogation). Le
  correctif SQL #2209 (`3afe55f7a`, `src/ameesh/storage/postgres/work.py:1600`) n'est pas reporté :
  sans le `to_jsonb(work_items)->'reviewers'` de #2076, l'alias `w` de #1425 ne masque plus la table
  et `item_columns(self.db)` reste correct.
- **Contrôle des avis du pilote du 17/10 ~12:00** (bureau) : **#2155 et #2156 sont retirés à leur
  tour**. Ils touchent `deploy/mise-a-jour/bascule.py` (chemin prod) et n'ont qu'une seule famille
  de relecture hors auteur (Codex) au lieu de deux ; aucune dérogation. La branche est reconstruite
  depuis l'unité 2 ; les unités 3 à 15 sont re-mergées sans ces deux lots. **#1152**, qui touche
  aussi `bascule.py` dans sa source, suit : sa résolution est mécanique (§11 bis) et ne demande pas
  de nouvel avis ; `bascule.py` reste exactement la version d'avant #2155/#2156 (blob `5ffdb35d6`).

Exécuteur : **mty-amh-impl-01** (composition, CI et fusion). Admissions du pilote et transports
consignés au registre ; aucune CI de lot, aucune fusion parallèle, 0500 non modifié.

## Composition (15 unités)

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #2172 | `5bde31e8f843` (`lot/2172-bail-dernier-essai`) | `08c2cedbc` | GO Codex 0B du gel `2a8b18bb` porté par patch-id identique ; enfant direct de main `18859e06` ; 2 f. +70/−8 |
| 2 | #2178 | `f4d69337bad1` (`lot/2178-l232d-hardening`) | `902931ce6` | admission 04:58, avis Claude + Codex ; 178 ciblés (3 skipped) ; 14 f. +639/−97 |
| 3 | #1465 | remise `4433e5f91` (`lot/1465-preuves-par-commit`) | `81cfdce0e` | migration 0445 additive, preuve dans la transaction du jalon ; 13 f. +1561/−5 ; fix test-only `fb9a2575` = `f0593b5e6`, rejoué en `7073b4025` (même patch) |
| 4 | #1612 | `70c9afa32073` (`lot/1612-ci-attempt-guard`) | `61a34c249` | admission renouvelée 16/10, verdict 9266 + 71 ciblés ; 2 f. +70/−5 |
| 5 | #1343 | remise `17976b9f4` (gel `08f384f0`) | `93ec69270` | admission 04:30, conditions levées ; union `tests.yml`/`ci.py` ; 9 f. +2928/−67 |
| 6 | #2064 | `faf6d999be14` (`agent/deepseek2-2064-ref-red`) | `bf744a710` | admission 05:25, GO Codex + DeepSeek sur le delta ; 4 ciblés ; 2 f. +44/−2 |
| 7 | #2158 | `d5eeb0950744` (`agent/deepseek4-2158-login-api-checklist`) | `609782418` | GO Codex 05:12, docs-only ; 2 f. +9/−1 |
| 8 | #2170 | `3b74f822d2a2` (`agent/deepseek2-2170-pgpass`) | `7ced42b3f` | admission 04:40, deux familles ; résolution #920 `54ab9ee8` disponible ; 10 f. +544/−17 |
| 9 | #1114 | `7c7be26c4782` (`lot/1114-eligibilite-alertes`) | `1b625be52` | gel 9860, verdict 9876, note 37713, `freeze.causes=[]` ; 6 f. +115/−19 ; sans #2076 (la boucle multi-relecteurs de `alert_briefs.py` disparaît, le reste de l'union #1114 × #917 est conservé) |
| 10 | #2097 | `951eb1ed06d8` (`agent/deepseek4-2097-flux-sweep`) | `40da8ac43` | GO Codex 05:12, admission 05:12 ; 9/9 témoins ; 3 f. +289 |
| 11 | #2125 | `a08ad864697a` (`lot/2125-lines-reverse-cost`) | `36dc90f4e` | GO Codex mesh-design 05:31, R1 ; 10/10 + 56/56, frontières 88/569 ; 2 f. +68/−8 |
| 12 | #2043 | `c63767e9e3e1` (`lot/2043-rebase-lot-sujet-perdu`) | `419990e65` | gel 9957, verdict Claude 9835 sur cette tête, `freeze.causes=[]` ; 35 ciblés ; 2 f. +156/−60 |
| 13 | #1152 | résolution `b200a3903126` | `536003982` | GO Codex final 05:43, 0B/0N ; parents `8fd29d790` (#1152×#2190) + `2fafa5755` ; delta #1152 identique au SHA près sur ses 7 fichiers ; branche gelée `74d8fd647f2` ; 175 ciblés auteur ; résolution mécanique sans #2155/#2156 (§11 bis), `bascule.py` inchangé depuis l'unité 2 |
| 14 | #2034 | `51ecaabd82fb` (`lot/2034-secrets-path-guards`) | `d3b271d7f` | deux avis secrets indépendants (Claude 9967 + DeepSeek 9974), admission 05:56 ; 79 ciblés auteur ; 3 f. +107/−9 |
| 15 | #2162 | `b9dcbef44634` (résolution reconstruite sur H′) | `dce18a9f7` | admission pilote 06:10 ; 4 f. +104/−23 ; recette 126 OK psql/psycopg ; conflit `backlog.py` résolu comme `2e6ef81e1` (blob identique), `storage/interface.py` et `storage/postgres/work.py` identiques à l'ancienne résolution ; trailer `ameesh-work: 2162` |

Les commits de fusion portent le lot comme second parent et leur sujet nomme le lot ; les unités
composées après l'entrée en vigueur de #2157 (`#2125`, `#2043`, `#1152`, `#2034`) et l'unité 15
(`#2162`) portent en plus le trailer `ameesh-work: <lot>`. Les unités antérieures ne l'ont pas (le
second parent et le sujet font foi) et ne sont pas réécrites.

## Retraits

| lot | tête retirée | motif |
|---|---|---|
| #2076 | `879cef08fc9b` (`lot/2076-multiple-reviewers`, PR1492) | décision de l'owner du 17/10 ~10:35 relayée par le bureau ; deux familles de relecture indisponibles pour #2076 (comptes Codex plafonnés jusqu'au 22/10) ; aucune dérogation à la règle des deux familles. Retour dans un paquet ultérieur avec sa correction, relue par deux familles distinctes de l'auteur. |
| #2084 | `4813dba027ff` (`lot/2084-reviewers-journal-notes`) | suite de relecture de #2076 (titre « #2076 : N du relecteur DeepSeek… »), il en dépend ; retiré avec lui. |
| #2155 | `d2edaa330842` (`lot/2155-console-scripts-venv-stable`) | chemin prod (`deploy/mise-a-jour/bascule.py`), une seule famille de relecture hors auteur (Codex) au lieu de deux ; décision du pilote du 17/10 ~12:00 (bureau), aucune dérogation. |
| #2156 | `0835fdeb6f6d` (`lot/2156-shebang-wrapper-pip`) | même motif : `deploy/mise-a-jour/bascule.py` (prod), seconde famille manquante ; décision du pilote du 17/10 ~12:00 (bureau). |

Les quatre lignes sont inscrites dans `paquets/retires.tsv`. Le correctif SQL #2209 (`3afe55f7a`)
est abandonné, pas reporté ; la migration `0456` de #2076 n'est plus dans la tête et n'est donc plus
à appliquer. #2155 et #2156 pourront revenir dans un paquet ultérieur avec leur seconde famille.

## Recomposition : preuves

- Aucun contenu nouveau : les unités 3 à 15 sont re-mergées à **delta constant**. Le `patch-id
  --stable` du diff d'unité (premier parent → commit de composition) est **identique** à celui de
  l'ancienne composition pour les 13 unités re-mergées, y compris #1152 et #2162.
- Les conflits de rejeu (#1465 `main.py`, #1612 `test_l161c_failures_realtime.py`, #1343
  `tests.yml`/`ci.py`, #2162 `backlog.py`) ont été résolus par les résolutions déjà consignées ;
  les blobs obtenus sont identiques aux unités d'origine. Le fix test-only de #1465 est rejoué en
  `7073b4025` (même patch que `f0593b5e6`).
- **H′ = `782e9edfcee390a93cd97044de481168427c4069`**, parents `d3b271d7f` + M′, arbre
  **`8b1af737f`** ; delta vs `d3b271d7f` = 3 fichiers, +163/−38 (manifeste 0500,
  `test_events_attach`, `test_l804`).
- **Unité 15 = `dce18a9f7628a9405fc1499c9a1fd776e99c48a0`**, parents H′ + `b9dcbef44634`, arbre
  **`85e1a6bff`** ; delta vs H′ = 4 fichiers, +104/−23.
- Retrait #2155/#2156 vérifié à la source : `deploy/mise-a-jour/bascule.py` = `5ffdb35d6` et
  `tests/test_1969_bascule.py` = `89b3ee755`, blobs de l'unité 2 ; la régression #1969 n'est plus
  dans le paquet.
- **#2184** n'est pas dans le paquet (second avis orchestrateur encore dû) : sa résolution avec
  #2076 (`53185066f2ee`) ne s'applique pas et n'a pas été consommée.
- `git diff --check` propre, aucun marqueur.

## Preuves locales (arbre recomposé)

- `tests.test_l94_underuse` + `tests.test_l26_exploitation` (#1114) : **78 OK**.
- `tests.test_1425_stop_sweep_suites` + `tests.test_705_reviewer_designated` +
  `tests.test_811_lot_workdirs` (#2162) + `tests.test_1677_prise_empilee` : **115 OK**.
- `tests.test_1969_bascule` (version d'avant #2155/#2156) : **15 OK**.
- `tests.test_1252_vm_agents_install` + `tests.test_2152_vm_drain` +
  `tests.test_502_restart_lease` + `tests.test_503_agent_host` (#1152) : **118 OK**.
- Total rejoué sur la tête recomposée : **326 OK**. Aucune suite complète locale : une seule CI
  complète tourne sur la tête de la PR du paquet (§12), demandée par l'exécuteur sur cette tête
  gelée.

## CI et fusion

- Une seule CI complète sur la tête gelée de la branche (PR, suite psql+psycopg) ; aucune CI de
  lot, aucune relance. L'étiquette `ci-full-<sha>` est posée par l'exécuteur sur le SHA exact.
- Feu du pilote sur le SHA exact de la tête (`--auto --match-head-commit`) ; fusion automatique au
  vert, ne pas rebaser. Toute nouvelle tête annule le feu.
- Après fusion : rattachement des 15 lots au merge réel et aux runs, confirmation au pilote et aux
  porteurs ; activation staging de #2190 coordonnée par bureau. Aucune opération hôte ici.

## Hors périmètre

- **#2076**, **#2084**, **#2155**, **#2156** : retirés, voir la section Retraits.
- **#2187** : exclu, son delta (`53a8b628`) instrumente l'attente de drain (`horloge()`,
  `ATTENTE_MAX`, borne de tours) que #1152 final supprime (arrêt prouvé, `DRAIN_ATTENTE`
  interrupteur de pause immédiate, défaut 0). L'inclure reviendrait à supprimer tout son contenu.
- **#2186** : dépend de #2043 (dans 0600) ; non attribué, à ordonner au véhicule suivant s'il est
  transporté.
- **#2199** : correctif d'isolation CI du paquet 0500, porté par le véhicule de réparation 0500,
  **hors 0600** (le merge réel `M′` l'apporte par la base).
- **#2008** : `bd6931cb4a7b` (GO8962) ; merge-tree rc 0 contre la tête 0600, aucune admission à ce
  véhicule gelé — à rattacher au véhicule suivant.
- **#377** : transport `ba7dcae45` (source `7f279512`) ; merge-tree contre la tête 0600 = 57
  conflits, aucune résolution mécanique — reporté, admission et véhicule dédié à désigner.
- **#800** : porté en qa, non admis à ce paquet.
- **#2098** : admission pilote, véhicule à préciser au prochain montage compatible.
- Ancienne résolution #1152 `89e24186b` (base `8ca9`) : périmée, non utilisée.
