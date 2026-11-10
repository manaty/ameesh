---
type: Study
status: draft
title: "Évaluation des modèles : mesures observées et comparaisons encore impossibles"
---

# Évaluation des modèles : mesurer avant de modifier le routage

## Décision et résultat

Étude du **lot #6650**, autorisée par la décision owner **#207426 a**,
réponse signée du 10/11/2026 à 05:51 UTC, approbation **#297**,
saisie par le bureau sous la délégation §10.
Vérification : `ameesh decide show 207426 --json`,
`proof_check.ok=true`. Le pilote #6648 ouvre les unités proposées
**après les deux relectures de familles distinctes de l’auteur Codex**.
Cette étude ne crée pas de lots, ne change ni comptes, ni routage, ni portes,
et ne lance aucune campagne de modèles.

**Résultat principal.** Sur 14 jours, les traces permettent de décrire le
coût comptable et les reprises des lots, mais pas de désigner une famille
« meilleure » à qualité égale. La sous-cohorte avec preuve de staging
compatible comprend **392 unités**, **3 734 tours attribués** et
**966,043786 USD comptables** du début du travail à la première attestation
compatible. Le réel/estimé médian jusqu’à intégration est **2,018** sur
382 ratios connus. Les familles ont des populations et des métiers différents.

Les trois familles ont produit des lots livrés et des blocages de revue.
**La justesse des B, la classe historique léger/sensible et l’effort exécuté
ne sont pas identifiables dans les sources structurées retenues.**
Ce sont des inconnus à instrumenter, pas des zéros ni une raison
d’extraire les corps de courrier. Le premier chantier proposé est un rapport
reproductible immédiatement utilisé, suivi des métadonnées manquantes ;
le routage restera indicatif jusqu’à une décision séparée.

## Ce qui existe, et ce qui a changé depuis octobre

