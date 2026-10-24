# Paquet integration/paquet-h-20261024-12 — composition (§12)

Base : `main` **`ef32b515ce5193785a524ed1d60348570c1e8aed`** (fusion du paquet
h-11, PR #2583, 24/10 22:06Z).
Branche `integration/paquet-h-20261024-12`, cible `main`. Porteur : **#3414**
(créé par mesh-design, fil du lot, 24/10 22:20Z).
Composeur : mty-amh-impl-01. Une seule suite complète est demandée sur la tête
finale (CI de la PR).

## Sources

- mesh-design, message **#134915** (24/10 22:20Z) : h-12 sur `main`
  `ef32b515c` (h-11 fusionné à 22:06Z) ; selon `scripts/paquet-candidats.py`
  6 lots prêts et rien d'autre en attente utile : #3397 (`0a0be59f5b80`,
  sensible, migration 0481 : à mettre au manifeste et au déploiement), #3403
  (`f7f328f36a32`), #3406 (`f7c2e7722301`), #3408 (`c8e0d26f773b`), #3409
  (`63062c8fbd93`), #3411 (`e2ee21d844f0`). #3399 n'entre que si l'avis de cx1
  est acquis avant le gel ; #3385 et #3388 restent dehors (hold pilote, lots
  canon). « Compose integration/paquet-h-20261024-12 avec paquet-compose.py,
  gèle, lance la CI complète, auto-merge au vert. Annonce-moi le gel : je
  vérifie les fusions contre le merge-tree et je pose « verdict not
  required » ».
- mesh-design, accusé de réception (24/10 22:26Z), complément : « au gel,
  prends tout ce que paquet-candidats.py donne prêt à ce moment-là. #3399 et
  #3401 (impl-03, gel #18160) peuvent s'ajouter aux 6 si leurs avis sont
  acquis ; sinon, ils attendent h-13. »
