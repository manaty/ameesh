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
active de CI. En construction/bêta, sa politique par défaut suit les coupures
de 11:00 et 23:00 Europe/Paris (canon Process §11 bis, instruction owner du
15 octobre vers 06:05). Seules les admissions présentes à la coupure entrent
dans le paquet ; une admission tardive attend la suivante. Un redémarrage
ne compose pas un second paquet pour une coupure déjà commencée. L'ancienne
cadence horaire/seuil reste disponible hors du mode construction/bêta,
sans configuration active. Les dépendances ouvertes viennent avant les
priorités ; un prérequis absent ou un cycle empêche l'admission.

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
ne relance et ne sonde jamais en boucle. Les blobs audités des workflows et
de leurs planificateurs sont vérifiés sur le commit exact : conserver le nom
d'un job tout en changeant sa matrice ou ses conditions ne transmet aucun
vert. Un échec d'agrégat avec toutes les parts vertes ne désigne aucun lot.
Une nouvelle tentative CI pendant l'attente du feu remplace la preuve verte
antérieure ; le moteur la relit avant de promouvoir.

En construction/bêta, un rouge conserve les preuves et sollicite le pilote,
sans lancer de CI diagnostique supplémentaire ni accuser un lot sans preuve.
L'isolation permettant de livrer le reste du paquet demande encore un
raccord de diagnostic qualifié avant activation. La bissection automatique
ci-dessous reste réservée à la politique hors construction/bêta.

Après un rouge, cette bissection teste ses moitiés avec leurs prérequis. Un
prérequis fautif reporte ses dépendants sans les désigner comme fautifs. Les
moitiés vertes ne suffisent jamais à qualifier leur union : le reste est
recomposé et passe sa propre CI complète. Une interaction entre deux moitiés
vertes provoque une pause avec ses preuves. Le plafond d'échecs borne le coût.
La nouvelle union devient le périmètre des diagnostics suivants : un membre
déjà exclu ne peut réapparaître comme contexte de prérequis.

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

`train_review.qualify` fournit maintenant le validateur pur de ces preuves.
Il couvre chaque commit de contenu par les avis sur la tête finale, avec
des familles différentes de cet auteur et entre elles, et des sessions
distinctes. Le correcteur est l'auteur de son commit. Un blocage d'un autre
relecteur ne disparaît pas sous un nouvel `ok` ; un regel ne l'efface pas.
Le classement et son nombre d'avis viennent du fournisseur de politique,
avec refus préalable des releases et des classements inconnus. Ce module ne
constitue pas un fournisseur de données ni une autorisation de runtime.

`train_review.qualify_recorded` raccorde le contrat de lecture de #1465 au
validateur. Le fournisseur est obligatoire et explicite ; les reçus de
commit et de verdict sont convertis sans compléter de champ. Une absence
reste inconnue, une contradiction du fournisseur devient un refus du train,
et un reçu d'un autre sujet est rejeté. Ce raccord ne garantit pas la
frontière d'écriture du fournisseur : celle-ci doit encore prouver que le
commit a été créé dans le tour enregistré et que l'acteur du jalon est celui
du tour. Aucun appel du runner n'est ajouté.

`train_history.Recorded` prépare la lecture réelle utilisée par les futures
admissions : plage complète `base..gel` depuis les objets Git fetchés et tous
les jalons du lot (`milestones(..., None)`, sans limite de page). La génération
de file doit toujours désigner le dernier gel, même si le SHA est inchangé.
Un dépôt shallow, une base absente, un gel déplacé ou un auteur inconnu refuse
la qualification. Le besoin d'avis et le fournisseur exact restent explicites ;
ce lecteur ne classe pas les lots et n'active aucun fournisseur ni runner.
Les modules SQL du train sont inscrits à la liste pilote psql, en complément
de leur exécution complète dans le paquet.

`train_admissions.Admissions` raccorde maintenant ce lecteur à la file réelle
et aux dépendances complètes du stockage. Une admission retirée, réinscrite,
regelée ou déplacée est refusée ; les lots en attente humaine ou bloqués
restent hors du paquet. Une exclusion exige le bail cible vivant et la même
génération de file, vérifiés par le CAS existant de `queue_update` ; elle ne
peut écraser une réadmission concurrente. Les preuves structurées conservent
le lot, la base, le gel, la classe, le nombre d'avis et le commit de politique.
Le mode `packet` ne lit aucune CI de lot ; `targeted` exige un lecteur explicite
et ses runs exacts.

