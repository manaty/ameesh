# Paquet integration/paquet-h-20261027-34 — composition (§12), sur main issu de h-33

Base : `main` **`f31c9f4396db75fbd301bbad0e0dcebaec8e5d79`** (fusion de h-33,
PR #2939 ; arbre identique à la tête de paquet `d396879ad525`). Branche
`integration/paquet-h-20261027-34`, cible `main`. Porteur : **#3859**.
Composeur : mty-amh-impl-01 (deepseek, agents-1).

Déclencheur : lot #3859 (mesh-design, 27/10 02:46Z), précisé par le pilote
(mesh-design, 27/10 03:25Z et 03:26Z) : composer sur la tête de `main` après
h-33 avec #3837, #3845, #3848, #3853 si propre après #3848, #3854 avec #3845
(le P2 sensible de #3845 bloque ; les deux entrent ensemble, #3854 avec son
avis sur e171a3ba8a87), #3846 s'il est gelé et revu à temps, #3506 à essayer.
#3846 a été gelé et revu à temps (b7108d561926, 03:36Z) : le pilote a demandé
de le prendre tel quel. #3853 est **retiré** sur B/P1 de cx2 (03:38Z) : sa
trace « sauvegarde/reporte » pour manaty-mesh-1 est refusée par
`preuve_absence` de #3848 et l'attestation du staging échoue ; #3848 entre
seul. #3868 (l85/l232a, avis dev4) est ajouté, ses deux fichiers de test
fusionnent proprement. #3867 (canon check mesure le bloc de règles entier,
avis impl-03) est ajouté sur demande du pilote (03:45Z), fusion propre. #3506 reste en erreur de provenance sur le runtime
d'avant #3837 : il est retiré, sans retenir les autres.

Méthode : composition par `scripts/paquet-compose.py` (un commit de fusion par
unité, trailer `ameesh-work:`), manifeste, contrôle d'inclusion, tests ciblés
**avant** le gel, `freeze-checks`, puis une seule `ci run --full --avis 0` sur
la tête gelée. Aucune migration dans le paquet.

## Admissions (7 lots, 7 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3837 | `cb62b67d5c53` | gel #21138 : 4 OK, dont #21149 mesh-design (claude) et #21168 ameesh-dev3 (deepseek) | porte : gels d'une tête réécrite retirés de la lecture, attribution inter-lots additive |
| #3845 | `29c0c7541320` | gel #21127 : #21129 ameesh-cx2 ok, #21131 mty-amh-impl-04 ok | paquet horaire déclenché par minuterie, sans agent ; expéditeur des courriers |
| #3854 | `e171a3ba8a87` | gel #21172 : #21182 mty-amh-impl-04 ok | panier #3845 : minuterie ameesh-paquet-horaire posée par role timers |
| #3848 | `cef2dde6f80b` | gel #21151 : #21163 ameesh-dev4 ok | staging depuis une VM, poste absent reporté, attestation des trois VM |
| #3846 | `b7108d561926` | gel #21229 : #21243 mesh-design ok, #21237 ameesh-dev4 ok (test-only, aucun avis requis) | test l112 : le témoin du point sûr compte depuis la porte appliquée |
| #3868 | `6c811f946518` | gel #21256 : #21265 mesh-design ok (et #21195 ameesh-dev4 ok sur la tête précédente) | tests l85/l232a : positions stables hors xmin étranger, reset SSE sous transaction étrangère, fenêtre capacité |
| #3867 | `2792a13e71f0` | gel #21250 : #21257 mty-amh-impl-03 ok | briefs : le bloc de règles entier mesuré, plus la seule projection |

#3845 est sensible (`deploy/**`) et porte un P2 (minuterie installée par aucun
chemin de déploiement) : selon le canon §2, il n'entre qu'avec son panier
**#3854**, qui a son avis sur `e171a3ba8a87`. Les deux sont dans ce paquet.

## Composition (7 unités, 7 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3837 | `cb62b67d5c53` | `c5d2be2f3` | composition + résolution docs/review_authors |
| 2 | #3845 | `29c0c7541320` | `a55d95ccd` | composition |
| 3 | #3854 | `e171a3ba8a87` | `27d71ab8f` | composition (panier de #3845) |
| 4 | #3848 | `cef2dde6f80b` | `abb3b7586` | composition |
| 5 | #3846 | `b7108d561926` | `047a30524` | composition (test-only, fusion propre) |
| 6 | #3868 | `6c811f946518` | `40210babc` | composition (test-only, fusion propre) |
| 7 | #3867 | `2792a13e71f0` | `17cccb38e` | composition (fusion propre) |

