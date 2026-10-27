# Paquet integration/paquet-h-20261027-33 — composition (§12), sur main issu de h-32

Base : `main` **`4df372403b68addb1c62db353f01a165a8766a9e`** (fusion du paquet
h-32, PR #2923, tête `ee27710ac`, sur la release 1.24.0 `04cf3474`).
Branche `integration/paquet-h-20261027-33`, cible `main`. Porteur : **#3840**.
Composeur : mty-amh-impl-01 (deepseek, agents-1).

Déclencheur : lot #3840 (mesh-design, 27/10 00:37Z) ; demande du pilote
(mesh-design, 27/10 01:23Z) d'ajouter **#3837** à la composition tant qu'elle
n'est pas gelée. Une première composition à 12 unités a été gelée à
`584d9efe9de7` et a passé une ci-full (run 40436505390). Le pilote a ensuite
**retiré #3837** (27/10 02:30Z) après deux tests négatifs déterministes de la
porte ; la composition finale porte **11 lots / 11 unités**. Voir « Retrait de
#3837 ». Méthode de h-32 : liste calculée `scripts/paquet-candidats.py` depuis
la tête exacte de `main`, tests locaux ciblés **avant** le gel, manifeste,
contrôle d'inclusion, `freeze-checks`, puis une seule `ci run --full --avis 0`
sur la tête gelée.

Les **9 lots prêts** de la liste calculée sur `4df372403b68` (#3813, #3822,
#3832, #3833, #3835, #3836, #3838, #3842, #3844) sont tous composés. La liste
signale #3829 et #3834 « conflit avec main » (pile #3829/#3834 sur `canon.py`) :
ils sont composés avec la résolution #3829 × #3777 de h-32 (unité 4), et non
omis. **#3837 est écarté sur décision du pilote**. #3506 reste en erreur de
porte (voir « Écartés »).

## Admissions (11 lots, 11 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3813 | `e563ff25600a` | gel #20929 : #20934 ameesh-cx1 ok, #20935 mesh-design ok | paquet-candidats : une porte illisible n'invalide plus la liste ; inclusion refuse une unité en erreur |
| #3822 | `36c7baaf4647` | gel #20939 : #20940 mesh-design ok | test instable 1772 : un bac par test, clones neufs, skip motivé sans clone promisor |
| #3832 | `b5024890d926` | gel #21032 : #21038 auditeur ok | échéances sous `--parallel` : témoins élargis, compteur de connexions par `/proc`, join l72 à 30 s |
| #3829 | `815b77b486b7` | gel #20972 : #20973 auditeur ok | `canon._git` : cache lié à l'instance du dépôt (clone/init vident la destination) |
| #3834 | `c35a9b4f1dc4` | gel #20982 : #20984 auditeur ok | panier « #3829 » : dépôt nu reconnu avant remontée, hits du cache stables (N1, N2) |
| #3833 | `8681f68ff264` | gel #20981 : #20985 auditeur ok | banc ameesh-pg : `--init` et healthcheck exec, plus de backend tué au `VACUUM FULL` |
| #3838 | `43c0475160ca` | gel #21007 : #21008 auditeur ok, #21014 ameesh-cx1 ok | carte : aucun job sur une PR de paquet, la mesure des lots reste |
| #3835 | `d751bcf0d6de` | gel #21027 : #21028 ameesh-cx1 ok, #21029 auditeur ok | porte : un délai de `ref_reachable` dit le délai, ne fait plus refuser un gel remplacé |
| #3836 | `66a5117a704f` | gel #21031 : #21035 ameesh-cx1 ok, #21039 auditeur ok | base du mesh : transaction idle détectée et garde, plus de blocage de 7 min |
| #3842 | `2414275cd1d6` | gel #21081 : #21084 ameesh-dev4 ok, #21085 auditeur ok, #21090 ameesh-cx1 ok | test l56 : rôles par processus, `DROP ROLE` stable sous `--parallel` ; double fork détaché |
| #3844 | `66a08b1526e2` | gel #21083 : #21086 ameesh-dev7 ok | panier « #3836 » (N2, P3) : l'export de snapshot exempte sa transaction de la garde idle |

## Composition (11 unités, 11 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3813 | `e563ff25600a` | `f0382bb087fd` | composition |
| 2 | #3822 | `36c7baaf4647` | `1f71cffa2a25` | composition |
| 3 | #3832 | `b5024890d926` | `f5c886c639dc` | composition |
| 4 | #3829 | `815b77b486b7` | `7cefc51a1acc` | composition + résolution #3777/#3829 |
| 5 | #3834 | `c35a9b4f1dc4` | `1a8f079f3b0b` | composition |
| 6 | #3833 | `8681f68ff264` | `11e9cd62566b` | composition |
| 7 | #3838 | `43c0475160ca` | `c8bb00d56177` | composition |
| 8 | #3835 | `d751bcf0d6de` | `b6c7db85e1dc` | composition |
| 9 | #3836 | `66a5117a704f` | `701da1194d8f` | composition |
| 10 | #3842 | `2414275cd1d6` | `1e390e88c0d5` | composition |
| 11 | #3844 | `66a08b1526e2` | `70f08c136938` | composition |

10 fusions propres, **1 résolution de composition** (unité 4, #3829 × #3777),
0 union mécanique, 0 adaptation de test. Le contrôle des définitions dupliquées
sur `src/ameesh` ne trouve aucun doublon introduit par le paquet.

## Résolution de composition (unité 4)

#3829 est gelé sur `7280d0267` (h-31) ; h-32 a fait entrer #3777 dans la même
zone de `src/ameesh/canon.py` (`_FORMAT_STATE_RE`, `_CACHE_STATEFUL_DATES` :
les lectures qui dépendent des refs, de l'heure ou du mailmap ne sont pas
cachées). Le conflit est résolu dans le commit `7cefc51a1acc` en **union** :
les symboles et les quatre contrôles de #3777 restent en place, et le cache
d'instance de #3829 (`_repo_cache_key`, `_git_directory`, `_repository_identity`,
`_CLONE_VALUE_OPTIONS`, `_INIT_VALUE_OPTIONS`) est ajouté. L'unité 5 (#3834)
remplace ensuite la partie « dépôt nu / ctime » par sa version N1/N2 relue.
Le contrôle d'inclusion lit ce contenu comme partiel pour #3829 (120/135
lignes) : les 15 lignes absentes sont exactement celles que #3834 remplace,
et l'entrée #3834 est présente. Aucun choix sémantique du composeur au-delà de
cette union.

## Retrait de #3837 (décision pilote, 27/10 02:30Z)

La ci-full 40436505390 sur `584d9efe9de7` (composition à 12 unités) a rendu
deux tests négatifs déterministes de
`tests.test_3220_deleted_prior_content.SamePathReintroducedRemovalTest`
(psycopg 4/8) : le filtre #3837 retirait le correcteur dont le retrait de
garde est réintroduit au même chemin (`{'impl'}` au lieu de `{'impl','cx3'}` ;
`out["ok"]` vrai). Le pilote a confirmé en local : vert sur `main 4df37240` et
sur la tête #3835 `d751bcf0`, rouge sur la tête #3837 `e8c3aa8db9ba`. C'est une
régression de sûreté de la porte, pas un flake.

#3837 est donc **retiré** de h-33 : #3835 entre seul, sans l'unité de
résolution #3835 × #3837, et la résolution `canon.py` de l'unité 4 est conservée. Rejeu
sur la nouvelle tête (11 unités) : les 2 tests `SamePathReintroducedRemovalTest`
sont verts. #3837 est à corriger hors paquet (h-34) ; #3506 reste en erreur de
porte en attendant ce correctif.

La même ci-full a aussi rendu 2 rouges `tests.test_l85_events.EventLogPgTest`
(psycopg 2/8) : 21/21 verts en local sur `584d9efe`, dépendants de
l'environnement et du `--parallel 2` (§11 bis) ; non concluant, aucune unité
retirée. `tests.test_l112_host_gate.ExecuteurTest.test_retrait_pendant_un_tour_au_point_sur_puis_reprise`
est hérité de la base (signalé « inherited » par la CI).

## Migrations et exploitation

Aucune migration dans les 11 unités.

- **#3836** modifie `deploy/mise-a-jour/poste.sh` et
  `deploy/serveur/installer-serveur.sh` (garde idle-in-transaction) et
  `docs/EXPLOITATION.md` : geste d'hôtes au déploiement staging de la tête.
- **#3844** modifie `deploy/mise-a-jour/sauvegarder-prive.py` : l'export de
  snapshot exempte sa transaction de la garde idle, pour que la sauvegarde
  privée ne soit pas tuée par #3836.
- **#3833** modifie `scripts/pg-up.sh` et le README. À l'activation staging,
  le bureau recrée une fois `ameesh-pg` sur chaque hôte avec le script de
  `main` (`docker stop ameesh-pg`, `docker rm ameesh-pg`, `scripts/pg-up.sh` ;
  volume conservé), conformément à la note d'activation du lot (pilote,
  27/10 00:12Z). Preuve de non-régression par dev5, vérification par le
  signaleur impl-03 ; le lot ne sera « fait » qu'après (§5).