Le fournisseur de politique reste obligatoire : il reçoit le lot courant,
l'admission, la base exacte et le dépôt d'historique. Il doit rendre un
`Requirement` sur ce lot/base/tête, avec classe et nombre d'avis connus,
lié à une source versionnée. Aucun tel fournisseur n'est activé. Le seul
classement `review_policies.risk_classes` ne fournit pas les nombres d'avis
par sous-domaine. Mesh-design confirme ce port obligatoire le 15 octobre à
08:49, avec référence à `processus/lots.md` §2/§12 du canon manaty,
commit `ea043e087fb0c80708ed35b18863da1da473fccb` ; aucune déduction depuis les
notes ou le registre courant ne le remplace.

`train_policy.Versioned(path, commit, project)` prépare maintenant ce
fournisseur. Il lit uniquement l'objet Git `federation.yaml` au commit complet
audité choisi explicitement, jamais le checkout. Il exige la politique
`risk_classes.projects.<projet>` avec son `default` explicite, et la nouvelle
déclaration suivante (exemple de forme, pas une couverture de dépôt) :

```yaml
review_policies:
  train_reviews:
    schema: ameesh-train-reviews-policy/1
    projects:
      exemple:
        rules:
          - paths: ["queue/**"]
            reviews: 1
          - paths: ["db/**", "security/**", "deploy/**"]
            reviews: 2
```

Chaque règle porte exactement `paths` et `reviews`, entier 1 ou 2. Il n'y a
pas de `default` pour les comptes : chaque chemin non léger du diff doit
être couvert explicitement, sinon le fournisseur refuse. Le maximum des
règles applicables l'emporte ; une règle positive élève aussi un chemin
léger en sensible. Un chemin léger sans règle demande zéro avis avant fusion.
Les renommages gardent leurs deux chemins et les suppressions leur ancienne
portée. Une identité de fichier que la normalisation changerait est refusée.
La preuve lie lot, base, tête et commit du canon, sans repli entre projets.

Mesh-design confirme le 15 octobre à 11:42 qu'il prépare cette déclaration
dans le canon, sans fournisseur concurrent identifié. SQL/sécurité/prod
demandent deux avis ; les chemins CI restent à deux tant que leur granularité
ne distingue pas la porte de fusion. Nexlink doit être qualifié par son
pilote avant activation. Le lecteur seul ne complète pas le canon existant
et n'active aucun train. Quatorze témoins Git passent ; le raccord complet
politique/lecteur historique/file SQL passe avec psql et psycopg, avec refus
du regel sur le même SHA. Aucun run distant n'a été lancé.

