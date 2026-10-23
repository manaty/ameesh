# Paquet integration/paquet-h-20261023-1100 — composition (§12)

Base : `main` **`aa33722c8`** (landing de la release 1.19.0, coupure de 11:00 du
23/10). Branche `integration/paquet-h-20261023-1100`, cible `main`, lot porteur
**#3105** (issue manaty/ameesh#2370). Une seule suite complète sur la tête
finale.

Composeur : mty-amh-impl-01, sur la délégation de mesh-design du 23/10 09:16Z,
la liste calculée du 23/10 09:1xZ sur `aa33722c8` et le canon §11 bis/§12.
Le train reste en shadow ; le repli manuel s'applique.

## Unités (11 unités, 11 lots portés)

| lot | source | classe | avis (familles) | auteur | objet |
|---|---|---|---|---|---|
| #3011 | `c90ae483a91b` | sensitive | 2/2 ok (codex cx2, claude dev2) | mty-amh-impl-06 | EP2 : score des candidats, délai par rang, garde du bail |
| #3091 | `7b4ea68487f4` | sensitive | 2/2 ok (claude dev2, codex cx1) | ameesh-dev3 | Porte paquet : base de composition prouvée sur la cible |
| #2954 | `1522c3a39dde` | sensitive | 2/2 ok (claude dev1, codex cx2) | mty-amh-impl-08 | Panier ameesh : `Work.set_type` classé au contrat, doc |
| #3008 | `2af62cfad8e8` | sensitive | 2/2 ok (codex cx2, deepseek impl-01) | ameesh-dev4 | Porte de fusion : un re-run vert lève le check requis rouge |
| #3023 | `da2dcaa1e064` | light | 1/1 ok (claude dev1) | mty-amh-impl-07 | Panier #393 : argv |
| #3044 | `baebdb693403` | light | 1/1 ok (claude auditeur) | mty-amh-impl-05 | Jauges Codex : compte team au repos |
| #3102 | `118e560a8110` | light | 1/1 ok (claude dev2) | mty-amh-impl-05 | Test instable : canal d'état L93 (EPIPE) |
| #3038 | `c572c3ee995c` | light | 1/1 ok (claude dev1) | mty-amh-docs-01 | Taille mesurée : cible publiée, ou doute |
| #3068 | `b44c74fe1063` | sensitive | 2/2 ok (codex cx2, deepseek dev5) | auditeur | Classe de relecture : cible distante complète |
| #3086 | `2eba1eeed026` | light | 1/1 ok (claude dev2) | claude1 | Projection : fermer l'issue adoptée d'un lot clos |
| #3035 | `2e2ed5161073` (résolution) | sensitive | 2/2 ok (codex cx2, claude auditeur) | mty-amh-impl-01 | Hôte lost : réconciliation vérifiée (`lost -> returning -> available`) |

## Admissions

#3011, #3091, #2954, #3008, #3023, #3044, #3102, #3038, #3068, #3086, #3035

## Composition (11 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #3011 | `c90ae483a91b` | `d727be32a` | fusion de la tête gelée, 0 conflit |
| 2 | #3091 | `7b4ea68487f4` | `bb6aeae96` | fusion de la tête gelée, 0 conflit |
| 3 | #2954 | `1522c3a39dde` | `257d923f3` | fusion de la tête gelée, 0 conflit |
| 4 | #3008 | `2af62cfad8e8` | `4dc09c677` | fusion de la tête gelée, 0 conflit |
| 5 | #3023 | `da2dcaa1e064` | `2c4ac499b` | fusion de la tête gelée, 0 conflit |
| 6 | #3044 | `baebdb693403` | `31a13bc81` | fusion de la tête gelée, 0 conflit ; la porte du véhicule la laisse en light (3 fichiers depuis la base) |
| 7 | #3102 | `118e560a8110` | `99ce480bc` | fusion de la tête gelée, 0 conflit |
| 8 | #3038 | `c572c3ee995c` | `7b6a1132d` | fusion de la tête gelée, 0 conflit |
| 9 | #3068 | `b44c74fe1063` | `20f40604f` | fusion de la tête gelée, 0 conflit |
| 10 | #3086 | `2eba1eeed026` | `2981b4fba` | fusion de la tête gelée, 0 conflit |
| 11 | #3035 | `2e2ed5161073` (résolution #3010) | `f9c15d2ea` | 2 conflits résolus (voir ci-dessous) |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Résolutions préparées, retraits et adaptations de contenu

| lot | paire | réf | contenu |
|---|---|---|---|
| #3035 | #3010 | `refs/ameesh/resolutions/3035/3010` = `2e2ed5161073` | `docs/EXECUTEUR-MEDIEE.md` : compteurs du contrat à l'union ; `src/ameesh/runner.py` : le seuil de perte garde `host_lifecycle.staleness_s` du paquet et la boucle de réconciliation #3035 |

Le lot #3035 garde sa tête gelée `b598739371f1` et ses deux avis ; le commit de
résolution est composé comme source de l'unité 11. La résolution #2954×#3035
reste disponible pour l'autre paire.

