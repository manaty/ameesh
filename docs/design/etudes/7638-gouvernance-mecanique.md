---
type: Study
title: "Gouvernance du mesh Manaty — étude chiffrée de la part mécanique à mettre en code (lot #7638)"
description: "Étude du lot #7638 (issue manaty/ameesh#5829) : sur 7 jours de la base du mesh, les trois agents de gouvernance (orchestrateur, auditeur, mesh-design) font 3 759 tours (42,9 % du mesh) et 1 357 $ estimés (55,7 %) ; 8 520 messages reçus, 2 858 gestes pilotes dont 1 428 déjà émis hors tour ; un échantillon reproductible de 60 tours sans geste (20 par agent, tirage md5) donne 31 tours mécaniques (51,7 %) pour 30,3 % de leur coût. Quatre règles de code (porte de réveil sans conséquence, digest d'état, routage et désignation automatiques, suppression des reprises de paquet) retireraient environ 1 260 tours et 190 $ par semaine — les 257 étiquettes de continuité (90 $) sont un majorant, pas le gain ; un cinquième levier (fusion automatique Nexlink) reste à mesurer. Aucun code, aucun réglage modifié par ce lot."
status: draft
tags: [gouvernance, mecanique, tours, cout, canon, quorum, paquet, fusion]
generated: { by: "mty-amh-impl-01/deepseek", at: "2026-11-17T03:05:00Z" }
stale_after: 2027-02-17
sources:
  - { resource: "https://github.com/manaty/ameesh/issues/5829", title: "issue miroir du lot #7638 (ameesh)" }
  - { resource: "#7627", title: "spec validée par l'owner le 17/11/2026, §5 (répartition IMA / ameesh)" }
  - { resource: "base du mesh (AMEESH_DSN), tables turn_costs, ameesh_events, agent_mailbox, work_items, 2026-11-10 02:25Z → 2026-11-17 02:25Z", title: "population, courrier, gestes, étiquettes et échantillon" }
  - { resource: "7638-annexe-echantillon.md", title: "annexe reproductible : requête de tirage, 60 identifiants, classes, coûts et facteurs" }
  - { resource: "../../../src/ameesh/pilot_gesture.py", title: "journal et porte des gestes pilote (`pilot.gesture`)" }
  - { resource: "../../../src/ameesh/packet_automerge.py", title: "fusion automatique des paquets, déjà utilisée hors tour" }
  - { resource: "../../../src/ameesh/train_fire.py", title: "feu et file des paquets (orchestrateur)" }
  - { resource: "../../../src/ameesh/role_timers.py", title: "passes périodiques des rôles" }
  - { resource: "../../../src/ameesh/adapters.py", title: "consigne de synthèse de session (`SUMMARY_PROMPT`, ligne 114)" }
  - { resource: "../../../src/ameesh/reprise.py", title: "brief de reprise et de rotation, déjà déterministe en secours" }
---

# Gouvernance du mesh Manaty : quelle part mécanique mettre en code ?

## 1. Question et périmètre