Le candidat canon `c61e6a9736d368829b9664dc765eea11f5679ca3`
([home PR85](https://github.com/manaty/home/pull/85), mesh-design, 15 octobre
11:50) est consommable par ce lecteur sur les deux diffs réels : #1467
`e4e4c85c..0905f9cd` et #1470 `344c052b..6553d3ae` demandent chacun deux
avis. Ce témoin de raccord ne qualifie pas la politique. Orchestrateur a
ensuite ajouté les deux portes `scripts/migration-landing.mjs` et
`scripts/lib/migration-landing.mjs` ; mesh-design annonce à 11:57 la tête
`35e773e510d32dd05df1172536222e240a0c2ddc`, encore candidate dans l'attente
de l'avis DeepSeek sur cette correction. Aucun commit candidat n'est
sélectionné dans un profil actif.

Interface de raccord proposée à impl07 le 15 octobre à 03:54 : un `Receipt`
porte `subject` (`commit:<sha>` ou `milestone:<id>`), `commit` résolu, `actor`,
`session`, `turn`, `harness` et `record` de provenance exacte. Un reçu de
verdict porte aussi les cinq champs originaux du jalon (`id`, `actor`,
`at_ts`, `sha`, `verdict`) ; un reçu ne peut être réaffecté à un autre fait.
Le fournisseur doit établir ces liens avant de construire les reçus ; une
chaîne `record` fournie par un appelant ne suffit jamais à les attester.
La plage Git et l'historique des jalons sont complets, sans limite de lecture
silencieuse. Aucun appel CLI de fourniture arbitraire de reçus n'est ajouté.

Source vérifiée par mesh-design le 15 octobre à 03:53 : #1053 à `f934b3e7`
est descriptif au gel ; `turn_costs` (0014) et `persona_sessions` (0066)
ne portent pas seuls un lien exact commit/verdict vers son tour. Une session
peut être rouverte en modifiant son intervalle. Le rapprochement temporel le
plus proche demeure donc refusé ; le fournisseur exact reste chez #1465.

Les deux modes de CI sont explicites : `packet` préserve le remplacement
de la CI de lot par celle du paquet (§12), `targeted` exige en plus la CI
ciblée du lot. En construction/bêta, §11 bis impose `packet`, sans CI par lot
hors incident ou déblocage de CI. Aucun run supplémentaire n'est imposé
implicitement et aucun branchement de déploiement n'est activé.

Le feu de #1374 à `979f2e2b` porte un lot et son gel. Il ne constitue pas un
feu machine complet de ce nouveau manifeste. `train_fire.Fires` implémente
désormais le reçu structuré du paquet : plan complet, empreinte JSON, acteur
pilote authentifié et identifiant persistant. Cela couvre source, cible,
base, commit, arbre, membres ordonnés avec leur génération et preuves. Un
plan diagnostique, partiel ou modifié est refusé. Les tuples des preuves et
leur représentation JSON en tableaux ont la même empreinte.

Don et retrait passent par `work.corrector(history=False)`, la garde de rôle
déclaré et d'identité de session utilisée par #1374. L'acteur du don est
recontrôlé au moment d'autoriser et avant la promotion ; l'historique des
attributions ne fournit aucune autorité. Aucun feu ne se déduit d'une note.
Les reçus sont conservés dans `integration_train_fires`, extension de la
migration additive réservée 0442 ; un nouveau don après retrait a un autre
identifiant et ne réactive jamais l'ancien reçu. Les trois opérations de
stockage ajoutées restent refusées par l'API médiée (total d'interface 391).

Don, retrait et réservation de promotion verrouillent la même ligne cible
`merge_locks`. Le premier retrait réussi empêche la promotion. Si la
réservation de promotion gagne, elle exige le bail vivant et l'intention
persistée `promoting` sur le plan exact ; son état est commis **avant** le
geste externe. Un retrait arrivé ensuite indique qu'il faut réconcilier : il
n'annonce jamais un succès pendant un push incertain. Cette réservation
marque le début de la promotion ; elle n'est pas réactivée après exception,
crash ou perte de réponse. Un nouvel appel ne rejoue pas le push.

Après réservation, l'adaptateur de promotion doit relire les admissions, la
CI complète courante et les gardes du dépôt, puis promouvoir le plan exact
avec `Repository`. Le fournisseur renouvelle et contrôle aussi le bail juste
avant ce geste. Le moteur conserve le reçu dans son intention de promotion.
`Fires.reconcile` achève le reçu après redémarrage, sous un nouveau bail
vivant, si une lecture fraîche prouve la cible sur le commit exact du train.
Une cible encore sur la base ne suffit pas à prouver l'absence d'un push en
vol : l'intention reste incertaine. Une cible divergente exige inspection.
Le moteur ne déclare pas `landed` sans reçu réconcilié sur le plan et l'ID
du feu exacts. `train_runtime.Runtime` raccorde ces validations et la lecture
distante aux reçus persistants. Son fournisseur d'admissions est obligatoire :
il doit livrer les faits complets de la file, les dépendances, les preuves
historiques et les exclusions sous bail. Le constructeur exige le même bail
dans tous les adaptateurs et le même dépôt dans le contrat CI ; il ne fournit
aucune politique de qualification implicite.

`train_runner.Passage` assemble ces ports pour un passage borné du runner.
Le pilote doit fournir explicitement propriétaire, politique, objet Git du
canon, contrat CI complet, fournisseur exact de provenance et callback de
diagnostic. Le mode éteint ne lit ni Git ni SQL. Chaque appel reçoit sa
connexion courante et ne conserve que l'époque du bail ; aucune connexion
fermée ne peut être réutilisée au passage suivant. Le clone privé est distinct
pour chaque source, cible et propriétaire, et séparé du clone du radar. La
qualification récupère d'abord les branches exactes dans ce clone : un dépôt
ne connaissant que la cible peut ainsi lire l'historique des lots. Aucun
profil ni point d'appel du runner actif n'est installé par ce module.