6 fusions propres, **1 résolution de composition** (unité 1, #3837 ×
#3835/#3836 de h-33), 0 union mécanique, 0 adaptation de test (la fusion de
#3868 sur `tests/test_l85_events.py` est propre). Aucun doublon de définition
introduit dans `src/ameesh`.

## Résolution de composition (unité 1, #3837)

#3837 est gelé sur `cb62b67d5c53`, dont la base est `6bdaa808` (avant
plusieurs unités de h-33) : sa fusion dans la tête de `main` rencontre deux
conflits textuels.

- `docs/EXECUTEUR-MEDIEE.md` : compteurs recomposés en union, **450 refusées**
  et **522 opérations d'interface** (main 449/521, #3837 ajoute une opération
  et un refus). C'est la même recomposition que la tête 12 unités de h-33
  (`2327b264b`). Le test `test_les_compteurs_du_doc_suivent_le_contrat` fait
  foi et est vert sur la tête du paquet.
- `src/ameesh/review_authors.py` : conflit avec #3835/#3836, déjà dans
  `main`. La résolution est exactement celle relue de la tête 12 unités de
  h-33 (`2327b264b:src/ameesh/review_authors.py`) **plus** le delta propre de
  la nouvelle tête #3837 (`e8c3aa8d..cb62b67d`, garde `_outside_delivered` :
  `patch[1]` interdit la dispense sur un retrait réintroduit). Vérification :
  le fichier fusionné est **identique octet pour octet** à
  `2327b264b:src/ameesh/review_authors.py` après application du delta
  `e8c3aa8d..cb62b67d`. Aucun choix sémantique au-delà de cette union.

## Écartés de h-34

| lot | motif |
|---|---|
| #3853 | B/P1 cx2 (03:38Z) : sa trace `sauvegarde/reporte` de manaty-mesh-1 est refusée par `preuve_absence` de #3848 ; l'attestation du staging échoue. Lot fautif retiré sur décision pilote (03:40Z) ; #3848 entre seul. |
| #3506 | porte illisible : provenance inconnue des commits gelés (dont `790265d8e396`, gel #19251 du lot #3509). Tenté après #3837 ; l'erreur persiste sur le runtime d'avant #3837. Retiré sans retenir les autres (consigne pilote 03:25Z). |
| #3519, #3526 | non prêts (hors paquet). |

## Migrations et exploitation

Aucune migration.

- **#3848** touche `deploy/mise-a-jour/staging.sh`, `actif.py` et
  `release_cutoff.py` : le staging depuis une VM saute le poste avec trace,
  applique les migrations depuis une VM d'agents et atteste les trois VM ;
  le poste rattrape à son retour. Recette bureau-goal prévue après fusion
  (bureau, #147558).
- **#3854** modifie `role_timers.py` : la minuterie du paquet horaire est
  posée par le déploiement, sans installation à la main.
- **#3837** touche la porte de fusion (`review_authors.py`) : à activer en
  staging avec le reste de la tête.
- **#3867** mesure le bloc de règles entier dans le brief (`reprise.py` et
  `canon check`), pas la seule projection : sans migration.

## Contrôles

- Inclusion : **aucun défaut** (7 attendus, 7 compositions, contenus complets ; prêts calculés : #3845, #3854, #3848, #3846, #3868, #3867 ; #3837 admis par le pilote avec résolution).
- `freeze-checks` : **8/8 verts** (garde mécanique #2449, compilation, contrat de train #3606, attribution #1632, lint L63, alert types #1060, doc readers L161a, contrat de la porte #3687).
- Tests ciblés : **64 tests, 0 échec** (psql + psycopg) sur #3846/#3867 et le delta #3868 précédent ; sur la tête finale, le nouveau témoin `test_a_foreign_transaction_does_not_move_the_witness_position` (6c811f94) est vert, les autres fichiers de #3868 étant identiques. #3837/#3845/#3854 : **300 tests, 0 échec** sur leurs commits identiques, dont `test_les_compteurs_du_doc_suivent_le_contrat` et `SamePathReintroducedRemovalTest`.