- **#2954 — adaptation de contenu, pas un retrait** : les deux lignes de
  compteurs ajoutées par #2954 (`490`/`418`) sont réécrites par l'union avec
  #3035 (`491`/`419`) dans `docs/EXECUTEUR-MEDIEE.md` ; le contenu du lot est
  bien porté, seule la valeur d'union diffère.

Aucune autre adaptation de code ou de banc n'a été nécessaire à la composition :
les 10 autres unités se sont composées sans conflit. La réparation de la CI
ci-dessous est un correctif de bancs, de classement et d'ordre du help, pas un
retrait d'unité.

## CI rouge sur 018272ca94 (23/10 09:33Z) — réparation de composition

La première suite complète a échoué sur 15 témoins distincts (29 occurrences
sur les deux dorsales). Aucune unité n'est retirée : les écarts sont des bancs
non adaptés aux comportements relus, un classement de colonnes et l'ordre du
help racine.

| unités | symptôme | correctif |
|---|---|---|
| #3011 × bancs de `sweep` (#508, #L790, #1425, #2339) | `Runner.sweep` publie les scores de rôles (`role_evaluation_once`) et purge les libertés d'élection (`_election_free_since`) ; les doublures n'avaient ni horloge ni ces attributs | les faux exécuteurs neutralisent la passe et partent sans liberté observée |
| #3011 × `test_cluster` | `role_standings` (0476) non classée : ses 9 colonnes restaient refusées au rôle superviseur | classée fermée dans `FERMEES` — état interne d'élection, comme `agent_candidates` ; aucun GRANT modifié |
| #3011 × `test_2179` | `role status` inséré entre `host enroll` et `hosts`, help racine non alphabétique | ligne déplacée entre `review-class` et `run` |
| #3091 × #2505 | le feu nomme la cible déclarée et épingle la ref lue dans `base_sha` ; le témoin attendait l'ancien libellé `origin/main` | témoin adapté : `target=main`, `base_sha=origin/main`, diff toujours lu depuis `origin/main` (léger, README seul) |

Ciblés rejoués le 23/10 : 2505 4/4, 508 8/8, L790 24/24, 1425+2339 17/17,
`test_cluster` classement 1/1, 2179 help 1/1. Aucun avis supplémentaire n'est
requis : le porteur est dispensé (#1979) et ces changements ne touchent que des
bancs, le classement d'une colonne et l'ordre du help.

Correctif : `b9b9ae185` (7 fichiers, +31/−4). La tête de la branche devient le
commit qui porte ce manifeste ; la suite complète unique est relancée sur elle.

## Écartés (ce véhicule)

| lot | source | motif |
|---|---|---|
| #3096 | `d326f0f85fb3` | **bloquant post-fusion de #2955** (B11 de la veille GitHub) ; nouvelle tête gelée, avis ameesh-dev5 et mty-amh-impl-08 attendus au calcul. Paquet suivant dès verdict. |
| #2994 | `5158130ae2da` | erreur de lecture de la porte : provenance du gel antérieur inconnue (absent du dépôt), regel requis |
| #2914 | `66a06cf09d5c` | erreur de lecture de la porte : même cause, regel requis |

## En attente au calcul (non admis)

| lot | motif |
|---|---|
| #3048 | conflit `src/ameesh/train_contracts.py` avec la cible |
| #3007 | verdicts-missing sur la tête `3a0842b5833f` |
| #3066 | verdicts-missing sur la tête `05eae2b0c265` |
| #3085 | verdicts-missing sur la tête `966bf4d87038` |
| #3057, #3070, #3033 | blocked-verdict |
| #3054 | no-verdict |
| #3063 | missing-branch |
| #2987 | branch-not-published |
| #390, #114, #2614, #2993 | verdicts-missing (et conflit pour #390/#114) |

## Contrôles

- **Liste calculée** du 23/10 09:1xZ sur `aa33722c8` : **10 lots prêts**
  (#3011, #3091, #2954, #3008, #3023, #3044, #3102, #3038, #3068, #3086),
  plus **#3035** par sa résolution préparée `2e2ed5161073` ; 2 erreurs
  localisées (#2994, #2914) et le reste en attente motivée.
- **Essais de fusion** : 11/11 unités composées, 0 conflit hors la paire
  #3035×#3010 résolue ; aucun fichier produit touché hors la résolution.
- **Tests ciblés** : 39 verts — contrat (`test_l_interface_entiere_est_classee`,
  `test_les_compteurs_du_doc_suivent_le_contrat`), `test_3008_required_gap_rerun`
  (14), `test_3035_host_reconcile` (23).
- **Contrôle d'inclusion** (`scripts/paquet-inclusion.py --manifest
  reviews/integration-paquet-h-20261023-1100.md --head <tête finale>
  --ready-file prets-1100.txt --no-expected`) : à consigner au commit de
  contrôles.
- **Contrôles statiques du gel** (`scripts/freeze-checks.py`) : au gel du
  porteur #3105, sur la tête finale.

## CI

Suite complète unique portée par le run `pull_request` de la PR sur la tête
finale qui porte ce manifeste, étiquette `ci-full-<tête>` ; aucune CI de lot
entre deux paquets (§11 bis). Les runs d'une tête remplacée sont annulés.
