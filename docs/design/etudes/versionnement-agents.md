---
type: Study
status: draft
title: "Versionnement pour agents : contrats composés et attestations de tests"
---

# Versionnement pour agents : contrats composés et attestations de tests

## Décision et résultat attendu

Étude du lot #6173, autorisée par la décision owner #198158, option a,
réponse signée de human:smichea du 07/11/2026 à 13:50 UTC, approbation #242.
La preuve est relisible par `ameesh decide show 198158 --json` :
`proof_check.ok = true`. Le pilote crée les lots après relecture de cette
étude ; ce document ne les crée pas et n'active aucune nouvelle porte.

Le résultat recherché est de détecter un contrat cassé sur l'arbre composé
avant de consommer un cycle de CI complète, et de savoir exactement quelle
preuve peut être conservée quand cet arbre change. Git reste le support des
branches, SHA, revues, PR et fusions. Ni changement de VCS, ni prod, argent
ou secrets dans ce chantier.

## Constat et références

Le relevé du pilote du 07/11, repris dans #6173 et dans la décision #198158,
compte quatre paquets rouges ou amputés d'un lot, soit environ quatre cycles
de CI perdus. Ce sont des cycles, pas une mesure de minutes ni quatre défauts
indépendants :

| Lot | Paquet / contrat | Effet relevé |
| --- | --- | --- |
| #6091 | h-09, `test_4259_client_literals` | Lot écarté deux fois |
| #6090 | h-12, `test_migrations_additives` | Lot écarté ; faux positif sur un `COMMENT ON`, corrigé par #6153 |
| #6090 | h-13, `test_l107_executor_contract` | Lot écarté ; `work.by_unique_ref` non classé dans le contrat médié |

Les deux retraits de #6091 et les deux de #6090 constituent le total fourni ;
le relevé ne donne pas un identifiant de run distinct pour chaque retrait.
Il faut conserver cette limite plutôt que fabriquer quatre durées.
`test_l107_executor_contract` est absent de `tests/parts/toujours.txt` à la
base étudiée ; #6147 y a ajouté `test_migrations_additives` au cas par cas.
Le premier incident de #6090 rappelle qu'un rouge peut venir du test : une
sélection plus large ne corrige pas un classificateur erroné.

Lecture du code sur `origin/main` à `e4c2576d1` :