La spec owner du 17/11/2026 (#7627) retient que la répartition IMA / ameesh ne
doit pas laisser au jugement d'un agent ce que le code peut faire. Le lot
#7638 demande une étude chiffrée, sans changement de comportement :

1. classer un échantillon représentatif des tours des trois agents de
   gouvernance — orchestrateur, auditeur, mesh-design — entre décision de
   jugement et geste mécanique (relais, accusé, routage, relance, contrôle
   d'état) ;
2. pour chaque classe mécanique, donner la règle de code qui la remplace, les
   tours et les dollars évités, le risque ;
3. proposer une liste de lots de suite avec leur gain attendu.

Périmètre : les trois agents `claude-opus-5-5` qui portent la gouvernance du
mesh Manaty. Hors périmètre : le canon lui-même, les fiches d'agent, la
répartition IMA / ameesh (§5 de la spec), et toute modification de code ou de
réglage. Les coûts Claude sont ceux du grand livre (`turn_costs.usd`), estimés
pour des sièges en forfait : le gain est d'abord un gain de **quota** et de
latence, pas une facture payée au token.

## 2. Méthode

- **Fenêtre** : 7 jours glissants au 17/11 02:25Z (2026-11-10 02:25Z →
  2026-11-17 02:25Z).
- **Sources** : `turn_costs` (un tour, son agent, son coût estimé, son
  horodatage de début et de fin), `ameesh_events` (`pilot.gesture` : agent,
  geste, réf ; `turn.context` : étiquette du tour), `agent_mailbox`
  (expéditeur, destinataire, type), `conversation_log` (consigne d'entrée et
  texte final de chaque tour).
- **Tours avec geste** : un tour est « avec geste » si un `pilot.gesture` du
  même agent tombe entre son début et sa fin. Les trois agents sont
  mono-tour, la jointure par fenêtre est donc exacte à la seconde près.
- **Échantillon** : 60 tours **sans geste** (20 par agent), tirés par
  `md5(turn_id)` croissant, lus intégralement (consigne + texte final) et
  classés en jugement ou mécanique. Les tours sans geste sont 2 983 des 3 759
  tours (79,4 %) : c'est là que se joue la décision de classer. L'annexe
  [`7638-annexe-echantillon.md`](7638-annexe-echantillon.md) donne la requête
  exacte, les 60 identifiants, leur étiquette, leur coût, leur classe et le
  motif ; aucun contenu de conversation n'y est reproduit.
- **Règle de classement** : un tour est **jugement** (J) dès qu'il produit un
  verdict de revue, un diagnostic, un arbitrage, une conception ou une
  décision conditionnelle ; il est **mécanique** si tout son contenu est un
  triage sans conséquence (M1), un contrôle d'état ou un rapport périodique
  (M2), un relais ou un routage (M3), ou de la continuité (M4). Un tour mixte
  est classé J : la règle est conservatrice, donc la part mécanique est un
  minorant de la classification.
- **Étiquettes de tour** : `messages`, `événements`, `prioritaire`, `consigne`,
  `reprise`, `suite`. Les étiquettes de continuité sont comptées exactement,
  mais une étiquette n'est pas une classe : être sans `pilot.gesture` ne
  prouve pas qu'une reprise ou une continuation est supprimable sans perte de
  jugement. L'annexe sépare la population des étiquettes du sous-ensemble
  réellement évitable.
- **Extrapolation** : par strate d'agent, facteur = population sans geste de
  l'agent / 20 (43,90 pour l'auditeur, 58,90 pour mesh-design, 46,35 pour
  l'orchestrateur). Les tours s'estiment directement ; les parts de coût sont
  calibrées sur le coût exact de la population (905,87 $) parce que
  l'échantillon brut coûte 19,04 $ pour 60 tours, soit 4,2 % de plus que la
  moyenne.
- **Limites** : l'échantillon est petit (60) ; la frontière jugement/mécanique
  est un jugement humain documenté, pas une vérité mesurée ; les gestes
  `pilot.gesture` ne portent pas l'identifiant du tour et sont rattachés par
  fenêtre. Les 776 tours avec geste (451 $) ne sont pas échantillonnés : aucun
  gain de cette étude ne les compte.

## 3. Mesures

### 3.1 Population

| Mesure (7 jours) | Valeur |
|---|---:|
| Tours du mesh | 8 758 |
| Coût estimé du mesh | 2 438 $ |
| Tours des trois agents de gouvernance | 3 759 (42,9 %) |
| Coût estimé des trois agents | 1 357 $ (55,7 %) |
| Tours Claude du mesh | 4 535 |
| Tours Claude des trois agents | 3 677 (81,1 %) |
| Tours avec geste pilote | 776 (20,6 %), 451 $ |
| Tours sans geste | 2 983 (79,4 %), 906 $ |

Le déséquilibre est net : trois agents font 43 % des tours, 56 % du coût
estimé et 81 % des tours Claude, pour un mesh de plusieurs dizaines d'agents.

### 3.2 Courrier reçu

8 520 messages reçus par les trois agents :

| Expéditeur | Messages | Part |
|---|---:|---:|
| Événements machine (`ameesh`) | 3 076 | 36,1 % |
| Agents d'exécution | 2 389 | 28,0 % |
| Autres agents de gouvernance | 1 650 | 19,4 % |
| Bureau | 798 | 9,4 % |
| Veille | 528 | 6,2 % |
| Humain (`human:smichea`) | 79 | 0,9 % |

Le courrier « entre eux » (gouvernance + bureau) est 28,7 %, les événements
machine 36 %, les agents d'exécution 28 %, la veille 6 %, l'humain moins de 1 %.
Près de deux tiers du courrier qui réveille la gouvernance vient donc de la
machine ou d'elle-même, pas d'un travail à décider.

### 3.3 Gestes déjà mécaniques

2 858 gestes pilotes sont journalisés sur la fenêtre. 1 430 (50,0 %) tombent
dans un tour d'agent ; **1 428 (50,0 %) sont émis hors tour**, par le code :

| Geste | Total | Dans un tour | Hors tour |
|---|---:|---:|---:|
| `merge` | 1 386 | 409 | 977 (70,5 %) |
| `packet` | 755 | 401 | 354 |
| `assign` | 622 | 525 | 97 |
| `decision` | 68 | 68 | 0 |
| `queue` | 27 | 27 | 0 |

La fusion de paquet est donc déjà, pour l'essentiel, un geste de code : la
preuve est faite qu'un geste peut se passer de tour. Ce qui reste dans les
tours n'est pas le geste lui-même, mais la vérification des conditions
(revue, CI, tête gelée) et le traitement des exceptions. Un même tour peut
porter plusieurs gestes : 1 430 gestes tombent dans 776 tours seulement.

### 3.4 Étiquettes de tour

| Étiquette | Tours | Coût estimé |
|---|---:|---:|
| `messages` | 2 772 | 1 030 $ |
| `événements` | 539 | 131 $ |
| `prioritaire` | 87 | 52 $ |
| `consigne` (synthèse de rotation) | 97 | 30 $ |
| `reprise` (lot repris) | 217 | 80 $ |
| `suite` (continuation après plafond) | 47 | 35 $ |

Le total des lignes d'étiquette est 3 759, exactement le nombre de tours.
Les tours de continuité (`consigne` + `reprise` + `suite`) sont 361 tours
(9,6 %) et 145 $ (10,7 %). Ce sont des étiquettes, pas une classe : une
`reprise` peut porter un verdict ou un arbitrage, et l'échantillon la classe
au cas par cas. 89 `consigne`, 135 `reprise` et 33 `suite` sont sans geste
(257 tours, 90 $) — c'est la population exacte des étiquettes, pas le gain
d'une règle (voir §4).

### 3.5 Échantillon classé (60 tours sans geste)

| Classe | Tours | Coût | Part du coût |
|---|---:|---:|---:|
| **Jugement** (verdict de revue, diagnostic, arbitrage, conception) | 29 | 13,27 $ | 69,7 % |
| **M1 — Triage sans conséquence** (écho, « rien à faire », alerte déjà traitée) | 10 | 0,91 $ | 4,8 % |
| **M2 — Contrôle d'état et rapport périodique** (paquet, CI, release, passage horaire) | 8 | 1,35 $ | 7,1 % |
| **M3 — Relais et routage** (copie, transmission, assignation, désignation de relecteur) | 6 | 1,33 $ | 7,0 % |
| **M4 — Continuité** (consigne, reprise, suite) | 7 | 2,17 $ | 11,4 % |
| **Mécanique (M1–M4)** | **31** | **5,76 $** | **30,3 %** |

Le jugement domine le coût (70 %), mais la mécanique domine le nombre de
tours (51,7 %). Les tours mécaniques sont moins chers (0,19 $ en moyenne contre
0,46 $), ce qui explique l'écart entre part des tours et part du coût.

## 4. Chiffrage par classe et gain des règles

Volumes et coûts estimés sur la population des 2 983 tours sans geste, avec
les facteurs de l'annexe. Les coûts sont calibrés sur le total exact de
905,87 $ (l'échantillon brut vaut 943,89 $, soit +4,2 %) :

| Classe | Tours/semaine | Coût/semaine | Part du coût sans geste |
|---|---:|---:|---:|
| J — Jugement | ~1 440 | ~632 $ | 69,8 % |
| M1 — Triage sans conséquence | ~526 | ~47 $ | 5,2 % |
| M2 — Contrôle d'état et rapport | ~391 | ~64 $ | 7,0 % |
| M3 — Relais et routage | ~286 | ~63 $ | 6,9 % |
| M4 — Continuité | ~340 | ~100 $ | 11,0 % |
| **Mécanique** | **~1 543 (51,7 %)** | **~274 $ (30,2 %)** | |

Le potentiel mécanique est donc d'environ **1 540 tours et 274 $ par
semaine**. Mais toutes les classes mécaniques ne sont pas retirables par les
règles proposées : M4 contient des synthèses de rotation et des continuations
qu'on ne peut pas supprimer sans perte. Le gain des règles est :

| Règle | Tours/semaine | Coût/semaine | Base |
|---|---:|---:|---|
| S1 — Porte de réveil sans conséquence | ~526 | ~47 $ | M1 (estimation) |
| S2 — Digest d'état paquet / CI / release | ~391 | ~64 $ | M2 (estimation) |
| S3 — Routage et désignation automatiques | ~286 | ~63 $ | M3 (estimation) |
| S4 — Suppression des reprises de paquet | 61 | 13,75 $ | reprises de paquet (exact) |
| **Total S1–S4** | **~1 264** | **~188 $** | |

Soit environ **190 $ par semaine** (820 $ par mois, 9 800 $ par an en
équivalent grand livre) et **1 260 tours par semaine**. Le levier reste
d'abord un levier de quota Claude : 81 % des tours Claude sont des tours de
gouvernance, et 51,7 % des tours de gouvernance sans geste sont mécaniques.

Deux écarts avec la version précédente de l'étude sont assumés :

1. **Base d'extrapolation.** La version précédente extrapolait les classes
   mécaniques sur les 2 983 tours sans geste, mais calculait le jugement comme
   le reste des 3 759 tours (1 357 $) : elle mélangeait deux populations et
   comptait les 776 tours avec geste comme du jugement. Ici toutes les classes
   sont estimées sur les 2 983 tours sans geste, dont le coût exact est
   905,87 $ ; les 776 tours avec geste restent hors chiffrage.
2. **Étiquettes de continuité.** Les 257 tours et 90 $ des étiquettes
   `consigne` + `reprise` + `suite` sont une population exacte, pas un gain :
   89 consignes (25 $) restent une synthèse de rotation, 33 suites (22 $) sont
   la continuation du travail, 74 reprises (29 $) ne portent pas un paquet.
   S4 ne retire que les 61 reprises de paquet (13,75 $), comptées exactement.

Le reste de la classe M4 (~279 tours, ~86 $ estimés) n'est donc pas retiré par
S1–S4 : c'est de la continuité qui demande une décision séparée (§8). Les
étiquettes exactes donnent, pour ce reste, 89 consignes + 33 suites + 74
reprises hors paquet = 196 tours et 76 $ ; l'écart avec 279 tours / 86 $ est
l'erreur d'échantillonnage de M4, qui sur-représente la continuité.

Un levier complémentaire, non compté ci-dessus : les 776 tours avec geste
coûtent 451 $, et 409 fusions (dont 120 côté Nexlink) y consomment encore un
tour. Si une partie de ces tours ne portait que l'exécution du geste et son
compte rendu, le gain serait de l'ordre de 100 $ par semaine ; l'étude ne le
compte pas, faute d'échantillon dédié. C'est l'objet de S5.

## 5. Règles de code proposées

Principe commun (§7 du canon) : le code fait le geste, l'agent garde le
jugement. Chaque règle est un filtre ou un calcul déterministe, doublé d'une
porte de sortie vers l'agent quand l'état ne rentre pas dans le cas connu.

### 5.1 M1 — Porte de réveil sans conséquence

**Règle** : avant de réveiller un agent, une table de conséquences par type de
réveil — événement machine, courrier d'information, écho de l'action de l'agent
lui-même — lit l'état courant (lot fermé, alerte déjà résolue, verdict déjà
livré, courrier qui ne demande rien). Si le réveil ne change ni l'état ni
l'action attendue, il est consommé sans tour et versé au digest périodique ;
s'il est inconnu, il passe (défaut sûr).

**Gain** : ~526 tours et ~47 $ par semaine. **Risque** : rater un événement
dont la conséquence n'est pas modélisée. **Garde** : digest toutes les 30 min,
compteur d'« événements non classés » alerté, et possibilité pour un agent de
demander le flux brut.

### 5.2 M2 — Digest d'état des paquets, de la CI et des releases

**Règle** : l'état (paquet, CI, release, staging) est déjà dans la base ; un
calcul périodique produit le digest et ne réveille que sur **transition**
(rouge → vert, file → fusionné, attente → bloqué). Un rapport horaire de type
« rien n'a changé » est calculé, pas écrit par un agent. Les seuils (SLO,
durée de CI) restent des alertes, avec un seul tour à la première violation.

**Gain** : ~391 tours et ~64 $ par semaine. **Risque** : digest périmé ou
trompeur. **Garde** : le digest est daté, calculé sur la même source que l'UI,
et ne porte aucune décision ; l'agent garde un tour sur exception.

### 5.3 M3 — Routage, désignation et relais sans tour

**Règle** : l'assignation d'un lot libre se fait déjà avec le garde d'empreinte
(#3185) ; il reste à en faire une règle de capacité sans tour. La désignation
des relecteurs par famille et indépendance est celle de #1617 (livrée) : le tour
restant est la notification et l'exception, pas la désignation. Un relais ou une
copie se fait par `--cc` sans réveil ; un accusé se fait par `--ack`.

**Gain** : ~286 tours et ~63 $ par semaine. **Risque** : mauvaise assignation
ou mauvais relecteur. **Garde** : journal du geste, override du pilote, refus
si l'agent est au plafond de lots.

### 5.4 M4 — Reprises de paquet par le composeur

**Règle** : quand un lot porteur de paquet est pris en charge par le composeur,
la `reprise` de l'agent n'a plus d'objet : le composeur gère la composition, la
CI, le gel et la fusion ; l'agent n'est réveillé que sur exception (CI rouge,
conflit, tête changée). La règle s'applique aux 61 reprises de paquet sans
geste de la fenêtre.

**Gain** : **61 tours et 13,75 $ par semaine, comptés exactement** (pas une
extrapolation) sur les lots porteurs dont le titre commence par « Paquet »
(les champs `package_id`/`package_parent` ne sont pas renseignés pour ces
lots) ; les 257 tours / 90 $ des étiquettes de continuité sont un majorant,
pas le gain : 89 `consigne` (25 $) sont des synthèses de rotation, 33 `suite`
(22 $) sont la continuation du travail, 74 `reprises` (29 $) ne portent pas
un paquet.

**Variante à décider séparément** : remplacer aussi la synthèse de rotation
(`SUMMARY_PROMPT`, `adapters.py:114`) par le brief déterministe de
`reprise.py`, en ne gardant une synthèse LLM que sur exception. Cela
retirerait jusqu'à 89 tours et 25 $ de plus, mais supprime la synthèse par
modèle à la rotation ; l'étude ne le compte pas.

**Risque** : perdre une nuance qu'un agent aurait résumée. **Garde** : la note
déterministe reprend lot, branche, tête, derniers verdicts, attentes ouvertes
et prochaine action ; la rotation garde une synthèse courte en secours.

### 5.5 Levier à mesurer — fusion automatique Nexlink

**Constat** : 977 des 1 266 fusions de mesh-design se font déjà hors tour ;
côté Nexlink, les 120 fusions de l'orchestrateur sont toutes dans un tour.
**Règle** : mesurer pourquoi ces 120 fusions ne passent pas par le workflow
`merge-pr` (tête gelée, CI verte, deux avis) puis activer la file là où les
portes sont déterministes. **Gain** : non chiffré par cette étude ; l'ordre de
grandeur de ~100 $ par semaine reste une hypothèse à mesurer.
**Risque** : fusion de production — la règle est sensible et demande le go de
l'owner ; elle ne doit pas être activée par la présente étude.

## 6. Lots de suite proposés

| Lot | Contenu | Classe | Estimation | Gain attendu |
|---|---|---|---:|---:|
| S1 | Porte de réveil sans conséquence + digest 30 min | M1 | 1,5 j | ~526 tours, ~47 $/sem |
| S2 | Digest d'état paquet / CI / release, réveil sur transition | M2 | 1,5 j | ~391 tours, ~64 $/sem |
| S3 | Routage et désignation automatiques, relais `--cc` / `--ack` | M3 | 1 j | ~286 tours, ~63 $/sem |
| S4 | Reprises de paquet prises par le composeur | M4 | 1 j | 61 tours, 13,75 $/sem (exact) |
| S5 | Mesure puis activation de la fusion Nexlink sur tête gelée | gestes | 2 j | à mesurer |
| **Total S1–S4** | | | **5 j** | **~1 264 tours, ~188 $/sem** |

**Qualification : aucune n'est acquise par l'étude.** Le §8 ameesh qualifie
selon les chemins réellement touchés, pas selon « sans migration ni prod » :

- **S1** touche le réveil et les consignes (`runner.py`, `notify.py`,
  `harness*`) : chemin sensible probable.
- **S2** est un calcul de digest ; il peut rester léger s'il ne change pas la
  porte de réveil, mais `ameesh review-class` le confirmera sur les fichiers.
- **S3** touche la désignation et le feu (`review*`, `fire`, `train*`,
  `packet_candidates`) : chemin sensible.
- **S4** touche `adapters.py` et `reprise.py` (consignes) : chemin sensible.
- **S5** touche la porte de fusion et la prod : sensible, avec go owner.

S2 est donc le seul candidat sérieux à la classe légère ; S1, S3, S4 et S5
doivent être désignés et qualifiés sur leurs chemins avant lancement. La
présente étude ne les déclare pas légers.

Source des chemins : canon `b23fed82692e6027bb352a82b20e36e276d4736f`,
`federation.yaml`, classes de risque du projet ameesh — `runner.py` en
concurrence, `reprise.py` en consignes, `review*.py` / `fire.py` / `train*.py`
en porte de fusion.

## 7. Risques et limites

- **Faux négatif** : supprimer un tour de triage peut masquer un cas que le
  code n'a pas modélisé. Toutes les règles ci-dessus gardent un défaut sûr
  (le réveil inconnu passe) et un digest de rattrapage.
- **Dérive du digest** : un état calculé peut être périmé ; il est daté et
  recalculé sur la même source que l'UI, jamais recopié à la main.
- **Perte de jugement** : les règles ne touchent ni les verdicts de revue, ni
  les diagnostics d'incident, ni les arbitrages de conception, qui restent
  hors du périmètre. Le classement conservateur (tout tour mixte = jugement)
  sous-estime plutôt la part mécanique ; à l'inverse, le gain des règles est
  un majorant, puisque des tours mécaniques restent non couverts.
- **Mesure** : 60 tours classés à la main, ±12 points à 95 % ; les coûts
  Claude sont des estimations de grand livre pour des sièges en forfait. Les
  chiffres disent l'ordre de grandeur et le quota libéré, pas une facture.
  L'annexe donne les 60 identifiants et la requête : un relecteur peut
  reclasser et recalculer.
- **Population non échantillonnée** : les 776 tours avec geste (451 $) ne sont
  pas classés ; S5 devra les mesurer avant tout chiffrage.

## 8. Décision demandée à l'owner

1. Valider S1 à S4, pour un gain attendu d'environ **1 260 tours et 190 $ par
   semaine**, et leur ordre de lancement : S2 (le plus gros gain), puis S1,
   S3, S4.
2. Pour S4, décider séparément si la synthèse de rotation est aussi remplacée
   par le brief déterministe (jusqu'à +89 tours et +25 $/semaine) ou si elle
   est conservée à chaque rotation.
3. Pour S5 (fusion Nexlink) : lancer la mesure seule, sans activation, et
   revenir avec le chiffrage avant tout go de production.
4. Qualifier chaque lot sur ses chemins réels (`ameesh review-class`) : S2 est
   le seul candidat léger ; S1, S3, S4 et S5 touchent des chemins sensibles.

Aucun changement de comportement n'est fait par ce lot : le document est une
proposition, les lots de suite seront créés après la décision.

## 9. Note de révision (relecture #223308 du 17/11)

- **B1** : l'annexe
  [`7638-annexe-echantillon.md`](7638-annexe-echantillon.md) donne la requête
  de tirage, les 60 identifiants, l'agent, l'étiquette, le coût, la classe et
  le motif, ainsi que les facteurs par agent. Les 257 étiquettes de continuité
  sont distinguées de leur classement mécanique : S4 ne retient que les 61
  reprises de paquet, comptées exactement.
- **B2** : le gain annoncé est celui des règles (S1–S4 : ~1 264 tours,
  ~188 $/semaine), et non les 354 $ de la version précédente, qui mélangeait
  deux populations et comptait les 257 étiquettes comme évitables.
- **Qualification** : S1–S4 ne sont plus déclarés légers d'avance ; la
  qualification est renvoyée aux chemins réels. S2 est le seul candidat léger ;
  S1, S3, S4 et S5 touchent des chemins sensibles (`runner.py`, `notify.py`,
  `review*`, `fire`, `train*`, `adapters.py`, `reprise.py`, porte de fusion).
