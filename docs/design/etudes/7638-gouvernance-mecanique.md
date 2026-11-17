---
type: Study
title: "Gouvernance du mesh Manaty — étude chiffrée de la part mécanique à mettre en code (lot #7638)"
description: "Étude du lot #7638 (issue manaty/ameesh#5829) : sur 7 jours de la base du mesh, les trois agents de gouvernance (orchestrateur, auditeur, mesh-design) font 3 759 tours (42,9 % du mesh) et 1 357 $ estimés (55,7 %) ; 8 511 messages reçus, 2 858 gestes pilotes dont 1 428 déjà émis hors tour ; un échantillon tiré de 60 tours sans geste donne 55 % de gestes mécaniques (triage, contrôle d'état, relais, continuité) pour 37 % de leur coût. Quatre règles de code (porte d'événement sans conséquence, digest d'état, routage et désignation automatiques, note de session déterministe) retireraient environ 1 600 tours et 350 $ par semaine ; un cinquième levier (fusion automatique Nexlink) reste à mesurer avant activation. Aucun code, aucun réglage modifié par ce lot."
status: draft
tags: [gouvernance, mecanique, tours, cout, canon, quorum, paquet, fusion]
generated: { by: "mty-amh-impl-01/deepseek", at: "2026-11-17T02:32:39Z" }
stale_after: 2027-02-17
sources:
  - { resource: "https://github.com/manaty/ameesh/issues/5829", title: "issue miroir du lot #7638 (ameesh)" }
  - { resource: "#7627", title: "spec validée par l'owner le 17/11/2026, §5 (répartition IMA / ameesh)" }
  - { resource: "base du mesh (AMEESH_DSN), tables turn_costs, ameesh_events, agent_mailbox, conversation_log, 2026-11-10 02:25Z → 2026-11-17 02:25Z", title: "population, courrier, gestes, étiquettes et échantillon" }
  - { resource: "../../../src/ameesh/pilot_gesture.py", title: "journal et porte des gestes pilote (`pilot.gesture`)" }
  - { resource: "../../../src/ameesh/packet_automerge.py", title: "fusion automatique des paquets, déjà utilisée hors tour" }
  - { resource: "../../../src/ameesh/train_fire.py", title: "feu et file des paquets (orchestrateur)" }
  - { resource: "../../../src/ameesh/role_timers.py", title: "passes périodiques des rôles" }
  - { resource: "../../../src/ameesh/adapters.py", title: "consigne de synthèse de session (`Summarize this session…`, ligne 114)" }
  - { resource: "../../../src/ameesh/reprise.py", title: "brief de reprise et de rotation" }
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
- **Sources** : `turn_costs` (un tour, son agent, son coût estimé, son horodatage
  de début et de fin), `ameesh_events` (`pilot.gesture` : agent, geste, réf),
  `agent_mailbox` (expéditeur, destinataire, type), `conversation_log` (consigne
  d'entrée et texte final de chaque tour).
- **Tours avec geste** : un tour est « avec geste » si un `pilot.gesture` du même
  agent tombe entre son début et sa fin. Les trois agents sont mono-tour, la
  jointure par fenêtre est donc exacte à la seconde près.
- **Échantillon** : 60 tours **sans geste** (20 par agent), tirés par
  `md5(turn_id)`, lus intégralement (consigne + texte final) et classés en
  jugement ou mécanique. Les tours sans geste sont 2 983 des 3 759 tours
  (79 %) : c'est là que se trouve la décision de classer. L'échantillon donne
  une précision d'environ ±12 points à 95 % sur les proportions ; les volumes
  et coûts sont ensuite extrapolés par agent (facteur propre à chaque agent).
- **Étiquettes de tour** : `messages`, `événements`, `prioritaire`, `consigne`,
  `reprise`, `suite` (le libellé que le runner donne à la consigne). Les
  étiquettes de continuité sont comptées exactement, pas extrapolées.
- **Limites** : l'échantillon est petit (60) ; la frontière jugement/mécanique
  est un jugement humain documenté, pas une vérité mesurée ; les gestes
  `pilot.gesture` ne portent pas l'identifiant du tour et sont rattachés par
  fenêtre.

## 3. Mesures

### 3.1 Population

| Mesure (7 jours) | Valeur |
|---|---:|
| Tours du mesh | 8 760 |
| Coût estimé du mesh | 2 439 $ |
| Tours des trois agents de gouvernance | 3 759 (42,9 %) |
| Coût estimé des trois agents | 1 357 $ (55,7 %) |
| Tours Claude du mesh | 4 536 |
| Tours Claude des trois agents | 3 677 (81,1 %) |
| Tours avec geste pilote | 777 (20,7 %), 450 $ |
| Tours sans geste | 2 983 (79,3 %), 906 $ |

Le déséquilibre est net : trois agents font 43 % des tours, 56 % du coût
estimé et 81 % des tours Claude, pour un mesh de plusieurs dizaines d'agents.

### 3.2 Courrier reçu

8 511 messages reçus par les trois agents :

| Expéditeur | Messages | Part |
|---|---:|---:|
| Événements machine (`ameesh`) | 3 068 | 36,0 % |
| Autres agents de gouvernance | 1 651 | 19,4 % |
| Bureau | 796 | 9,4 % |
| Agents d'exécution | 2 389 | 28,1 % |
| Veille | 528 | 6,2 % |
| Humain (`human:smichea`) | 79 | 0,9 % |

Le courrier « entre eux » (gouvernance + bureau) est 28,8 %, les événements
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
(revue, CI, tête gelée) et le traitement des exceptions.

### 3.4 Étiquettes de tour

| Étiquette | Tours | Coût estimé |
|---|---:|---:|
| `messages` | 2 774 | 1 031 $ |
| `événements` | 539 | 131 $ |
| `prioritaire` | 86 | 50 $ |
| `consigne` (synthèse de session) | 97 | 30 $ |
| `reprise` (lot repris) | 217 | 80 $ |
| `suite` (continuation après plafond) | 47 | 35 $ |

Le total des lignes d'étiquette est 3 760 pour 3 759 tours : un tour porte deux
lignes de coût dans la fenêtre.

Les tours de continuité (`consigne` + `reprise` + `suite`) sont 361 tours
(9,6 %) et 145 $ (10,7 %) : ils ne produisent aucune décision de gouvernance,
seulement de la continuité de session. 89 `consigne`, 135 `reprise` et 33
`suite` sont sans geste (257 tours, 90 $).

### 3.5 Échantillon classé (60 tours sans geste)

| Classe | Tours | Coût | Part du coût |
|---|---:|---:|---:|
| **Jugement** (verdict de revue, diagnostic, arbitrage, conception) | 27 | 12,01 $ | 63,1 % |
| **M1 — Triage sans conséquence** (écho, « rien à faire », alerte déjà traitée) | 11 | 1,28 $ | 6,7 % |
| **M2 — Contrôle d'état et rapport périodique** (paquet, CI, release, passage horaire) | 12 | 2,89 $ | 15,2 % |
| **M3 — Relais et routage** (copie, transmission, assignation, désignation de relecteur) | 4 | 1,03 $ | 5,4 % |
| **M4 — Continuité** (consigne, reprise, suite) | 6 | 1,83 $ | 9,6 % |
| **Mécanique (M1–M4)** | **33** | **7,03 $** | **36,9 %** |

Le jugement domine le coût (63 %), mais la mécanique domine le nombre de
tours (55 %). Les tours mécaniques sont moins chers (0,21 $ en moyenne contre
0,44 $), ce qui explique l'écart entre part des tours et part du coût.

## 4. Chiffrage par classe

Volumes et coûts extrapolés à la population des 2 983 tours sans geste, avec
les étiquettes exactes pour M4 :

| Classe | Tours/semaine | Coût/semaine | Part du coût de gouvernance |
|---|---:|---:|---:|
| M1 — Triage sans conséquence | ~550 | ~62 $ | 4,6 % |
| M2 — Contrôle d'état et rapport | ~600 | ~146 $ | 10,8 % |
| M3 — Relais et routage | ~200 | ~56 $ | 4,1 % |
| M4 — Continuité | 257 (exact) | 90 $ (exact) | 6,6 % |
| **Mécanique** | **~1 607 (42,8 %)** | **~354 $ (26,1 %)** | |
| Jugement | ~2 152 | ~1 003 $ | 73,9 % |

Soit environ **350 $ par semaine** (1 500 $ par mois, 18 000 $ par an en
équivalent grand livre) et **1 600 tours par semaine** retirables sans toucher
au jugement. Le levier est d'abord un levier de quota Claude : 81 % des tours
Claude sont des tours de gouvernance, et 43 % des tours de gouvernance sont
mécaniques.

Un levier complémentaire, non compté ci-dessus : les 777 tours avec geste
coûtent 450 $, et 409 fusions (dont 120 côté Nexlink) y consomment encore un
tour. Si la moitié de ces tours ne portait que l'exécution du geste et son
compte rendu, le gain serait de l'ordre de 100 $ par semaine ; l'échantillon
de 12 tours avec geste montre surtout du jugement autour du geste (staging,
diagnostic, arbitrage), ce qui interdit de le compter sans mesure dédiée.

## 5. Règles de code proposées

Principe commun (§7 du canon) : le code fait le geste, l'agent garde le
jugement. Chaque règle est un filtre ou un calcul déterministe, doublé d'une
porte de sortie vers l'agent quand l'état ne rentre pas dans le cas connu.

### 5.1 M1 — Porte d'événement sans conséquence

**Règle** : avant de réveiller un agent, une table de conséquences par type
d'événement lit l'état courant (lot fermé, alerte déjà résolue, écho de l'action
de l'agent lui-même, courrier qui ne demande rien). Si l'événement ne change ni
l'état ni l'action attendue, il est consommé sans tour et versé au digest
périodique ; s'il est inconnu, il passe (défaut sûr).

**Gain** : ~550 tours et ~62 $ par semaine. **Risque** : rater un événement
dont la conséquence n'est pas modélisée. **Garde** : digest toutes les 30 min,
compteur d'« événements non classés » alerté, et possibilité pour un agent de
demander le flux brut.

### 5.2 M2 — Digest d'état des paquets, de la CI et des releases

**Règle** : l'état (paquet, CI, release, staging) est déjà dans la base ; un
calcul périodique produit le digest et ne réveille que sur **transition**
(rouge → vert, file → fusionné, attente → bloqué). Un rapport horaire de type
« rien n'a changé » est calculé, pas écrit par un agent. Les seuils (SLO,
durée de CI) restent des alertes, avec un seul tour à la première violation.

**Gain** : ~600 tours et ~146 $ par semaine, dont ~100 $ pour le seul volet
statut. **Risque** : digest périmé ou trompeur. **Garde** : le digest est daté,
calculé sur la même source que l'UI, et ne porte aucune décision ; l'agent
garde un tour sur exception.

### 5.3 M3 — Routage, désignation et relais sans tour

**Règle** : l'assignation d'un lot libre se fait déjà avec le garde d'empreinte
(#3185) ; il reste à en faire une règle de capacité sans tour. La désignation
des relecteurs par famille et indépendance est celle de #1617 (livrée) : le tour
restant est la notification et l'exception, pas la désignation. Un relais ou une
copie se fait par `--cc` sans réveil ; un accusé se fait par `--ack`.

**Gain** : ~200 tours et ~56 $ par semaine. **Risque** : mauvaise assignation
ou mauvais relecteur. **Garde** : journal du geste, override du pilote, refus
si l'agent est au plafond de lots.

### 5.4 M4 — Note de session déterministe

**Règle** : remplacer la consigne « Summarize this session… »
(`adapters.py:114`) par une note construite du grand livre : lot, branche,
tête, derniers verdicts, attentes ouvertes, prochaine action. Garder une
synthèse par modèle seulement à la rotation de session, pas à chaque reprise.
Supprimer la `reprise` d'un lot porteur de paquet quand le composeur le prend
en charge.

**Gain** : 257 tours et 90 $ par semaine (exact). **Risque** : perdre une
nuance qu'un agent aurait résumée. **Garde** : la note reprend le dernier texte
de tour et les attentes ouvertes ; la rotation garde une synthèse LLM courte.

### 5.5 Levier à mesurer — fusion automatique Nexlink

**Constat** : 977 des 1 266 fusions de mesh-design se font déjà hors tour ;
côté Nexlink, les 120 fusions de l'orchestrateur sont toutes dans un tour.
**Règle** : mesurer pourquoi ces 120 fusions ne passent pas par le workflow
`merge-pr` (tête gelée, CI verte, deux avis) puis activer la file là où les
portes sont déterministes. **Gain potentiel** : ~100 $ par semaine.
**Risque** : fusion de production — la règle est sensible et demande le go de
l'owner ; elle ne doit pas être activée par la présente étude.

## 6. Lots de suite proposés

| Lot | Contenu | Classe | Estimation | Gain attendu |
|---|---|---|---:|---:|
| S1 | Porte d'événement sans conséquence + digest 30 min | M1 | 1,5 j | ~62 $/sem |
| S2 | Digest d'état paquet / CI / release, réveil sur transition | M2 | 1,5 j | ~100 $/sem |
| S3 | Routage et désignation automatiques, relais `--cc` / `--ack` | M3 | 1 j | ~56 $/sem |
| S4 | Note de session déterministe et suppression des reprises de paquet | M4 | 1 j | ~90 $/sem |
| S5 | Mesure puis activation de la fusion Nexlink sur tête gelée | gestes | 2 j | ~100 $/sem (à confirmer) |

S1 à S4 sont des lots légers (filtres et calculs, sans migration ni prod) et
peuvent être lancés en parallèle après décision. S5 touche la fusion de
production : mesure d'abord, activation ensuite, avec le go de l'owner.

## 7. Risques et limites

- **Faux négatif** : supprimer un tour de triage peut masquer un cas que le
  code n'a pas modélisé. Toutes les règles ci-dessus gardent un défaut sûr
  (l'événement inconnu passe) et un digest de rattrapage.
- **Dérive du digest** : un état calculé peut être périmé ; il est daté et
  recalculé sur la même source que l'UI, jamais recopié à la main.
- **Perte de jugement** : les règles ne touchent ni les verdicts de revue, ni
  les diagnostics d'incident, ni les arbitrages de conception, qui restent
  hors du périmètre.
- **Mesure** : 60 tours classés à la main, ±12 points à 95 % ; les coûts Claude
  sont des estimations de grand livre pour des sièges en forfait. Les chiffres
  disent l'ordre de grandeur et le quota libéré, pas une facture.

## 8. Décision demandée à l'owner

1. Valider S1 à S4 (lots légers) et leur ordre de lancement.
2. Pour S5 (fusion Nexlink) : lancer la mesure seule, sans activation, et
   revenir avec le chiffrage avant tout go de production.

Aucun changement de comportement n'est fait par ce lot : le document est une
proposition, les lots de suite seront créés après la décision.