- Calcul `scripts/paquet-candidats.py --repo . --base ef32b515c`
  (24/10 22:2xZ, code de `main`, venv du projet) : **7 lots prêts**, 5 en
  attente, 0 erreur. Prêts : #3397, #3403, #3406, #3408, #3409, #3411,
  **#3401**.
  - #3401 n'est pas cité par le courrier du pilote (22:20Z) : il est passé en
    `qa` à 22:19Z et ses deux avis sont arrivés à 22:24Z (cx2 #18163,
    auditeur #18164). La liste calculée est la liste de départ (#2404) : ce
    lot prêt est donc **composé**.
- **Recomposition** (24/10 22:47Z) : le calcul refait sur la même base donne
  **8 lots prêts** — **#3412** est devenu prêt à 22:31Z, par les avis de
  l'auditeur (#18168, 22:30:46Z) et de pilot-resident-3389 (#18169,
  22:31:17Z), tous deux acquis **avant le gel de 22:35Z** ; l'avis cx1 était
  déjà là (#18162, 22:21Z). Conformément au complément du pilote, #3412 est
  donc ajouté au paquet, et la première tête (`0d01403546a8`, 7 unités) est
  remplacée : le run `40139339139` est annulé, la tête est regelée et une
  nouvelle `ci-full` est demandée.
  - #3399 reste hors paquet : `blocked-verdict` sur le gel effectif
    `0ef61be0204b` (l'avis cx1 n'est pas acquis).
  - #3414 est le véhicule lui-même : le calcul le refuse comme unité
    (`packet-vehicle`), il n'entre jamais dans sa propre composition.
- #3397 : retiré de h-11 après la CI rouge de `bb6bde211` (B1 #2928 : le
  retour anticipé de `review_gate` perdait le diagnostic « without an exact
  provenance receipt »). Corrigé depuis (`0a0be59f5b80`, gel #18115, avis
  2/2 : cx1 #18117, auditeur #18119, aucun bloquant). Son delta porte la
  migration **additive 0481** `turn_costs_agent_recorded` (index
  `(agent, recorded_at desc, id desc)`, `create index if not exists`, aucun
  retrait ni donnée modifiée) : à appliquer en staging **avant** de relancer
  les exécuteurs (§11 bis/§14).
- #3412 : priorité 1 (déploiement des VM). Le redémarrage d'agents-2 de
  staging 1.21.0 (21:10–21:19Z) s'est arrêté sur un bail de rôle élu repris
  par l'autre hôte candidat, laissant 17 runners sur l'ancien code. Le
  correctif traite ce cas comme normal et ne coupe plus le balayage.

## Admissions (8 lots, 8 unités)

| lot | source composée | classe | avis (gate) | objet |
|---|---|---|---|---|
| #3397 | `0a0be59f5b80` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 18117 ; auditeur (claude), verdict 18119 | UI `/ui/` : `/api/v1/alerts` — porte d'avis sans Git, index du dernier tour, instantané partagé ; migration additive 0481 |
| #3403 | `f7f328f36a32` | sensitive | 1/1 : ameesh-cx2 (codex), verdict 18136 | Ménage : le détail « processes inside (pid cmd) » n'écrit plus l'argv au journal partagé |
| #3406 | `f7c2e7722301` | light | 1/1 : auditeur (claude), verdict 18107 | packet-watch : l'alerte « créneau de paquet manqué » reconnaît un lot de composition ouvert, et un seul composeur par créneau |
| #3408 | `c8e0d26f773b` | light | 1/1 : ameesh-cx1 (codex), verdict 18116 | Watch : un `ameesh-watch-<agent>.env` épinglé à un autre hôte ne produit aucun envoi |
| #3409 | `63062c8fbd93` | light | 1/1 : ameesh-dev3 (deepseek), verdict 18141 | Débit : un lot rouvert puis retenu (`blocked`, `waiting_human`) reste ouvert |
| #3411 | `e2ee21d844f0` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 18156 ; auditeur (claude), verdict 18158 | Désignation : un agent éphémère n'est ni relecteur désigné ni preneur automatique |
| #3401 | `a8b73841e1d3` | sensitive | 2/2 : ameesh-cx2 (codex), verdict 18163 ; auditeur (claude), verdict 18164 | Chronologie : la coupure `MAX_LOTS` est dite, plus de liste qui semble complète |
| #3412 | `a3decdb4bfe6` | sensitive | 2/2 : ameesh-cx1 (codex), verdict 18162 ; auditeur (claude), verdict 18168 (+ pilot-resident-3389, claude, 18169) | `vm.sh` : un bail de rôle élu repris par un autre hôte candidat n'arrête plus le balayage ; les runners suivants ne restent plus sur l'ancien code |

Les têtes composées sont les têtes **publiées** lues par le calcul (gel
effectif = tête publiée, verdicts acquis sur cette tête). #3397 est la
priorité du pilote (correctif de la lenteur `/api/v1/alerts`, 93–102 s) et
porte la seule migration du paquet ; #3401 est la suite du chantier Chronologie
(#3395, déjà dans `main` avec h-11) ; #3412 est la priorité 1 du déploiement
staging (élection de rôle, runners bloqués sur l'ancien code).

## Composition (8 unités, 8 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3397 | `0a0be59f5b80` | `419b4137c29c` | 0 conflit |
| 2 | #3403 | `f7f328f36a32` | `89f492927301` | 0 conflit |
| 3 | #3406 | `f7c2e7722301` | `62d620cacc63` | 0 conflit |
| 4 | #3408 | `c8e0d26f773b` | `36d528d8bbf7` | 0 conflit |
| 5 | #3409 | `63062c8fbd93` | `0412ba401bbe` | 0 conflit |
| 6 | #3411 | `e2ee21d844f0` | `6f4481e048e3` | 0 conflit |
| 7 | #3401 | `a8b73841e1d3` | `8c75672cdd5d` | 0 conflit |
| 8 | #3412 | `a3decdb4bfe6` | `09831527caef` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et sa tête (`vehicule 1200 : #<lot> <tête>`), et il porte un trailer
`ameesh-work:`. La base `ef32b515ce5193785a524ed1d60348570c1e8aed` n'a pas
bougé pendant la composition.

## Contrôles

- **Liste calculée** : 8 prêts au recalcul de 22:47Z, **8/8 composés**, 0
  écarté, 4 en attente (motifs ci-dessous), 0 erreur. Le véhicule #3414 est
  exclu du calcul comme unité (`packet-vehicle`).
- **Conflits** : 8/8 unités fusionnées sans intervention ; chaque arbre de
  composition est identique à `git merge-tree --write-tree` de ses parents ;
  aucun marqueur de conflit, aucun arbitrage de code ; `git diff --check`
  vert.
- **Tests ciblés de l'arbre intégré** : **262/262 verts** en Python
  (`test_3397_alerts_lenteur`, `test_2743_housekeeping_clones`,
  `test_3396_housekeeping_unreadable`, `test_2601_packet_watch` : 62 ;
  `test_3408_watch_foreign_pin`, `test_3292_debit`,
  `test_3411_ephemeres_hors_designation`, `test_progress`, `test_l84_serve`,
  `test_2928_verdict_provenance` : 136 ; `test_503_agent_host` : 64).
  `bash -n deploy/mise-a-jour/vm.sh` vert. UI : **78/78** verts
  (`ui/test/*.test.mjs`) et `tsc --noEmit` vert.
- **Compilation** : `compileall src tests` verte.
- **Contrôles statiques** : 6/6 verts (`scripts/freeze-checks.py`).
- **Prévalidation** : indisponible au gel (`card-stale`, carte de 174 h) —
  non bloquante (§3), motif journalisé (`prevalidation-unavailable`) ; cité
  ici comme demandé par la demande de CI.
- **Diff** : 34 fichiers, +976/−66 (manifeste non compris) ; les 8 commits de
  composition portent leur trailer `ameesh-work:`.
- **Migrations** : une seule, **0481 additive** (#3397) ; aucun retrait
  d'index, aucune donnée modifiée, `lock_timeout` 5 s. Au déploiement :
  appliquer 0481 **avant** de relancer les exécuteurs (§11 bis/§14).
- **Inclusion** : `scripts/paquet-inclusion.py` rc 0 sur la tête (attendus =
  les 8 composés ; prêts = les 8 du recalcul de 22:47Z ; #3414 véhicule
  exclu).
- **Suite complète** : une seule `ci-full` demandée sur la tête finale, en
  mode paquet ; les verdicts unitaires couvrent les contenus, le paquet est
  relu sur sa tête par les familles du porteur (§12). Le run
  `40139339139` de la première tête (7 unités) est annulé.
- **Fusion** : sous garde de tête (`--match-head-commit`), dès le vert et les
  avis du porteur ; landings `ameesh work landing <unité> --vehicle 3414`
  déclarés au gel ; staging = bureau.

## En attente (calcul, hors paquet)

| lot | motif |
|---|---|
| #3186 | `blocked-verdict` sur le gel effectif `2403f00f4885` : conflit `src/ameesh/release_cutoff.py` avec main et dernier avis bloquant |
| #3385 | hold pilote (lot canon) ; branche `canon/3385-roadmap-24-10` non publiée sur origin : une tête locale n'est pas la forge |
| #3388 | hold pilote (lot canon) ; branche `lot/3388-renommage` non publiée sur origin |
| #3399 | `blocked-verdict` sur le gel effectif `0ef61be0204b` (classe sensitive) : l'avis cx1 n'est pas acquis |

## Écartés (ce véhicule)

Aucun lot prêt du calcul n'est écarté : les 8 prêts sont composés.
