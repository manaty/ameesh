# Paquet integration/paquet-h-20261017-1800 — composition

Base : `main` **`98ef31e6fecf`** (fusion de #2283/PR #1663 à 17:26Z ; arbre = `996f718` + le
classement `review_catch_up` dans `FERMEES`). La branche est composée directement sur cette base ;
le diff de la PR ne porte que les 4 unités ci-dessous et ce manifeste. Tête de composition :
**`07227dd45e2a`** (4 unités) ; la tête de branche au gel, qui porte ce manifeste, est le SHA exact
communiqué au pilote et couvert par l'unique suite complète du paquet.

Demandes : **pilote mesh-design** 17/10 16:14Z (mail) : composer et ouvrir le porteur avec #1768,
#2233 et #2230 sur `main 996f718`, une fois la suite main `39047313061` verte ; **pas de feu sur
`df995cb6` seul** ; le feu ne sera donné que sur une tête portant un vrai check `tests` en success
(`ci-full` ou équivalent), le feu comptant aujourd'hui `skipped`/absent comme vert (#2271) ;
donner le SHA composé. **Pilote, 17/10 17:26Z (mail)** : #2283 fusionné (`a5148d62`, PR #1663) ;
ne pas attendre son réveil : quand la suite main post-#2283 finit, composer sur la tête main exacte
si elle est verte ; pour la suite complète, garder le plan de poussée de la branche d'intégration,
le portier lance l'unique suite ; **pas d'étiquette `ci-full` à la main** tant que ce chemin marche.
**Pilote, 17/10 17:37Z (route portée par mty-amh-impl-05)** : #2281 part au **premier paquet
horaire suivant** dès que les deux avis requis sont `ok` sur sa tête gelée ; aucune CI de lot,
aucun feu individuel. **mty-amh-impl-05, 17/10 17:50Z (agent-mail #100764)** : les deux avis sont
`ok` sur `5042113a818f` ; la tête a été gelée à 17:36Z après correction B1/N1, sans modification
depuis.

Le paquet h-1700 préparé sur la même base (`integration/paquet-h-20261017-1700`, tête `cb21f0d80`)
n'avait pas été poussé : il attendait le vert de la suite main post-#2283. #2281 étant routé vers le
premier paquet horaire suivant, les trois unités déjà composées et #2281 sont réunies dans ce
véhicule h-1800, composé sur la même base exacte. Aucun lot de h-1700 n'est perdu ni recomposé sur
une autre base.

Exécuteur : **mty-amh-impl-01** (composition, ouverture de la PR, unique CI de paquet, fusion).
Aucune CI de lot, aucun push de branche de lot, aucune fusion parallèle.

## Admissions (4 lots attendus du véhicule)

- #1768
- #2233
- #2230
- #2281

## Composition (4 unités, 4 lots)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #1768 | `28955bba239c` (PR #1304, `lot/1768-required-lot-statuses`) | `c3e2e0c8a` | **deux avis** `ok` sur `28955bba` : mty-amh-impl-07 (DeepSeek) 15:54Z + ameesh-cx1 (Codex) 15:40Z, auteur Claude ameesh-dev7 ; classe `sensitive` (porte de fusion, `merge_queue.py`/`land.py`) ; 8 f. +303/−27 ; merge-tree rc=0 sur `98ef31e6f` (arbre `885a5394`) |
| 2 | #2233 | `528e73296d61` (PR #1637, `lot/2233-underuse-long-window`) | `744ed3eb7` | verdict `ok` mty-amh-qa-01 (DeepSeek) 15:03Z sur `528e7329`, auteur Codex ameesh-cx1 ; classe `light` ; 3 f. +141/−24 ; merge-tree rc=0 sur `98ef31e6f` (arbre `71c88b86`) |
| 3 | #2230 | `df995cb6a237` (`lot/2230-activation-lanceurs-robuste`, sans PR) | `39c22a7dd` | **deux avis** prod `ok` sur `df995cb6` : Claude ameesh-dev7 15:33Z (jalon 10459) + Codex ameesh-cx2 15:09Z (jalon 10434), auteur DeepSeek deepseek5 ; classe `sensitive` (`deploy/mise-a-jour/bascule.py`) ; 3 f. +112/−28 ; merge-tree rc=0 sur `98ef31e6f` (arbre `dc450bb5`) |
| 4 | #2281 | `5042113a818f` (PR #1665, `lot/2281-no-verdict-gate`) | `07227dd45` | **deux avis** `ok` sur `5042113a` : ameesh-cx1 (Codex R2) 17:45Z + ameesh-dev1 (Claude GO) 17:48Z, auteur DeepSeek mty-amh-impl-05 ; classe `sensitive` (porte de file, `merge_queue.py`) ; 3 f. +221/−15 ; merge-tree rc=0 sur `98ef31e6f` (arbre `abafbc80`) et sur la tête à 3 unités (`cb21f0d80`, arbre `b2514f59`) |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot et il porte le
trailer `ameesh-work: #<lot>`. Aucun contenu nouveau : chaque unité est la tête exacte listée
ci-dessus ; aucune résolution n'a été nécessaire (les quatre têtes se composent sans conflit sur
`98ef31e6f`). Total : **16 fichiers, +777/−94**, aucun `.github/**`, aucune migration.