Cinq témoins de ce passage passent avec psql et psycopg ; la passe psycopg
inclut aussi les treize gardes du runtime (18 tests verts). Git et SQL sont
réels et jetables, CI et fournisseur de reçus sont simulés explicitement.
Le clone initial ne connaît que la cible ; le témoin suit récupération du
lot, politique versionnée, deux avis, publication, attente du feu, changement
de connexion et promotion du commit testé. Un second propriétaire reste
exclu par le bail sans créer son clone ; l'absence de provenance refuse la
publication. Ces témoins ne qualifient pas le fournisseur externe ni les
workflows distants.

Après réservation du feu, Runtime relit la cible distante, les générations
de la file, les prérequis ouverts et leur ordre, les branches sources
réellement fetchées, les preuves historiques et la CI complète courante.
Un défaut à cet instant conserve l'intention
incertaine et interdit le push. La reprise passe uniquement par la lecture
fraîche de cible et `Fires.reconcile`, sous le nouveau bail. La comparaison
des preuves dans le moteur respecte leur passage par JSON (tuples en mémoire,
tableaux persistés) sans accepter une preuve différente.

Le raccord doit encore empêcher la voie de feu direct de #1374 de contourner
la réservation du paquet. Lecture de `979f2e2b` : `fire._lock` prend seulement
`fire:<lot>` ; `_merge_direct` ne prend pas le bail cible de `land.lock_key`.
Ce bail par lot ne sérialise donc pas une fusion directe avec le train.
Mesh-design demande le 15 octobre à 04:46 à l'auteur de #1374 de coordonner
le bail cible commun, l'ordre de prise et un témoin train/direct. Ce code
n'est pas encore intégré à cette branche. La livraison `82a8a0f8` de #1374
prend ce bail après `fire:<lot>` et emploie `land.LOCK_TTL_S` à la prise et
au renouvellement. Son parent `c975eb0c` (correcteur cx1, courriers #48028 et
#48030 du 15 octobre à 06:34) canonicalise `land.lock_key` pour `main`,
`origin/main`, `refs/heads/main` et `refs/remotes/origin/main`. Ces deltas
attendent leurs avis indépendants ; cette lecture de raccord ne les remplace
pas et ne fusionne pas leur code.

`Ledger` accepte désormais un alias seulement si la fonction partagée lui
donne exactement la clé de sa cible canonique. Runtime exige la même égalité
pour chaque admission. Avec l'ancienne fonction, les alias restent refusés
avant tout effet ; avec le correctif partagé, ils emploient le même bail.
La cible brute de chaque admission reste dans le manifeste et dans son
identité : le raccord ne réécrit pas une génération ou une preuve de la file.
Le refus côté train seul ne sérialise toujours pas une autre voie qui
utiliserait une clé différente. Aucun CLI, branchement du runner ni profil
actif n'est ajouté.

Vérification du fournisseur #1465 à `4750fc09`, le 15 octobre : la restriction
à HEAD et au committer du tour refuse un SHA arbitraire et un autre acteur,
mais ne prouve pas encore la création dans le tour. Un témoin Git réel avec
stockage simulé laisse un ancien commit sans reçu en HEAD, puis l'enregistre
sous une nouvelle session, un nouveau tour et une autre famille du même
acteur : `record_commit` accepte cette attribution. Aucun changement de SHA
ni checkout n'est nécessaire. Le scénario a été transmis directement à
impl07, copie dev4 et mesh-design ; aucun SQL du fournisseur n'a été exécuté
ici. Le port de lecture reste explicite et aucun fournisseur n'est activé.

Le correctif fournisseur `1acc269d` exige désormais une attestation pre-commit
propre au tour, stockée dans le répertoire Git du worktree puis consommée par
l'écriture du reçu. Trois témoins de raccord Git réel/stockage simulé
confirment le refus du HEAD hérité, le refus d'une attestation écrite après
ce commit et l'acceptation d'une nouvelle création attestée avec consommation.
L'écart précis est clos sur cette tête ; aucun SQL ni suite du fournisseur
n'est rejoué ici et ses deux revues indépendantes restent requises.

Source du travail indépendant : mesh-design, message #45970 du 15 octobre
à 04:06 et note du registre #1467 à 04:06, précisant que le hold ne concerne
que les raccords externes #1465/#1470 ; la préparation du feu continue.
Mesh-design confirme le schéma et demande les témoins de crash/reprise dans
son courrier du 15 octobre à 04:14 ; ce cadrage ne vaut ni revue du code ni
feu d'activation.

## Raccord des dépôts restant avant gel

Sur ameesh, `train_contracts.ameesh()` décrit le contrat vérifié sur
`e4e4c85c` : push `integration/**`, `plan`, quatre parts psycopg, six parts
psql, `plateforme macOS` et `tests`. Aucun dispatch supplémentaire. Les blobs
de `tests.yml`, `test-plan.py`, `test-parts.py`, `ci-flow.py` et `map.py` sont
liés à ce contrat. Leur modification exige un nouveau contrat audité ; elle
ne peut hériter des anciens noms de jobs. `Repository.publish` exige ce
contrat et vérifie les blobs dans le commit local avant tout geste distant,
y compris la prise du bail Git. Un fichier absent, modifié ou remplacé par
un lien symbolique refuse la publication. Les planificateurs exécutables
restent acceptés. Le push ne lance donc pas une CI dont le contrat est déjà
connu comme périmé.

Sur Nexlink, aucun push `integration/**` ne déclenche les suites. La lecture
fichier par fichier sur `develop@ddbe5c9f073b60d218da0414688188899da3b544`
confirme les dix-huit `workflow_dispatch` et la garde `expected_sha` dans
chaque job exécutable. Le tableau ci-dessous identifie leurs blobs et jobs ;
les jobs réutilisables de statut ne remplacent aucune exécution.

| Workflow `.github/workflows/*.yml` | Blob | Jobs exécutables au dispatch |
| --- | --- | --- |
| backend-suite | `bd96a40996502a4b240edd1aad66c29e3f6568ef` | backend ; changed-paths réservé au push develop |
| desktop-suite | `0b02fbc6ff255bd3ee559d3ed93d44315f7af24e` | desktop ; Desktop suite Windows/macOS selon CI_HOSTED_DESKTOP |
| host-rust | `1d6fb4f00d2eb23fa9b41d49e601a31a3f77e080` | host (ubuntu-latest) ; matrice Windows/macOS selon CI_HOSTED_DESKTOP |
| mobile-suite | `2de99b599e30b29b36bd52a8562876727696becf` | mobile |
| scripts-suite | `d3adae3ac635760db96f8b15a7d23b8a78f2fdb9` | scripts |
| website-suite | `dc8a926633307654b234a230f71721e5bb08e8a3` | website |
| sql-invariant | `83ef745d40f634db7a2cdc1b8bed46e6001d0358` | sql-rules (CONTENT_DB=1) |
| sync-checks | `86af79354c5eb614ff9a499c49c7b500f091f121` | all |
| message-crypto | `d35012345ade7c9c67fce1816ebcc5227e387022` | typescript, backend-copy, python |
| node-oplog | `dbe8104b5c108dfd93842d824743330fe1996882` | typescript, python |
| node-channel | `3d92156b06261ae014777db965ace7d7767ab144` | typescript, python |
| nexlink-key | `c9bdb1322a70a4160d0a6ce0474c6615b5549d81` | typescript, python, drift |
| host-key | `77f81bd501839d6d22a81206008ef5cc4d8317cb` | typescript, python |
| page-host | `8ca62018d647efc68842a0df17697d296611736c` | typescript, sync |
| remote-help-ts | `545896e468bbde7a4790855d0df16f4820577319` | shared, drift, website-remote-mock, backend, wp10-wake-sampler |
| routing-library | `d210b0d096864a7b7cd063e54983a27d094d224b` | vectors |
| rollback-checks | `dca228aa5602bfac6b245a9c326953832d7372f6` | all |
| docs-gate | `b3ca96df80f6950e3d029c84ef8fd6b29c856268` | docs |

**Écart bloquant constaté, communiqué au pilote à 03:50** : backend-suite
(ligne 368) et website-suite (ligne 122) passent par `change-set.mjs` avec
`workflow_dispatch` sur le train. Ce script (blob
`7278613797a7d233f17e430b06fd3ef1df3a6e89`, ligne 104) ne force la suite
complète que sur push/develop. Un job vert peut ainsi n'avoir exécuté que
les impactés, voire avoir sauté l'étape de tests. Il n'existe donc pas encore
de contrat Nexlink complet utilisable. Orchestrateur a confié le raccord
au chantier CI #1470 / mty-nxl-impl-01 (courrier du 15 octobre à 03:55,
confirmé par mesh-design à 03:57), deux avis indépendants avant CI. Le SHA
du gel permet de reprendre l'audit ; aucun dispatch n'a été lancé ici.