- [paquet-auto.py](../../../scripts/paquet-auto.py) : `contract_modules`
  charge la liste de contrats ; `check_local_tests` exécute contrats,
  modules propres et voisins. La boucle de publication pousse, gèle et
  demande la CI complète avant cette vérification (#4304). Les freeze-checks
  et le contrôle des types clients précèdent déjà la poussée.
- [paquet-horaire.py](../../../scripts/paquet-horaire.py) : pilote la
  composition, la publication et la reprise sur résultat CI ; `publish`
  demande la CI complète. Plusieurs chemins peuvent donc publier : changer
  seulement l'ordre dans un appelant serait insuffisant.
- [test-plan.py](../../../scripts/test-plan.py) et
  [test-parts.py](../../../scripts/test-parts.py) : sélection commune P0/P1
  pour lots, contrats issus de `toujours.txt`, repli complet en cas de doute ;
  suite complète sur paquet, main et release. `pilote-psql.txt` complète
  l'exécution psql ; ce n'est pas le registre des contrats sémantiques.
- [Étude L161](l161-tests-par-impact.md) : la carte mesurée test → code
  couvre blocs exécutés, fichiers lus, répertoires listés et tables touchées.
  Elle aide à choisir et attribuer, sans démontrer la causalité d'un rouge.
- [prevalidation.py](../../../src/ameesh/prevalidation.py),
  [stockage](../../../src/ameesh/storage/postgres/prevalidation.py) et
  [étude de prévalidation](1770-prevalidation-au-gel.md) : rapport lié à une
  tête, un plan, un snapshot, des paramètres de canon et un environnement.
  Réutiliser ce support avant d'introduire une nouvelle table de preuves.

## Axe 1 — vérifier les contrats de la composition avant publication

**Proposition.** Après composition et freeze-checks, calculer la sélection
sur le diff base → arbre intégré, puis exécuter ses contrats sur cet arbre
exact, dans un environnement isolé et avec `ameesh heavy` pour la série.
Les tests locaux par unité gardent leur rôle de diagnostic ; la CI complète
reste obligatoire sur le paquet final. Une attestation locale ne la remplace
pas. Lancer les contrats une seule fois, puis fournir leur rapport à
`check_local_tests`, évite le doublon dans la même tentative.

Le plan et son résultat sont figés avant de pousser la branche, de geler et
de demander `ci-full`. `paquet-horaire.py` relit la même preuve dans ses
chemins de publication/reprise ; une attestation absente ne doit pas être
transformée en vert par un chemin alternatif. Le manifeste de composition
reste traçable dès sa construction ; la publication attend seulement cette
passe bornée. Les suites par unité peuvent encore tourner en parallèle de
la CI après publication. C'est le déplacement ciblé de #4304 décidé ici,
pas l'attente de toutes les suites locales avant poussée.

| Résultat de la passe | Action proposée |
| --- | --- |
| Vert complet sur le contenu attendu | Publier puis lancer la CI complète |
| Échec de test concluant | Diagnostiquer sur base et préfixes ; recomposer sans le lot démontré fautif, puis revérifier les contrats |
| Interaction entre lots ou cause inconnue | Nommer les candidats et transmettre au pilote ; ne pas accuser un lot par la seule couverture |
| Non concluant : quota, créneau, interruption, délai, environnement indisponible | Consigner les modules non joués et publier avec CI complète ; jamais une attestation verte |
| Arbre changé pendant l'exécution | Résultat hors portée ; recalculer pour la nouvelle composition dans le budget restant |

Un échec concluant non attribué suspend cette tentative de publication,
avec rapport exploitable et reprise par notification ; il ne justifie ni une
boucle d'attente ni la mise en hold des autres lots du projet. La règle canon
§3 sur les non-concluants continue de s'appliquer.

Le budget englobe attente du créneau, exécution et recompositions. Il vient
du canon compilé, comme les paramètres `packet_tests` existants ; aucun
délai spécifique à un client dans les scripts. La proposition initiale est
une cible de deux minutes pour cette passe, à confirmer par mesure, et une
borne canon qui préserve la publication sous trente minutes (§6). Une
expiration est non concluante et ne devient pas une nouvelle retenue.

**Coût et risque.** Deux lots d'intégration de 2 à 3 h après le lecteur de
sélection. Surcoût : durée des seuls contrats avant la CI ; gain attendu :
moins de recompositions après démarrage de CI. Risques : lancer des tests
sur une mauvaise tête, doubler les séries, laisser un ancien chemin publier,
retenir indéfiniment un paquet. Les témoins doivent exercer ces quatre cas.

## Axe 2 — choisir les contrats par le diff

**Proposition.** Un registre versionné, déclaratif, relie des chemins et
opérations de changement aux modules de contrat. La politique est déclarée
par projet, accessible via le canon compilé ; le registre du dépôt porte les
chemins du produit. Aucun nom de client ni chemin d'un autre projet dans le
moteur. Première application à ameesh, puis application via l'adaptateur de
plan de chaque autre projet, conformément au canon §7.

Exemples de règles à valider, pas encore une configuration active :

| Diff concerné | Contrats à inclure au minimum |
| --- | --- |
| `storage/interface.py`, stockage médié, `mediated_executor/contract.json` | `test_l107_executor_contract` |
| Ajout/modification sous `migrations/`, moteur de migrations | `test_migrations_additives`, `test_migrations`, contrat colonnes/lecteur N-1 existant |
| Parseur CLI et déclarations de commandes | Contrat CLI existant et `test_4259_client_literals` |
| Rôles SQL / contrat superviseur | Modules de droits/rôles/colonnes actuellement dans `toujours.txt` |
| Registre ou moteur de sélection lui-même | Validation du registre et sélection conservatrice de tous les contrats connus |

Le module exact de chaque règle est validé contre l'inventaire des tests au
SHA candidat. Une règle inconnue, un module absent ou une couverture de
chemin ambiguë produit une sélection conservatrice explicitement motivée,
jamais zéro contrat. Renommage = ancien et nouveau chemin ; suppression =
ancien chemin ; fichier nouveau non couvert = repli sur l'ensemble des
contrats, et sur la suite complète lorsque le plan L161f l'exige déjà.
Le registre doit savoir déclarer une règle universelle pour les invariants
réellement globaux. Le diff inclut tests, données, manifests et configuration,
pas seulement les fichiers Python.

Combinaison : contrats déclarés pour le diff ∪ contrats universels ∪ P0/P1
issus de la carte L161e. La carte enrichit la sélection mais ne retire jamais
un contrat déclaré. Le sélecteur produit pour chaque module ses raisons,
les chemins déclencheurs et le digest du registre. Les consommateurs du gel,
du paquet et de CI utilisent le même plan sérialisé. Une carte absente ou
périmée suit le comportement conservateur existant de chaque consommateur ;
ce chantier ne change pas silencieusement les règles de fraîcheur de L161f.

**Transition.** Au début, union avec `toujours.txt`, comparaison en mode
observation et aucun retrait de couverture. Après équivalence constatée,
chaque entrée de la liste reçoit une règle ou le statut universel, puis la
liste devient une projection générée pour les anciens lecteurs. Son retrait
physique attend la migration de tous les consommateurs. Une politique plus
sélective ne réduit jamais la CI complète du paquet ou de release.

**Coût et risque.** Trois lots de 2 à 3 h : lecteur, règles initiales,
intégration aux plans. Le risque principal est un faux négatif ; les fixtures
reprennent les trois régressions du relevé et les changements sans couverture.
Un registre explicite reste à maintenir : la validation de complétude et la
carte réduisent les oublis, sans promettre une découverte sémantique parfaite.

## Axe 3 — attestation par contenu, portée et invalidation

**Proposition.** Étendre le rapport de prévalidation existant avec une
attestation versionnée. Une attestation contient : schéma ; SHA source et
base ; arbre testé ; identifiant du changement ; ensemble ordonné des lots
et de leurs SHA ; plan et digest de sélection ; tests effectivement joués et
résultats ; canon/registre/runner de tests et leurs digests ; profil, pilotes,
version du schéma de base et fixtures ; exclusions ; horodatage et liens vers
les journaux. Le JSON canonique et son digest détectent un artefact altéré ;
ils ne constituent pas une signature d'autorité. Le producteur est celui du
rapport d'exécution existant, pas une déclaration libre de l'auteur.

L'arbre Git exact est la clé de réutilisation initiale. Le `patch-id` stable
est seulement un index pour retrouver une preuve candidate : il ignore du
contexte, et ne prouve ni la présence finale du contenu après revert, ni sa
compatibilité avec ses nouveaux voisins. La réutilisation entre arbres
différents reste exclue dans cette première tranche. Une éventuelle preuve
sur sous-arbre nécessiterait une étude complémentaire de ses dépendances.

| Événement | Sort de l'attestation |
| --- | --- |
| Nouveau SHA, même arbre, mêmes paramètres et tests | Réutilisation possible, provenance des deux SHA conservée |
| Regel identique | Réutilisation, sans exécution identique supplémentaire |
| Recomposition ou retrait de lot changeant l'arbre | Invalidation `tree-changed`, nouveau plan et rejeu |
| Résolution de conflit ou rebase modifiant le contenu | Invalidation même si le patch-id ou la liste des lots paraît inchangé |
| Revert conservant l'ascendance | Arbre comparé ; aucune preuve déduite de l'ascendance |
| Plan, tests, canon, registre, profil, pilotes, schéma ou fixtures changés | Invalidation nommée de la portée concernée ; rejeu avant nouveau vert |
| Test incomplet, sauté ou environnement absent | Statut non concluant ; aucune preuve verte pour ces modules |
| Expiration selon politique canon du projet | Rejeu ; l'âge maximal ne vient pas d'une constante de script |

Le manifeste étant lui-même un contenu Git, l'arbre attesté doit être
l'arbre final comprenant le manifeste déterministe. Le résultat est déposé
hors de cet arbre (artefact du rapport et référence dans l'état du lot/paquet),
ce qui évite une boucle où insérer le digest modifierait l'arbre attesté.
Une référence de preuve n'autorise jamais un arbre différent à fusionner.
Les avis de relecture et le contrôle de tête restent distincts de la preuve
de tests : cette attestation ne transporte pas un verdict humain.

**Coût et risque.** Deux lots de 2 à 3 h : format et vérificateur purs,
puis branchement au rapport existant et au manifeste. Privilégier le JSON du
rapport déjà stocké ; si son évolution exige une migration, la réserver dans
le lot concerné, additive et avec contrat N-1. Risques : cache trop permissif,
preuve non concluante présentée verte, confusion digest/signature, récursion
d'arbre ; les tests doivent les refuser explicitement.

## Mesure, activation et retour arrière

Comparer avant/après sur des paquets réellement composés : nombre de cycles
CI, retraits après lancement CI, contrats rouges captés avant publication,
durée et taux de non-concluants de la passe, réutilisations et invalidations
par motif. Les quatre cycles du relevé sont un point de départ, pas une
prévision de gain. L'objectif de validation est de capturer les régressions
connues avant CI, sans dépassement de budget ni baisse de couverture.

Déployer d'abord les lecteurs compatibles, puis les données du registre,
puis l'intégration en observation, enfin l'ordre de publication et la
réutilisation stricte. L'activation et le repli se règlent par politique
canon du projet. Repli : ancienne liste et fonctionnement #4304, sans
réutilisation d'attestation ; aucune attestation passée ne devient verte par
ce repli. Pas de nouvelle tranche dormante avant activation de la précédente.
Les choix de nouveaux paramètres canon font l'objet du lot correspondant,
avec compatibilité des lecteurs avant écriture de la politique.

## Découpage proposé au pilote

Estimations de travail auteur, tests ciblés compris, hors attente de revue.
La classe est celle des chemins proposés selon `ameesh review-class`, vérifiée
sur le canon courant ; les fichiers nouveaux seront reclassés sur leur diff
réel au gel. Les lignes sont des propositions, sans numéro de lot créé.

| Unité | Livrable et fichiers principaux | Tests ciblés attendus | Dépendances | Coût / capacité | Classe |
| --- | --- | --- | --- | --- | --- |
| A | Schéma et lecteur du registre ; nouveau `src/ameesh/contract_selection.py`, `mechanics.py` | Registre invalide, règle universelle, module absent, chemins inconnus, compatibilité canon N-1 | Aucune | 3 h, Codex ou DeepSeek | Sensible (canon) |
| B | Règles initiales et témoins ; nouveau registre sous `tests/parts/`, projection de `toujours.txt` | Fixtures #6090/#6091, renames, suppressions, union conservatrice, ordre déterministe | A | 2 h, DeepSeek | Léger pour données/tests seuls ; sensible si lecteur modifié |
| C | Plan unique ; `scripts/test-plan.py`, `prevalidation.py`, sélection de `paquet-auto.py` | Même sélection aux trois entrées, repli sans carte, contrat jamais retranché | A, B | 3 h, Codex | Sensible |
| D | Exécution composée avant publication ; `paquet-auto.py`, paramètres `packet_tests` de `mechanics.py` | Ordre contrats → push → ci-full, borne attente comprise, non-concluant, arbre modifié, absence de double exécution | C | 3 h, Codex | Sensible |
| E | Reprise et publication cohérentes ; `paquet-horaire.py`, politique canon du projet | Tous chemins de reprise, rouge non attribué, retrait/recomposition, budget épuisé, retour arrière | D | 2 h, DeepSeek | Sensible |
| F | Format d'attestation et validation pure ; nouveau `src/ameesh/test_attestation.py` | Arbre identique, patch-id insuffisant, revert, paramètres changés, artefact altéré, non-concluant | Aucune, format de rapport existant | 3 h, Codex ou DeepSeek | Sensible dès usage en porte ; reclasser le diff |
| G | Persistance/réutilisation ; `prevalidation.py`, stockage du rapport, `paquet-auto.py` | Regel même arbre sans rejeu, recomposition invalidée, manifeste final, lecteurs N-1 si format évolué | F, C, E | 3 h, Codex | Sensible |
| H | Adaptation aux autres projets via plan/registre déclarés et guide d'exploitation | Contrat du plan d'un second projet, aucune donnée client dans le moteur, activation et repli observés | C, E ; G pour attestations | 2 h par adaptateur, DeepSeek | Sensible (CI) |

A et F sont démarrables indépendamment après relecture de l'étude. B puis C
alimentent le chemin critique D → E ; F prépare G en parallèle de ce chemin.
Le pilote borne les empreintes communes (`mechanics.py`, `paquet-auto.py`,
`prevalidation.py`) lors de l'attribution. Le registre n'est pas un nouveau
moteur de fusion ; une attribution de faute fondée seulement sur la carte,
un DAG sémantique obligatoire ou une commutation automatique de patches sont
hors de cette première mise en œuvre.
