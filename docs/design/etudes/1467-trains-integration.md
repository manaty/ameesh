---
type: Study
status: in-progress
work_item: 1467
---

# Trains d'intégration automatiques

Le lot #1467 automatise la composition des paquets sur `ameesh/main` et
`Nexlink/develop`. La préparation ci-dessous reste hors du runner : elle ne
modifie aucun profil et ne déclenche aucune publication ni CI réelle.

Sources : bureau, fil #1467 du 15 octobre 2026 à 03:20 ; attribution et
cadrage mesh-design #45627 à 03:24 ; précisions mesh-design à 03:28 et 03:29 ;
contrat Nexlink communiqué par orchestrateur à 03:28 et diagnostic de son
profil à 03:31. La référence de six heures et ses phases figurent au registre
du lot ; les correctifs prioritaires précèdent l'activation de ce train.

## Socle implémenté

`train.Engine` avance par événements ou passages du runner, sans attente
active de CI. Un train part à cinq lots admissibles ou une heure après la
première admission / le départ précédent. Les dépendances ouvertes viennent
avant les priorités ; un prérequis absent ou un cycle empêche l'admission.

L'intention est conservée en base, dans `integration_trains` (migration
additive réservée 0442), avec une révision comparée à chaque écriture. La clé
est celle de `land.lock_key`, sous le bail existant `merge_locks`. Le propriétaire
du train se distingue de celui d'un atterrissage individuel, même dans le même
runner. Le stockage verrouille le bail puis recontrôle son échéance avant
l'écriture. Les nouvelles opérations sont refusées par l'API médiée ; aucun
droit ni accès distant n'est ajouté.

Le manifeste fixe source, cible, base, identités des admissions (génération,
gel et SHA), preuves de qualification, commit et arbre du train. Sa branche
`integration/train-<date-heure>-<empreinte>` est immuable. Le commit final est
reconstructible à l'identique après redémarrage ; chaque recomposition porte
un nonce distinct, donc ne peut hériter d'un vert rendu sur un autre essai,
même si son arbre est identique.

`train_git.Repository` réutilise `land.merge`, les portes et le bail Git
distant. Publication et promotion comparent atomiquement la branche attendue
et la référence du bail. La promotion écrit le commit testé lui-même. Une
cible déplacée, une branche de train modifiée ou une génération ancienne
refuse le geste. Les cibles protégées et les branches de release restent
refusées. Le premier raccord n'utilise aucune résolution automatique : un
conflit attend sa qualification de delta.

Les phases persistées précèdent les effets externes : `publishing`,
`ci-intent`, `ci`, `ready`, `promoting`, puis `landed`. Une réponse de dispatch
perdue ne provoque pas un deuxième dispatch. Une promotion incertaine conserve
son intention ; le passage suivant constate la cible exacte ou sollicite le
pilote. Une pause pour échecs répétés ne repart pas d'elle-même.

`train_ci.GitHub` distingue un lancement par push d'un lancement explicite
avec `expected_sha`. Il lit les workflows requis sur le commit, la branche,
l'événement et la tentative exacts. Un job requis sauté ne prouve pas une
suite exécutée. Une annulation ne permet pas d'accuser un lot. L'adaptateur
ne relance et ne sonde jamais en boucle.

Après un rouge, la bissection teste ses moitiés avec leurs prérequis. Un
prérequis fautif reporte ses dépendants sans les désigner comme fautifs. Les
moitiés vertes ne suffisent jamais à qualifier leur union : le reste est
recomposé et passe sa propre CI complète. Une interaction entre deux moitiés
vertes provoque une pause avec ses preuves. Le plafond d'échecs borne le coût.

## Qualification et feu : raccord restant avant gel

Les ports `qualify` et `authorize` sont obligatoires. Il n'existe aucun
fournisseur de GO implicite, aucun raccord au runner et aucune activation de
configuration dans cette préparation. Le dernier verdict `ok` de la file
actuelle n'est pas un contrat d'admission suffisant.

Le fournisseur doit vérifier les familles réelles à la date des commits et
verdicts, pour chaque auteur, à partir des sessions/tours historiques. Le nom
d'un agent, son registre courant et les textes libres ne sont pas des preuves.
Le contrat est coordonné directement avec impl07 (#1465 / ancien #1053).
Unknown refuse l'admission. Les avis requis, blocages B et P2 sensible,
retraits et deltas doivent être requalifiés juste avant chaque effet.

Les deux modes de CI sont explicites : `packet` préserve le remplacement
de la CI de lot par celle du paquet (§12), `targeted` exige en plus la CI
ciblée du lot. Le choix de déploiement attend l'arbitrage demandé au bureau ;
aucun run supplémentaire n'est imposé implicitement.

Le feu de #1374 à `979f2e2b` porte un lot et son gel. Il ne constitue pas un
feu machine complet de ce nouveau manifeste. Proposition de raccord : un
enregistrement structuré contenant source, cible, base, commit, arbre,
empreinte des membres et des preuves, acteur pilote et identité du feu,
révocable ; aucune inférence à partir des notes. `authorize` relit ce fait et
`promote` doit sérialiser la revalidation et le geste avec son retrait. Cela
réutilise le feu d'un pilote agent et ne crée aucune approbation humaine.
L'autorité déclarée se vérifie avec les gardes existantes, sans historique de
délégation comme substitut. Le raccord doit aussi empêcher la voie de feu
direct de #1374 de contourner la réservation du paquet.

## Raccord des dépôts restant avant gel

Sur ameesh, `tests.yml` déclenche déjà la suite complète au push sur
`integration/**`. Ne pas ajouter un dispatch à ce même push. Le contrat des
jobs requis doit couvrir toutes les parts exécutables de la suite complète,
avec leur matrice, à partir du workflow de la tête qualifiée.

Sur Nexlink, aucun push `integration/**` ne déclenche les suites. Chaque
workflow doit être vérifié sur la cible gelée : présence de `workflow_dispatch`,
garde `expected_sha` et jobs exécutables complets. La liste communiquée par
orchestrateur comprend dix-sept suites et `docs-gate` ; elle reste à vérifier
fichier par fichier avant raccord, sans extrapoler de celle d'ameesh.

L'échec `queue add` sur le poste orchestrateur est dû au profil
`critiques-2.json` sans `radar.projects`, alors que `radar-nexlink.json`
déclare les conditions du dépôt. Le diagnostic CLI ajouté nomme la source
et l'absence de projet radar ; il ne crée aucun clone implicite ni condition
d'atterrissage. Coordination directe avec impl05/#1466.

## Vérification

Les témoins ciblés couvrent cadence, dépendances, retrait et nouvelle admission,
intentions de dispatch/promotion, bissection avec prérequis, recomposition,
preuve de CI, branches et bail Git sur un dépôt bare jetable. Les tests SQL
portent sur les révisions, l'expiration, la reprise et le partage du bail avec
`land`. Les suites complètes appartiennent à la CI du paquet. Le lot reste en
`build` ; aucun gel, avis de livraison, feu, PR ou activation n'est déclaré.