Lecture de #1470 à `6553d3ae` le 15 octobre : le blob `change-set.mjs` devient
`4f72857235625d74647fc393e8aade609faa976b`. Son classifieur force `full` sur
`refs/heads/integration/train-*` pour `workflow_dispatch`, indépendamment du
diff Git ; sa CLI évite même de calculer ce diff sur ces réfs. Trois témoins
locaux supplémentaires confirment le dispatch complet, le maintien de la
sélection sur une branche de lot et la CLI complète sans dépôt Git disponible.
Le défaut précis du sélecteur est donc corrigé à cette tête. Cela ne fournit
pas encore le contrat exécutable de tous les workflows : leurs jobs, scripts
appelés et variantes de plateforme doivent être qualifiés et liés à leurs
blobs avant un dispatch de train. Aucun contrat Nexlink implicite n'est ajouté.

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

Seconde passe : 44 tests ciblés moteur/Git/CI/historique verts, dont
renouvellement de CI pendant attente du feu, refus d'une part manquante sous
agrégat vert, sources CI modifiées, exclusion conservée après recomposition,
attribution de correction et indépendance historique de chaque auteur.
Vérification du planificateur réel : les cinq blobs correspondent au contrat,
et un plan push/integration répartit tous les modules dans quatre parts
psycopg et six parts psql, sans sélection de lot ni voie doc/version.
Les trois témoins SQL psql et psycopg du premier point de travail restent
inchangés. La passe suivante ajoute les témoins SQL du feu : liaison exacte,
historique des retraits, ancien reçu, intention préalable, bail périmé,
course retrait/réservation, crash entre réservation et push, push réussi
dont la réponse est perdue, reprise sous un nouveau bail et absence de rejeu. Les
raccords encore nécessaires avant gel sont le fournisseur exact #1465, le
contrat Nexlink #1470 et la coordination du bail cible avec #1374. Les
validations avant promotion, la réconciliation et le contrôle local des
sources CI sont implémentés dans les adaptateurs. Les revues requises restent à
obtenir ; cette préparation ne vaut pas autorisation d'activer le train.