## Écartés de h-33

| lot | motif |
|---|---|
| #3837 | **retiré sur décision du pilote (27/10 02:30Z)** : sa tête `e8c3aa8db9ba` casse deux tests déterministes de `test_3220_deleted_prior_content.SamePathReintroducedRemovalTest` — régression de sûreté de la porte. Composition sans l'unité de résolution #3835 × #3837 ; à corriger pour h-34. |
| #3506 | en **erreur** dans la liste calculée : `cannot read the review gate: author provenance unknown for frozen commit(s): 790265d8e396` (cas #3781/#3837). Le correctif #3837 étant retiré, #3506 reste écarté ; à re-tester pour h-34 après le correctif de #3837. |

Non prêts sur la base, donc hors paquet (motifs de la liste calculée) :

| lot | motif |
|---|---|
| #3519 | `freeze-after-verdict: effective freeze refused` — pas de gel effectif |
| #3526 | `verdicts-missing: no review verdict ok on the freeze 5e1bfc4a8067` |
| #3845 | `blocked-verdict: effective freeze refused` — nouveau lot #1606, pas prêt |

## Contrôles

- Composition : 11 unités sur `4df372403b68`, 10 fusions propres, 1 résolution
  (#3829 × #3777), 0 union, 0 adaptation.
- Tests locaux avant gel : **63 tests ciblés, 0 échec** sur la tête 11 unités
  (`SamePathReintroducedRemovalTest` 2/2, `test_l107_executor_contract`,
  `test_3835_prior_freeze_timeout`, `EventLogPgTest` 21/21, base jetable).
  La composition à 12 unités avait 489 tests verts au gel ; le retrait de
  #3837 ne touche que `review_authors.py`/`storage`/`contract`, dont les
  témoins ciblés sont rejoués. La ci-full sur la tête gelée fait foi.
- Contrôles statiques du gel : `scripts/freeze-checks.py` **8/8 verts**
  (garde mécanique, compilation, contrat de train, attribution, lint L63, types
  d'alerte, lecteurs de doc, contrat de la porte).
- Contrôle d'inclusion `scripts/paquet-inclusion.py` sur la tête : **vert —
  11 compositions, 9 lots prêts tous composés, 10 contenus présents, #3829
  partiel (120/135) couvert par #3834, aucun défaut**.
- Prévalidation : non bloquante, journalisée par `ameesh ci run` ; sélection
  attendue non sûre (Python hors `src/ameesh/` : `scripts/paquet-candidats.py`,
  `scripts/paquet-compose.py`, `deploy/mise-a-jour/sauvegarder-prive.py`).