Base du code examinée : `main@988cbad42b297bdcfe4791a7144c37478d137764`.
Le format suit les études [#6173](versionnement-agents.md) et
[#6308](boucle-auto-reparation.md).

- La [décision 0020](../decisions/0020-catalogue-et-evaluation-des-modeles.md)
  distingue catalogue, découverte, évaluation et recommandation.
  Elle est une décision historique rapportée en octobre, avec attestation
  non signée dans ce fichier ; l’autorisation présente est #207426.
- Les [constats d’octobre](constats-modeles-2026-10.md) comparent quelques
  rejeux : un essai contaminé puis deux implémentations isolées et une revue.
  Leurs notes 8/10 ou 9/10 ne forment ni un taux de défaut ni une estimation
  de précision sur les 14 jours actuels. Leur `stale_after: 2026-11-04`
  est dépassé : ils restent une source d’hypothèses.
- La [décision 0023](../decisions/0023-critere-d-adoption-des-reglages.md)
  demande un gain significatif sans perte de qualité après revue et recette.
  Le présent relevé observationnel ne démontre pas ce critère.
- **Catalogue déjà implémenté** : `ameesh models list|show|discover`,
  [catalog.py](../../../src/ameesh/catalog.py),
  [stockage](../../../src/ameesh/storage/postgres/catalog.py),
  migration [0015](../../../src/ameesh/migrations/0015_model_catalog.sql),
  tests `test_catalog*` et `test_models_entrypoint.py`.
  Les efforts *acceptés* du catalogue ne disent pas l’effort *exécuté* d’un tour.
  Il n’y a pas lieu de recréer un catalogue dans ce chantier.
- **Banc déjà implémenté** : `ameesh replay bench|run|report`,
  [replay.py](../../../src/ameesh/replay.py), tests
  [#4292](../../../tests/test_4292_replay.py) et
  [#3735](../../../tests/test_3735_bench_catalogues.py).
  Il distingue comparaison de binaires et de modèles, prévoit base archivée
  sans historique/distant, tests du vrai lot et revue croisée anonyme.
  Son banc de trois lots et ses tolérances sont un outil, pas une preuve
  statistique de supériorité. **Aucun `replay run` n’est lancé ici** :
  une campagne consomme des jetons et demande une autorisation distincte.
- Depuis octobre, [estimates.py](../../../src/ameesh/estimates.py) et
  [estimate_history](../../../src/ameesh/storage/postgres/work.py)
  fournissent estimations au début du travail et temps écoulés jusqu’à la
  première intégration effective ; les reçus exacts et les
  [désignations](../../../src/ameesh/review_designation_pg.py) conservent
  les familles historiques des auteurs et relecteurs.
- [turn_costs](../../../src/ameesh/storage/postgres/costs.py),
  [cost_attribution.py](../../../src/ameesh/cost_attribution.py),
  `ameesh cost report|turns|attribution|gauges` et
  `ameesh accounts list` existent. Coût par tour, attribution au lot et
  jauges de comptes sont des objets différents. Les comptes courants ne
  permettent pas de reconstruire un effort ou une famille historiques.
  Aucun secret ni fichier privé de comptes n’a été ouvert.
- Le journal `turn.context`, produit par
  [runner._publier_contexte](../../../src/ameesh/runner.py), permet de
  compter des clôtures normalisées. La mémoire
  [packet-unit-red](../../../src/ameesh/packet_unit_red.py) et
  [paquet-auto](../../../scripts/paquet-auto.py) garde certaines sources
  retirées pour tests rouges. Ces deux observations ont une couverture
  temporelle partielle ; elles ne remplacent pas un registre de défauts.

## Périmètre, unités et qualité des données

**Fenêtre W : [2026-10-27 00:00 UTC, 2026-11-10 00:00 UTC[**, soit 14 jours
complets. Extraction en lecture seule de `public` le 10/11 à
**05:58:53 UTC**, transaction `REPEATABLE READ`, `read_only=True`,
borne de requête 30 s. Les états et tailles sont ceux du snapshot d’extraction,
pas une reconstruction historique à minuit. La vérification indépendante
de la requête d’annexe a lieu ensuite sur les mêmes bornes.

Trois grains restent séparés :

1. **Activité pendant W**, tous projets du mesh pour le livre comptable :
   **29 765 lignes/tours au total, dédupliqués par turn_id au sein de chaque famille** ;
   13 094 Claude, 6 540 Codex, 10 131 DeepSeek. Les lignes dont le lot
   appartient à ameesh sont respectivement 4 791, 2 965 et 3 507.
   Le reste comprend d’autres projets et les tours sans lot.
2. **Unités ameesh intégrées pendant W**, hors porteurs
   `integration/**` et `packet/**` : **985 unités**, 940 ratios connus.
   Le réel est défini comme dans les estimations : première fusion non
   annulée ou livraison, moins `started_at`, en temps écoulé.
   Les 255 porteurs sont séparés : leur ratio médian est 0,274 contre
   1,574 pour les unités. Les mélanger ferait paraître l’évolution
   artificiellement plus rapide.
3. **Unités livrées attestées pendant W** : les 30 événements
   `staging.attested` d’ameesh citent 395 identifiants distincts.
   Après rapprochement, **392 unités** ont une intégration effective
   antérieure ou égale à au moins une de leurs attestations.
   Une unité avait été intégrée avant W : elle reste dans cette cohorte
   de livraison. Les trois autres références sont séparées :
   un porteur ; #1606 sans ligne éligible selon la règle d’historique ;
   #3392, dont les attestations de W précèdent la première intégration
   effective retenue et dont l’état extrait est `blocked`.
   Cette incohérence ne prouve pas la livraison de #3392.
   **Une attestation compatible est une preuve enregistrée, pas une nouvelle
   recette physique ni une preuve que tout le code du lot est activé.**

Le chiffre « livré » ci-dessous désigne exclusivement cette dernière cohorte.
Les 18 anciens jalons `delivered` ne sont pas utilisés comme unique source :
la livraison par vagues est aussi attestée dans le journal.

**Famille = harnais exécuté**, jamais le nom d’un agent.
Pour un tour, on lit `turn_costs.harness` ; pour un verdict, le reçu exact
`milestone:<id>` ; pour les auteurs d’une unité, la dernière désignation
`designated` antérieure à sa première intégration, avec tous ses
`authors[].family|families` et `unknown_authors`.
Plusieurs familles prouvées donnent `mixed` ; une attribution absente ou
incomplète donne `unknown`. Le harnais du poseur de gel ne prouve pas à lui
seul les familles de tous les auteurs. Les changements d’auteur en cours de
lot restent un biais de cette attribution finale.

**Classes et dimensions.** Les sources structurées lues ne persistent pas
le risque calculé au gel : les désignations gardent politique, auteurs et
quorum, mais pas `risk`. Un quorum 2 ne prouve pas la classe sensible
(plancher explicite possible) et un quorum 0 ne prouve pas léger.
Le risque est présent dans le corps de certaines notifications, délibérément
non lu. Reclasser aujourd’hui tous les diffs avec le canon courant serait
une autre mesure. Les **392 classes restent inconnues** ; aucun lot n’est
rangé léger ou sensible par supposition. `type` et `change_size` sont
déclaratifs, relus dans le snapshot, et la taille manque pour **224/392** lots.

**Coût et jetons.** Le coût par lot livré somme les lignes attribuées au
lot entre son début et sa première attestation compatible, toutes familles
de contribution et de revue confondues. Il exclut les tours sans attribution,
le temps machine des outils, les coûts du porteur et les régressions traitées
après cette attestation : c’est un coût comptable attribué, pas un coût total
de possession. Des contributions de plusieurs harnais existent sur la grande
majorité des lots : le prix du lot n’est pas le prix du seul auteur.

Les USD du livre sont des estimations de consommation, **pas une facture ni
une dépense marginale comparable entre forfaits et paiement au jeton**.
Pas de projection d’économie en argent à partir de ces tableaux.
L’entrée Codex inclut son cache lu ; Claude et DeepSeek le publient à part
(convention documentée par `TurnCosts.session_total`).
L’entrée relue affichée vaut donc `input_tokens` pour Codex et
`input_tokens+cached_input_tokens` pour les deux autres.
Le cache écrit est conservé séparément dans la source et non ajouté aux
relectures. Une somme brute `input+cache` des trois harnais compterait
deux fois le cache Codex. Les sorties ne constituent pas une note de qualité.

## Mesures par famille, type et taille

### Coût et volume de travail observés par unité livrée attestée

Les moyennes de tours/jetons/coût ont le dénominateur **n lots** ;
les ratios utilisent uniquement le nombre de ratios connus. L’effort
`low|medium|high|…` reste **inconnu** : aucun champ historique exécuté
ne l’atteste dans le livre ou les événements retenus. Les étiquettes de
modèle du livre sont `claude-opus-5-5`, `gpt-6.1-sol` et
`deepseek-flash` sur toute W, une seule par harnais.
On ne peut donc séparer effet modèle et effet harnais, ni comparer deux
niveaux d’effort. Il s’agit d’étiquettes comptables observées, pas d’une
validation de l’identité fournisseur pour chaque appel.

| Famille des auteurs | Lots / ratios connus | Réel/estimé médian ; p80 | Tours / lot | Entrées relues M / lot | Sorties k / lot | USD moyen ; médian / lot |
| --- | --- | --- | --- | --- | --- | --- |
| claude | 103 / 99 | 2,17 ; 4,01 | 8,57 | 18,12 | 121,67 | 4,13 ; 2,44 |
| codex | 71 / 69 | 1,87 ; 3,98 | 13,65 | 31,71 | 172,67 | 3,14 ; 2,11 |
| deepseek | 199 / 198 | 1,99 ; 4,65 | 8,60 | 38,74 | 133,13 | 1,40 ; 0,80 |
| mixed | 15 / 12 | 1,43 ; 3,99 | 9,93 | 27,39 | 114,42 | 2,04 ; 1,73 |
| unknown | 4 / 4 | 1,95 ; 9,78 | 5,25 | 15,33 | 86,37 | 2,07 ; 2,16 |


Tous les lots de ce tableau ont une classe historique inconnue.
Les différences observées justifient de préparer des comparaisons contrôlées,
pas de baisser une exigence de revue ou de changer une famille en production.

### Reprises des auteurs

Un B est une **ligne de verdict `blocked` déclarée** avant intégration ;
il n’est pas automatiquement un défaut prouvé ni un verdict admis par la porte.
Les lignes répétées restent comptées comme travail de coordination.
Le nombre de lots avec au moins un B évite de faire passer les doublons pour
des défauts distincts. Un regel compte un SHA textuel de gel distinct après
le premier, entre début et intégration ; un gel répété du même texte SHA
ne compte pas. L’équivalence de préfixes Git et les gels omis ne sont pas
reconstruits ici.

| Famille des auteurs | B déclarés reçus | Lots avec ≥1 B / lots | Regels distincts / lots concernés |
| --- | --- | --- | --- |
| claude | 90 | 28 / 103 | 98 / 38 |
| codex | 69 | 29 / 71 | 112 / 42 |
| deepseek | 154 | 59 / 199 | 181 / 81 |
| mixed | 15 | 5 / 15 | 27 / 12 |
| unknown | 0 | 0 / 4 | 0 / 0 |


Ces B et regels ne sont pas des notes de compétence : complexité, sévérité,
relecteur, politique et corrections croisées diffèrent entre lots.

### Croisement famille × type

Classe historique inconnue pour toutes les cellules. `improvement`
correspond à amélioration ; `evolution` à évolution.

| Famille | Type | Lots / ratios connus | Médiane ; p80 | Tours / lot | USD moyen / lot |
| --- | --- | --- | --- | --- | --- |
| claude | bug | 42 / 40 | 2,42 ; 3,76 | 9,17 | 4,19 |
| claude | evolution | 42 / 41 | 1,48 ; 2,75 | 8,57 | 4,76 |
| claude | improvement | 19 / 18 | 3,94 ; 13,04 | 7,26 | 2,58 |
| codex | bug | 26 / 26 | 1,94 ; 3,45 | 10,15 | 2,48 |
| codex | evolution | 36 / 36 | 1,53 ; 4,08 | 17,06 | 3,85 |
| codex | improvement | 9 / 7 | 3,80 ; 9,70 | 10,11 | 2,17 |
| deepseek | bug | 72 / 72 | 1,64 ; 2,75 | 7,32 | 1,27 |
| deepseek | evolution | 67 / 67 | 1,46 ; 3,35 | 10,97 | 1,97 |
| deepseek | improvement | 60 / 59 | 3,84 ; 8,14 | 7,50 | 0,93 |
| mixed | bug | 6 / 6 | 2,63 ; 4,31 | 7,17 | 2,30 |
| mixed | evolution | 5 / 5 | 0,99 ; 2,44 | 10,60 | 2,37 |
| mixed | improvement | 4 / 1 | 1,85 ; 1,85 | 13,25 | 1,25 |
| unknown | bug | 2 / 2 | 1,65 ; 2,07 | 3,00 | 2,16 |
| unknown | evolution | 1 / 1 | 1,54 ; 1,54 | 9,00 | 2,22 |
| unknown | improvement | 1 / 1 | 20,92 ; 20,92 | 6,00 | 1,74 |


### Croisement famille × taille

`s/m/l` sont les tailles déclarées ; `unknown` reste visible.
Les cellules de 1 à 2 lots ne soutiennent aucune recommandation.

| Famille | Taille déclarée | Lots / ratios connus | Médiane réel/estimé | USD moyen / lot |
| --- | --- | --- | --- | --- |
| claude | l | 2 / 2 | 0,53 | 6,29 |
| claude | m | 18 / 18 | 1,59 | 5,64 |
| claude | s | 32 / 32 | 2,79 | 2,61 |
| claude | unknown | 51 / 47 | 2,16 | 4,46 |
| codex | l | 2 / 2 | 0,89 | 6,76 |
| codex | m | 14 / 14 | 1,84 | 3,48 |
| codex | s | 8 / 8 | 3,07 | 0,91 |
| codex | unknown | 47 / 45 | 1,79 | 3,26 |
| deepseek | l | 1 / 1 | 3,66 | 5,01 |
| deepseek | m | 23 / 23 | 1,14 | 2,76 |
| deepseek | s | 60 / 60 | 3,78 | 1,20 |
| deepseek | unknown | 115 / 114 | 1,66 | 1,21 |
| mixed | m | 2 / 2 | 0,65 | 1,95 |
| mixed | s | 4 / 4 | 1,34 | 1,88 |
| mixed | unknown | 9 / 6 | 3,50 | 2,13 |
| unknown | s | 2 / 2 | 10,93 | 1,96 |
| unknown | unknown | 2 / 2 | 1,95 | 2,18 |


Le croisement complet famille × type × taille est conservé en annexe ;
les cellules absentes sont sans observation, pas des performances nulles.

### Estimations : distinguer le biais de planification de la vitesse du modèle

`ameesh work estimates --app ameesh --json` a été consulté ; sa limite
`HISTORY_LIMIT=1000` ne couvre pas toute W. Sa vue globale (990 ratios,
médiane 1,031 au relevé) inclut des porteurs et une période différente ;
elle ne remplace pas la cohorte fixe. La table de référence publiée par
l’auditeur est lisible via `ameesh work show` ; cette étude ne la republie pas.

Sur les **985 unités intégrées** de W : 940 ratios comparables,
45 sans estimation antérieure exploitable (dont une sans début mesuré),
médiane 1,574 ; p80 3,450. Médianes par type :
bug 1,459 (382 ratios), évolution 1,377 (348), amélioration 2,971 (210).
Les petits paniers d’amélioration peuvent attendre une coupure plus longtemps
que leur estimation d’écriture ; le ratio mesure l’organisation entière.

Biais par **auteur de l’estimation**, et non par famille d’exécution
(seuil d’affichage 10 ratios ; les autres restent dans les agrégats) :

| Auteur de l’estimation | Lots comparables | Ratio médian ; p80 |
| --- | --- | --- |
| mesh-design | 517 | 1,47 ; 2,80 |
| auditeur | 158 | 1,47 ; 3,05 |
| mty-amh-docs-01 | 39 | 2,43 ; 4,93 |
| reference | 37 | 0,91 ; 1,31 |
| mty-amh-impl-03 | 27 | 3,59 ; 9,43 |
| bureau | 25 | 1,54 ; 2,63 |
| ameesh-dev3 | 19 | 2,06 ; 4,16 |
| mty-amh-impl-12 | 11 | 1,11 ; 7,10 |
| mty-amh-qa-01 | 11 | 5,56 ; 7,68 |
| mty-amh-impl-11 | 10 | 5,93 ; 15,65 |
| mty-amh-impl-13 | 10 | 1,34 ; 2,47 |
| orchestrateur | 10 | 1,42 ; 1,66 |


Le biais du rédacteur de cette étude, `mty-amh-impl-11`, figure dans le
même tableau. L’auteur ne s’évalue pas par une autre règle.

## Relecture : activité mesurée, justesse encore inconnue

Sur W, lots ameesh de toutes cohortes : reçus exacts des verdicts, familles
historiques ; sièges notifiés relus seulement dans
`agent_mailbox.payload.review_designation`, jamais dans le corps.

| Famille du reçu de verdict | Sièges notifiés | B | OK | B / lignes de verdict | B justes |
| --- | --- | --- | --- | --- | --- |
| claude | 1048 | 182 | 907 | 16,7 % | inconnu |
| codex | 1015 | 291 | 865 | 25,2 % | inconnu |
| deepseek | 895 | 247 | 660 | 27,2 % | inconnu |

71 notifications ont une famille vide ou absente ; elles restent hors des
trois lignes et ne sont pas affectées au registre actuel. Les sièges notifiés
dans W et les verdicts rendus dans W ne forment **pas le même entonnoir** :
revue commencée avant W, réponse après W, correction et changement de gel
peuvent déplacer les événements. Aucun taux de réponse n’en est déduit.

Les **720 B déclarés** ne donnent ni précision ni rappel du relecteur :
un B levé par un OK peut avoir conduit à une vraie correction, un retrait
de remarque ou un changement de périmètre ; un lot fusionné ne tranche pas.
Pour mesurer un « B juste », il faut un constat identifiable, son témoin
sur le SHA lu, une résolution distincte et son adjudication indépendante.
Les défauts manqués après fusion nécessitent aussi un lien au lot d’origine.
Le nombre de B justes et de faux positifs reste inconnu pour les trois familles.

## Rouges de paquet et tours au plafond

### Rouges : quatre mémoires de retrait, pas 91 défauts d’auteur

Le journal CI de `manaty/ameesh` contient dans W, par première apparition :
453 runs success, 91 failure, 38 cancelled, 64 superseded.
Ce sont des **runs**, pas des lots fautifs ni des défauts uniques ; plusieurs
runs/workflows peuvent porter le même arbre. Une panne d’infra, un test
instable et une faute de code n’ont pas la même signification.

Quatre notes structurées `packet-unit-red` citent une source précise :
#6183 et #6372, reçus de commits Codex ; #6228 et #6520, reçus DeepSeek.
Leurs sources distinctes portent respectivement 7, 1, 1 et 1 noms de tests.
Ce sont quatre mémoires de sources retirées, pas dix régressions :
un ensemble de tests peut avoir une cause commune.
**Zéro note Claude observée n’est pas zéro rouge Claude.**
La mémoire n’offre ni couverture exhaustive de W ni adjudication finale
(code, banc, interaction, non-concluant). Les marqueurs sont une borne basse ;
aucun classement de qualité ou taux de rouges par famille n’est calculé.

### Plafonds : source plus précise que les fins de session

`persona_sessions.end_reason` contient surtout `rotation` ou
`replaced by …` : cela ne suffit pas à compter les tours au plafond.
`turn.context.decision.close.kind` nomme en revanche
`duration`, `context_cap` et `reread_budget`.
Les premières observations retrouvées dans W commencent le **31/10 vers
01:05 UTC**, pas le 27/10. Compteurs globaux du mesh, toutes classes/projets :

| Harnais exécuté | Tours observés | Clôture durée | Clôture contexte | Clôture relecture | Rotation taille session |
| --- | --- | --- | --- | --- | --- |
| claude | 7927 | 59 | 42 | 16 | 67 |
| codex | 3129 | 11 | 0 | 0 | 672 |
| deepseek | 4946 | 151 | 11 | 0 | 457 |


Les rotations de session ne sont pas ajoutées aux clôtures : elles peuvent
survenir sur un même tour. Les clôtures observées sont 117/7 927 pour Claude,
11/3 129 pour Codex et 162/4 946 pour DeepSeek ; ce ne sont pas des taux sur
les 14 jours complets, ni des défauts. Les seuils de politique, modèles,
rôles et tailles de session diffèrent ; une clôture peut être la bonne
application d’un garde. Un tour sans observation n’est pas un tour sous plafond.

## Recommandations et incertitude

| Classe de travail | Proposition à instruire, sans changement immédiat | Preuve disponible et limite |
| --- | --- | --- |
| Implémentation légère, petit correctif, contenu/test isolé | Prioriser DeepSeek comme candidat de comparaison à coût comptable contenu ; conserver la revue indépendante existante | Coût attribué moyen 1,404 USD sur 199 lots attestés ; classe historique inconnue et revue incluse, donc aucune preuve de qualité égale |
| Implémentation sensible, SQL, sécurité, concurrence, porte de fusion | Maintenir les gardes et comparer Codex/Claude/DeepSeek sur des tâches de référence identiques avant toute nouvelle préférence | Octobre donne des pistes ; W ne distingue pas léger/sensible et ne mesure pas les défauts manqués |
| Diagnostic et revue avec contre-exemple exécutable | Inclure Codex dans un banc de relecture à défauts connus, à côté des autres familles | 291 B déclarés ne prouvent ni 291 B justes ni un rappel supérieur ; missions de revue non aléatoires |
| Conception, arbitrage et étude | Garder la famille prévue par la gouvernance actuelle ; évaluer à part la qualité de la décision et les corrections ultérieures | Une durée de lot et une note de revue de code ne mesurent pas la qualité d’une conception ; aucune nouvelle préférence Claude n’est démontrée |
| Relecture légère ou sensible | Proposer seulement des candidats indépendants éligibles, puis départager à couverture comparable ; jamais contourner familles exclues ou plancher | Les trois familles rendent des B et OK ; justesse et rappel inconnus |
| Réglage de l’effort | Ne rien conclure en faveur de low/medium/high à partir de W ; tracer l’effort effectif d’abord | Champ historique exécuté absent ; le catalogue et les réglages courants ne le remplacent pas |

Les propositions sont des hypothèses de mesure, pas une politique adoptée.
La [décision 0020](../decisions/0020-catalogue-et-evaluation-des-modeles.md)
place la recommandation à côté de la politique canon, et 0023 demande de
préserver la qualité. Cette étude ne justifie aucune économie promise,
augmentation de dépense ou campagne d’évaluation.

**Biais déterminants.** Attribution des lots non aléatoire ; spécialisation
des agents et des relecteurs ; gouvernance différente ; variantes d’effort
et version du harnais non jointes ; correction par un tiers ; coût des revues
porté par le lot ; mélange de temps de calcul, attentes CI/revue/coupures ;
sélection des seuls lots intégrés ou attestés (survivants) ; absence des échecs
abandonnés ; coûts sans lot et régressions après livraison exclus ;
classes historiques manquantes ; tailles incomplètes.
Les lectures ne permettent pas de corriger tous ces biais par pondération.
Aucun p-value ni intervalle de « confiance dans la famille » n’est inventé.

La sous-cohorte attestée n’est pas l’ensemble de ce qui était réellement actif :
l’absence d’attestation compatible est une absence de preuve dans ce relevé.
La divergence #3392 montre pourquoi même une liste structurée doit être
rapprochée des jalons. La cohorte d’intégration et la cohorte de staging restent
publiées séparément ; aucune différence entre elles n’est présentée comme
un effet de modèle.

## Découpage proposé au pilote

Unités **proposées**, non créées par cette étude. Estimations auteur avec
tests ciblés, hors attente et relecture, **≤ une journée chacune**.
Les noms de nouveaux fichiers sont indicatifs ; migrations additives à
réserver par le lot qui les réalise. Les champs inconnus restent rétrocompatibles
avec les écrivains N-1 ; aucun backfill par le registre actuel.
Les familles/capacités ci-dessous sont des compétences possibles,
pas un nouveau routage ni un droit à consommer des jetons.

| Unité | Livrable utilisé et fichiers proposés | Tests ciblés et preuve attendue | Dépendances | Effort / capacité | Classe proposée |
| --- | --- | --- | --- | --- | --- |
| M1 | Rapport en lecture seule par projet/fenêtre, `model_evaluation_report.py`, entrée CLI existante d’observation ; ratios, staging, coût, B, règles de couverture et export de tableaux utilisés par l’auditeur | Fenêtre UTC fixe, ≥14 j sans limite silencieuse 1000, porteurs séparés, deux cohortes, SHA/famille historique, mixed/unknown, cache Codex, compte forfait ≠ facture, donnée absente ≠ zéro ; fixtures des requêtes ci-dessous | Aucune ; sources existantes | 4 h ; un auteur SQL/mesure, une revue autre famille | Léger pour calcul pur/lecture sans nouveau droit ; reclasser sensible si droits ou exposition privée |
| M2 | Trace du risque au gel : étendre la charge durable de désignation dans `review_designation_pg.py` et son lecteur ; `risk`, base/gel exacts, politique/source ; rendu immédiat dans M1 si disponible | Deux quorums identiques/deux classes, plancher explicite, tête changée, requalification, carried, absence N-1, aucune modification de quorum/familles | Aucune pour la trace ; M1 consomme les champs optionnels | 4 h ; auteur connaissant la porte, avis indépendants selon canon | Sensible (porte de fusion/consignes) |
| M3 | Résolution explicite d’un constat de revue et lien de régression, réutiliser `review_correction.py`, `work_item_links` et un journal additif si nécessaire ; M1 montre confirmé/infirmé/non tranché | B→OK sans correction reste non tranché ; plusieurs constats/même gel ; retrait de remarque ; témoin sur SHA lu ; correction par relecteur ; adjudicateur indépendant ; aucune nouvelle porte de fusion | M1 pour restituer, M2 pour stratifier ; modèle de constat relu avant migration | 5 h ; auteur données/revue + relecteur indépendant des familles jugées | Sensible (consignes, SQL si nécessaire) |
| M4 | Snapshot du réglage effectivement passé à l’adaptateur : effort/modèle/version/compte de facturation par identifiant non secret, qualité requested/effective/unknown ; `runner.py`, `cost.py`, `context_profile.py` ou charge `turn.context` existante | Repli de harnais, modèle demandé ≠ exécuté, effort non annoncé, compte emprunté, N-1, panne de publication sans blocage du tour, pas de secret ni corps de prompt ; M1 expose les trous | Aucune pour la publication ; M1 pour lecture immédiate ; raccord borné au format existant | 4 h ; auteur harnais/mesure, revue sensible | Sensible (harnais/comptabilité), pas d’action sur compte ni de nouveau prix |
| M5 | Tableaux descriptifs et sensibilité : croiser les B par famille de relecteur et famille d’auteur, stratifier les lots comparables, inclure abandonnés/ouverts comme cohortes distinctes, relier rouges aux sources et aux non-concluants ; `model_evaluation_report.py`, `packet_unit_red.py` lecteur ; pas de nouveau moteur de catalogue ou banc | Dénominateurs exacts, petites cellules/inconnus, fenêtres comparables, déduplication run/source/test, coût de revue et sans lot, cas #3392, pas de causalité inventée | M1 ; exploite M2–M4 sans les attendre pour les compteurs déjà connus | 3 h ; auteur analyse/données, audit des formules | Léger en lecture ; toute nouvelle persistance SQL devient sensible |
| M6 | Avis de routage indicatif à attribution/désignation : consommer les seuls tableaux qualifiés, afficher raisons et insuffisance ; intégration minimale dans `file_decoupage.py` / `review_designation_activation.py` après décision séparée | Inconnus/échantillon insuffisant = aucun conseil ; identité indépendante, exclusions auteurs/correcteurs et planchers inchangés ; mode observation n’écrit ni compte ni politique ; retour immédiat à l’ordre existant | M1, M5, 14 j de métadonnées utiles M2/M4 et bilan M3 ; **décision owner distincte avant activation d’un effet de routage** | 4 h ; auteur orchestrateur/porte, revues autres familles | Sensible (attribution/porte/consignes) |

Total indicatif **24 h auteur**. **M1, M2 et M4 sont démarrables
indépendamment après relecture**, avec réservation des empreintes partagées :
M2 et M6 touchent la désignation ; M1 et M5 le rapport.
Une capacité de trois auteurs au plus suffit au démarrage ; pas six agents
sur les mêmes fichiers. Le pilote peut regrouper les unités consécutives.

M1 livre un rapport exécuté et utilisé, pas seulement une bibliothèque ;
M2 et M4 publient à leur livraison sur les tours/gels réellement traités,
avec lecture compatible M1 dans la même vague. M3 livre des résolutions
consultables sans imposer de décision nouvelle à toutes les revues.
M5 fonctionne déjà sur les sources connues, puis enrichit la couverture.
**M6 n’est pas construit comme une tranche dormante** : tant que les
données et la décision n’existent pas, il reste une unité proposée.

Une campagne comparative ultérieure réutiliserait `replay` : tâche et tests
de référence isolés de la solution, ordre A/B varié, mêmes contraintes,
critères de défaut explicites, relecture anonyme indépendante, plusieurs
tâches par strate et bilan de contamination. Son plan, plafond en jetons/coût,
compte exact et autorisation d’exécution sont **hors de #6650**.
Aucune campagne payante ou installation d’hôte n’est cachée dans M1–M6.

## Mesure, activation, retour arrière

**Mesure initiale.** W et les tableaux ci-dessus constituent le relevé
initial, avec leurs trous. M1 conserve fenêtre, identifiants de cohorte,
SHA/politique, qualité des valeurs, numérateurs/dénominateurs et version de
calcul ; pas de corps de courrier, de prompt ou de secret.
Un rapport tronqué ne produit pas de conseil. Les comptes/réserves restent
contrôlés par les mécanismes existants ; aucun quota n’est relevé.

**Activation par livrable.** Rapport immédiatement utilisable en lecture ;
publication des snapshots M2/M4 à la version suivante de leurs producteurs,
migrations additives avant les exécuteurs le cas échéant. Les hôtes N-1
continuent de publier les anciennes charges : leur effort/classe restent
inconnus. Observer au moins **14 jours complets après activation des champs**
avant de proposer un routage fondé sur ces champs, avec couverture montrée
par strate. Les B non adjudiqués restent non tranchés même après 14 jours.
Les tailles lacunaires ne sont pas remplies par une supposition.

**Critère de décision.** Le pilote et l’owner relisent le coût attribué,
le temps de livraison, les défauts confirmés et manqués, la justesse des
revues et les causes de non-conclusion, par cellules comparables.
Petit effectif, couverture insuffisante ou contradiction = conseil absent
et question précisément nommée, pas bascule par défaut.
Les seuils de recevabilité et marges de non-dégradation sont à proposer
dans le lot de routage, puis versionner ; aucun seuil statistique en dur
propre à une famille n’est dicté ici.

**Retour arrière.** Les premiers lots sont observationnels :
revenir au producteur précédent laisse les champs absents et les lecteurs
compatibles ; aucune suppression d’historique ni migration destructive.
Une panne du rapport retire le conseil et conserve les preuves.
Si M6 est ensuite autorisé, désactiver l’avis rend l’ordre antérieur
d’attribution/désignation ; ni annulation de travail engagé ni modification
de compte. Mesurer explicitement disponibilité du rapport, couverture
des métadonnées et décisions avec/sans conseil ; ne pas appeler « gain »
la seule baisse du nombre de messages ou du coût brut.

## Annexe : croisement complet sur les 392 unités attestées

Classe historique = **unknown** dans chacune des 39 cellules observées.
Moyennes de tours/sorties/USD par lot ; ratios sur le seul sous-effectif connu.
Les familles mixtes et inconnues restent visibles.

| Famille | Type | Taille | n / ratios | Ratio médian | Lots avec B | Regels | Tours / lot | Sorties k / lot | USD / lot |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| claude | bug | m | 9 / 9 | 1,50 | 7 | 26 | 16,33 | 265,88 | 7,20 |
| claude | bug | s | 15 / 15 | 2,42 | 3 | 10 | 7,60 | 88,57 | 3,01 |
| claude | bug | unknown | 18 / 16 | 2,83 | 4 | 6 | 6,89 | 78,02 | 3,66 |
| claude | evolution | l | 2 / 2 | 0,53 | 1 | 2 | 8,00 | 147,10 | 6,29 |
| claude | evolution | m | 6 / 6 | 1,55 | 2 | 5 | 7,00 | 130,69 | 4,73 |
| claude | evolution | s | 7 / 7 | 2,83 | 1 | 8 | 7,29 | 115,94 | 3,55 |
| claude | evolution | unknown | 27 / 26 | 1,44 | 7 | 34 | 9,30 | 144,27 | 4,97 |
| claude | improvement | m | 3 / 3 | 2,98 | 1 | 1 | 5,00 | 123,56 | 2,81 |
| claude | improvement | s | 10 / 10 | 5,86 | 0 | 0 | 4,80 | 56,19 | 1,34 |
| claude | improvement | unknown | 6 / 5 | 3,03 | 2 | 6 | 12,50 | 114,66 | 4,54 |
| codex | bug | m | 7 / 7 | 1,99 | 2 | 8 | 12,43 | 148,99 | 2,60 |
| codex | bug | s | 4 / 4 | 2,32 | 0 | 1 | 5,00 | 24,12 | 1,00 |
| codex | bug | unknown | 15 / 15 | 1,89 | 7 | 22 | 10,47 | 155,86 | 2,82 |
| codex | evolution | l | 2 / 2 | 0,89 | 2 | 4 | 14,00 | 395,22 | 6,76 |
| codex | evolution | m | 6 / 6 | 1,34 | 4 | 14 | 14,50 | 275,36 | 3,85 |
| codex | evolution | unknown | 28 / 28 | 1,63 | 12 | 55 | 17,82 | 191,83 | 3,64 |
| codex | improvement | m | 1 / 1 | 8,02 | 1 | 5 | 26,00 | 402,48 | 7,45 |
| codex | improvement | s | 4 / 4 | 3,46 | 0 | 0 | 4,00 | 46,91 | 0,82 |
| codex | improvement | unknown | 4 / 2 | 8,28 | 1 | 3 | 12,25 | 94,62 | 2,21 |
| deepseek | bug | m | 6 / 6 | 0,90 | 2 | 2 | 5,83 | 140,36 | 1,47 |
| deepseek | bug | s | 16 / 16 | 2,16 | 5 | 13 | 7,38 | 126,72 | 1,31 |
| deepseek | bug | unknown | 50 / 50 | 1,61 | 17 | 39 | 7,48 | 119,13 | 1,23 |
| deepseek | evolution | l | 1 / 1 | 3,66 | 1 | 9 | 25,00 | 423,86 | 5,01 |
| deepseek | evolution | m | 15 / 15 | 1,24 | 8 | 40 | 13,93 | 236,02 | 3,36 |
| deepseek | evolution | s | 13 / 13 | 3,12 | 7 | 28 | 11,69 | 179,24 | 1,95 |
| deepseek | evolution | unknown | 38 / 38 | 1,45 | 12 | 33 | 9,18 | 137,90 | 1,36 |
| deepseek | improvement | m | 2 / 2 | 2,87 | 0 | 1 | 9,00 | 116,93 | 2,17 |
| deepseek | improvement | s | 31 / 31 | 5,22 | 2 | 3 | 6,61 | 87,22 | 0,84 |
| deepseek | improvement | unknown | 27 / 26 | 2,06 | 5 | 13 | 8,41 | 118,36 | 0,94 |
| mixed | bug | s | 3 / 3 | 0,82 | 1 | 3 | 4,67 | 98,32 | 2,05 |
| mixed | bug | unknown | 3 / 3 | 4,31 | 1 | 7 | 9,67 | 107,39 | 2,54 |
| mixed | evolution | m | 2 / 2 | 0,65 | 1 | 7 | 9,50 | 175,84 | 1,95 |
| mixed | evolution | unknown | 3 / 3 | 0,99 | 2 | 6 | 11,33 | 132,64 | 2,65 |
| mixed | improvement | s | 1 / 1 | 1,85 | 0 | 0 | 7,00 | 75,64 | 1,37 |
| mixed | improvement | unknown | 3 / 0 | — | 0 | 4 | 15,33 | 91,30 | 1,21 |
| unknown | bug | s | 1 / 1 | 0,94 | 0 | 0 | 2,00 | 125,72 | 2,18 |
| unknown | bug | unknown | 1 / 1 | 2,35 | 0 | 0 | 4,00 | 18,20 | 2,13 |
| unknown | evolution | unknown | 1 / 1 | 1,54 | 0 | 0 | 9,00 | 82,48 | 2,22 |
| unknown | improvement | s | 1 / 1 | 20,92 | 0 | 0 | 6,00 | 119,08 | 1,74 |


## Annexe : requêtes et calculs reproductibles

Les fragments ci-dessous sont des **lectures d’analyse**, pas des migrations
ni une commande d’activation. Profil applicatif `read_only=True`,
`statement_timeout_ms=30000`, transaction cohérente pour les tableaux joints.
Ne pas imprimer le DSN ni lire les corps de courrier.

La requête principale restitue les cinq lignes du tableau par famille.
Pour type/taille, ajouter `type, change_size` à la projection et au GROUP BY ;
les autres formules et filtres restent identiques. Pour la cohorte
d’intégration, remplacer `active_at IS NOT NULL` par
`finished_at >= (SELECT lo FROM bounds)` ; seules ses statistiques de temps
et de reprises sont comparées ici, pas ses coûts à des phases différentes.
`percentile_cont` utilise l’interpolation linéaire comme
`estimates.quantile`. Les arrondis d’affichage sont appliqués après agrégation.

```sql
WITH bounds AS (
 SELECT '2026-10-27T00:00:00Z'::timestamptz lo,
        '2026-11-10T00:00:00Z'::timestamptz hi
), staged AS (
 SELECT x.value::bigint lot,a.created_at at
 FROM ameesh_events a CROSS JOIN bounds b
 CROSS JOIN LATERAL jsonb_array_elements_text(a.data->'lots') x(value)
 WHERE a.type='staging.attested' AND a.project='ameesh'
   AND a.created_at>=b.lo AND a.created_at<b.hi AND x.value ~ '^[0-9]+$'
), cohort AS (
 SELECT w.id,w.type,w.change_size,w.branch,w.started_at,f.at finished_at,s.at active_at,
        e.minutes,e.estimated_by
 FROM work_items w CROSS JOIN bounds b
 JOIN LATERAL (
   SELECT min(m.at) at FROM work_item_milestones m
   WHERE m.work_item_id=w.id AND m.kind IN ('merged','delivered')
    AND (m.kind<>'merged' OR NOT agent_mesh_merged_annulled(m.work_item_id,m.at))
 ) f ON f.at IS NOT NULL
 LEFT JOIN LATERAL (
   SELECT min(st.at) at FROM staged st WHERE st.lot=w.id AND st.at>=f.at
 ) s ON true
 LEFT JOIN LATERAL (
   SELECT minutes,estimated_by FROM work_item_estimates e
   WHERE e.work_item_id=w.id AND e.at<=w.started_at ORDER BY e.at DESC,e.id DESC LIMIT 1
 ) e ON true
 WHERE w.app='ameesh' AND f.at<b.hi
  AND (f.at>=b.lo OR EXISTS(SELECT 1 FROM staged st WHERE st.lot=w.id))
  AND (w.state<>'closed' OR w.close_reason='delivered')
), attribution AS (
 SELECT c.*,d.payload,
 CASE WHEN c.started_at IS NOT NULL AND c.minutes>0 AND c.finished_at>=c.started_at
  THEN extract(epoch FROM c.finished_at-c.started_at)/60/c.minutes END ratio
 FROM cohort c
 LEFT JOIN LATERAL (
   SELECT substring(e.note FROM position(': ' IN e.note)+2)::jsonb payload
   FROM work_item_events e
   WHERE e.work_item_id=c.id AND e.note LIKE 'review-designation:%'
    AND e.created_at<=c.finished_at
    AND (substring(e.note FROM position(': ' IN e.note)+2)::jsonb)->>'state'='designated'
   ORDER BY e.created_at DESC,e.id DESC LIMIT 1
 ) d ON true
), families AS (
 SELECT a.*,CASE
  WHEN a.payload IS NULL OR coalesce(jsonb_array_length(a.payload->'authors'),0)=0
   OR coalesce(jsonb_array_length(a.payload->'unknown_authors'),0)>0
   OR af.missing THEN 'unknown'
  WHEN cardinality(af.families)=1 THEN af.families[1]
  WHEN cardinality(af.families)>1 THEN 'mixed' ELSE 'unknown' END family
 FROM attribution a
 LEFT JOIN LATERAL (
   SELECT array_agg(DISTINCT fam) FILTER(WHERE fam IS NOT NULL AND fam<>'') families,
    bool_or(coalesce(author->>'family','')='' AND
            coalesce(jsonb_array_length(author->'families'),0)=0) missing
   FROM jsonb_array_elements(a.payload->'authors') z(author)
   LEFT JOIN LATERAL jsonb_array_elements_text(
    coalesce(nullif(author->'families','[]'::jsonb),jsonb_build_array(author->>'family'))
   ) x(fam) ON true
 ) af ON true
), metrics AS (
 SELECT f.*,m.b,m.distinct_heads,m.refreezes,t.turns,t.usd,t.prompt,t.output
 FROM families f
 LEFT JOIN LATERAL (
  SELECT count(*) FILTER(WHERE kind='verdict' AND verdict='blocked') b,
   count(DISTINCT sha) FILTER(WHERE kind='frozen' AND sha IS NOT NULL) distinct_heads,
   greatest(0,count(DISTINCT sha) FILTER(WHERE kind='frozen' AND sha IS NOT NULL)-1) refreezes
  FROM work_item_milestones m WHERE m.work_item_id=f.id
   AND m.at<=f.finished_at AND (f.started_at IS NULL OR m.at>=f.started_at)
 ) m ON true
 LEFT JOIN LATERAL (
  SELECT count(*) turns,sum(usd) usd,sum(output_tokens) output,
   sum(input_tokens+CASE WHEN harness='codex' THEN 0 ELSE cached_input_tokens END) prompt
  FROM turn_costs c WHERE c.work_item_id=f.id AND c.recorded_at>=f.started_at
    AND c.recorded_at<=coalesce(f.active_at,f.finished_at)
 ) t ON true
)
SELECT family,count(*) lots,count(ratio) ratios,
 percentile_cont(.5) WITHIN GROUP(ORDER BY ratio) median_ratio,
 percentile_cont(.8) WITHIN GROUP(ORDER BY ratio) p80_ratio,
 sum(turns) turns,sum(usd) usd,avg(usd) mean_usd,
 percentile_cont(.5) WITHIN GROUP(ORDER BY usd) median_usd,
 avg(turns) mean_turns,avg(prompt) mean_prompt,avg(output) mean_output,
 sum(b) blocked_rows,count(*) FILTER(WHERE b>0) lots_blocked,sum(refreezes) refreezes
FROM metrics
WHERE active_at IS NOT NULL
 AND coalesce(branch,'') NOT LIKE 'integration/%'
 AND coalesce(branch,'') NOT LIKE 'packet/%'
GROUP BY family ORDER BY family;
```

Les notifications, verdicts, CI, plafonds et rouges sont comptés séparément :

```sql
-- period_costs
SELECT c.harness,count(*) n,count(c.work_item_id) with_lot,
 count(*) FILTER (WHERE w.app='ameesh') ameesh_rows,
 sum(c.usd)::float8 usd,sum(c.input_tokens) input,sum(c.cached_input_tokens) cache,
 sum(c.output_tokens) output,count(*) FILTER (WHERE c.model IS NULL OR c.model='') model_unknown,
 count(*) FILTER (WHERE c.void_reason IS NOT NULL) void_rows,
 count(DISTINCT c.turn_id) turn_ids
 FROM turn_costs c LEFT JOIN work_items w ON w.id=c.work_item_id
 WHERE c.recorded_at >= '2026-10-27T00:00:00Z'::timestamptz AND c.recorded_at < '2026-11-10T00:00:00Z'::timestamptz GROUP BY c.harness;

-- review_period
SELECT m.kind,m.verdict,coalesce(p.harness,'unknown') family,count(*) n,
 count(DISTINCT m.work_item_id) lots FROM work_item_milestones m
 JOIN work_items w ON w.id=m.work_item_id
 LEFT JOIN provenance_receipts p ON p.subject='milestone:'||m.id
 WHERE w.app='ameesh' AND m.kind IN ('frozen','verdict')
 AND m.at >= '2026-10-27T00:00:00Z'::timestamptz AND m.at < '2026-11-10T00:00:00Z'::timestamptz
 GROUP BY 1,2,3 ORDER BY 1,2,3;

-- designated
SELECT payload->'review_designation'->>'reviewer' family,
 count(*) mails,count(DISTINCT (payload->'review_designation'->>'lot',payload->'review_designation'->>'freeze',recipient)) seats
 FROM agent_mailbox WHERE payload ? 'review_designation'
 AND created_at >= '2026-10-27T00:00:00Z'::timestamptz AND created_at < '2026-11-10T00:00:00Z'::timestamptz
 AND work_item_id IN (SELECT id::text FROM work_items WHERE app='ameesh')
 GROUP BY 1 ORDER BY 1;

-- ci
SELECT status,count(*) runs,count(DISTINCT work_item_id) lots
 FROM ci_runs WHERE repo='manaty/ameesh'
 AND first_seen_at >= '2026-10-27T00:00:00Z'::timestamptz AND first_seen_at < '2026-11-10T00:00:00Z'::timestamptz GROUP BY status;

-- context
SELECT data->>'harness' family,data->'decision'->'close'->>'kind' close_kind,
 data->'decision'->'rotation'->>'kind' rotation_kind,count(*) n,
 count(DISTINCT data->>'turn_id') turn_ids,
 min(created_at) oldest,max(created_at) newest
 FROM ameesh_events WHERE type='turn.context'
 AND created_at >= '2026-10-27T00:00:00Z'::timestamptz
 AND created_at < '2026-11-10T00:00:00Z'::timestamptz GROUP BY 1,2,3 ORDER BY 1,4 DESC;

-- red_notes
SELECT e.work_item_id,e.note,extract(epoch from e.created_at)::float8 ts
 FROM work_item_events e JOIN work_items w ON w.id=e.work_item_id
 WHERE w.app='ameesh' AND e.note LIKE 'packet-unit-red: %%'
 AND e.created_at >= '2026-10-27T00:00:00Z'::timestamptz AND e.created_at < '2026-11-10T00:00:00Z'::timestamptz ORDER BY e.id;
```

Le nombre de B justes ne se déduit d’aucune de ces requêtes.
Pour les quatre sources rouges, vérifier le reçu exact par
`SELECT actor,harness FROM provenance_receipts WHERE subject='commit:' || :source_sha`.
Il atteste la famille du créateur du commit, pas la cause finale du rouge.

**Vérification.** Les agrégats sont calculés sur les lignes extraites puis
recalculés en SQL indépendamment : même cohorte de 392 unités, mêmes cinq
groupes, mêmes sommes de tours et d’USD et mêmes médianes (écart d’arrondi
seulement). Sommes des groupes et cellules rapprochées des totaux, coût
post-attestation exclu, champs inconnus conservés. Les incohérences de cohorte
sont publiées ci-dessus. Pas de suite produit ou CI lancée pour cette étude
documentaire ; la relecture des deux autres familles juge les inférences et
le découpage, pas un classement de sa propre famille.