Passe feu/reprise : treize scénarios SQL du feu et trois du stockage sont
verts avec psql et psycopg, sous `ameesh heavy`. Les tests ciblés
moteur/Git/CI/historique et le contrat API sont verts (63 à la première
passe, puis revalidation du moteur et ajout du refus de réconciliation sans
reçu exact). Le diff est propre. Aucune suite complète ni CI distante n'a
été lancée ; le lot reste `build`, non gelé.

Passe adaptateur : 79 tests ciblés moteur/Git/CI/feu/stockage/contrat API
verts sous psycopg, puis les onze témoins runtime finaux verts sous les deux
pilotes. La passe psql couvre aussi les six témoins Git (dont refus avant
écriture distante pour contrat périmé et lien symbolique) et les seize
témoins feu/stockage. Les cinq sources CI du contrat ameesh correspondent
aux objets de cette branche, planificateurs exécutables compris. Retrait,
regel/réadmission, déplacement d'une branche source, nouveau prérequis,
nouvelle tentative CI et réponse de push perdue sont éprouvés à travers
Runtime et les reçus SQL réels. Les raccords externes et le runner restent
à compléter avant gel et activation.

Passe alias partagés : trois témoins ciblés psql verts sur la préparation,
puis quatre témoins interbranches sur #1374 à `82a8a0f8`, verts sur psql et
psycopg sous `ameesh heavy`. Le témoin de bail couvre les seize couples des
quatre alias, dans les deux sens train/feu direct, et vérifie le TTL à la prise
et au renouvellement. Les verrous SQL et les fonctions de clé/prise de bail
de #1374 sont réels ; les portes métier et effets distants du runtime restent
simulés. Le contrôle avec une ancienne fonction de clé refuse les alias ; le
contrôle avec la clé corrigée permet le parcours SQL publication/feu/promotion.
Aucun fichier `land`, `fire` ou `merge_queue` n'a été modifié ici.