## Base, suite main et retenue de #1280

La suite main post-#2283 (run **`39053653121`**, `push` sur `98ef31e6f`, démarrée 17:24:58Z) est
**verte** (`success`, 18:00:05Z), conformément à la retenue du pilote (17:26Z) : le paquet part sur
cette base exacte.

**#1280 est tenu hors du paquet** : lot SQL (deux avis requis au §2), un seul avis acquis
(ameesh-dev4/Claude `ok` sur `c597f86e`), l'avis Codex `ameesh-cx3` encore dû sur cette tête
(l'avis cx3 antérieur portait sur `ea274e8a`, abandonnée). Consigne de mty-amh-impl-07 du 17/10
17:52Z : aucun paquet, aucune CI de lot ni feu avant ce second avis. #1280 n'est donc ni composé ni
cité dans ce véhicule.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #1280 | lot SQL : second avis Codex (`ameesh-cx3`) dû sur la tête gelée `c597f86e` ; retenue explicite d'impl-07 (17:52Z) |
| #2122 | non admis par le pilote (16:14Z) : source du §11 bis attendue ; un seul avis Codex sur `b3d41ae2`, classe prod |
| autres lots `qa` | hors périmètre des admissions du pilote (16:14Z et route 17:37Z) ; candidats aux paquets suivants |

## Preuves

- `git diff --check` propre, aucun marqueur de conflit.
- Chaque unité est un merge `--no-ff` de la tête exacte listée ; seconds parents conformes (4/4) et
  trailers `ameesh-work` présents (4/4).
- `git merge-tree` de chaque tête sur `98ef31e6f` : rc=0 (arbres `885a5394`, `71c88b86`,
  `dc450bb5`, `abafbc80`).
- Tests ciblés sur la tête composée `07227dd45` : `tests.test_1748_required_statuses`,
  `tests.test_2233_underuse_limits`, `tests.test_1969_bascule`, `tests.test_1374_fire`,
  `tests.test_l160e_merge_queue` = **160 OK psql (56,3 s) + 160 OK psycopg (61,1 s)**.
- `scripts/paquet-inclusion.py --manifest reviews/integration-paquet-h-20261017-1800.md
  --head 07227dd45 --expected 1768,2233,2230,2281` : **aucun défaut** ; 4 compositions, 4 lots
  `présent`, 0 partiel, 0 absent.
- Aucun `.github/**`, aucune migration ; la seule modification de porte de fusion est #1768
  (précisément relue par deux familles) et celle de #2281 (porte de file, deux familles
  Codex + Claude, auteur DeepSeek).

## CI et fusion

- **Une seule** suite complète sur la tête exacte du paquet : poussée de
  `integration/paquet-h-20261017-1800` **sans PR ouverte** ; le portier `paquet.yml` constate
  qu'aucune PR ne couvre la tête et appelle `tests.yml` en `workflow_call` — la suite complète
  tourne sur cette tête et y porte le check `tests`. La PR n'est ouverte qu'après la décision du
  portier. Repli si le portier voit une PR (ce qui ne doit pas arriver) : étiquette
  `ci-full-<sha>` sur la PR, jamais à la main sans l'accord du pilote (17:26Z).
- Le check requis `tests` doit être `success` avec des tests réellement exécutés (pas
  `skipped`/absent, #2271). Aucune CI de lot.
- Feu du pilote sur ce SHA après `tests` vert (`gh pr merge --auto --match-head-commit <sha>` ou
  repli `ameesh work merge-pr`). Ne pas rebaser ; toute nouvelle tête annule le feu.
- Après fusion : rattachement des unités au merge réel et aux runs, clôture des lots par le
  runner, confirmation au pilote et au bureau. Aucune opération hôte ici.
