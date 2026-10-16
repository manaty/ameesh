---
type: Study
title: "Étude — cadence des paquets horaires compilée depuis le canon (ameesh #1757)"
description: "Bloc `packets.cutoffs` du canon machine : 24 créneaux horaires en Europe/Paris, distincts des coupures de release 11:00/23:00 ; ports stricts `packet_cutoffs`, `packets_on`, `last_packet`, `next_packet` du lecteur `ameesh.mechanics` ; absence = tuple vide et refus, surcharge de projet en bloc entier, calendrier 23/25 créneaux aux passages d'heure ; fournisseur du train #1467, sans activation."
status: draft
tags: [canon, mecanique, paquets, train, horaire, parametres]
generated: { by: "mty-amh-impl-07/deepseek", at: "2026-10-15T21:15:00+02:00" }
stale_after: 2027-01-15
sources:
  - { resource: "https://github.com/manaty/home/pull/94", title: "canon §11 bis horaire : fusions horaires, deux releases par jour (owner, 15/10/2026 19:50)" }
  - { resource: "https://github.com/manaty/home/pull/88", title: "paramètres mécaniques du processus (#1616), contenu conservé" }
  - { resource: "../../src/ameesh/mechanics.py", title: "lecteur strict, ports des paquets horaires" }
  - { resource: "../../src/ameesh/canon_sync.py", title: "canon sync : constat `mechanics`" }
---

# Contexte

Le canon §11 bis horaire (owner, 15/10/2026 vers 19:50, home#94 mergé
`937974cf`) remplace « un seul paquet par release » : chaque heure, un petit
paquet des lots à verdicts complets est composé, passe **une** CI complète et
est fusionné dès le vert ; à 11:00 et 23:00 la release est la tête de la cible,
taguée et déployée en staging. Le train #1467 est le moteur unique de ces
paquets.

Le lot #1757 est l'incrément fournisseur demandé par le consommateur cx3
(20:00) après le lot #1616, qui a livré le lecteur strict
`ameesh.mechanics` et le contrat `processus/mecanique.yaml`. Il publie le bloc
machine de la cadence horaire — distinct des coupures de release déjà
déclarées — sans activer aucun consommateur : la préparation ne vaut pas
activation.

# Contrat du canon

`processus/mecanique.yaml` (schéma `ameesh-mechanics/1`), au commit exact lu
par le lecteur :

- `cutoffs` (`["11:00", "23:00"]`) : les **coupures de release**, en heure
  locale du fuseau — inchangées ;
- `packets.cutoffs` : bloc **optionnel**, 24 créneaux `"HH:00"` en
  `Europe/Paris`, un par heure locale. C'est la cadence des paquets horaires,
  jamais déduite d'une durée ni des coupures de release ;
- `projects.<nom>.packets` : surcharge de projet, **bloc entier remplacé**
  (même sens que `cutoffs`) ; un projet sans `packets` hérite du bloc global.

Commit livré : `21d6653922bac554d2d6cd0570e7f6497b215c37`, branche
`canon/1757-paquets-horaires` de `manaty/home`, sur main fraîche
`c838dd3` — elle inclut le §11 bis horaire `937974cf` et **tout le contenu de
#1616 / PR home#88** (`processus/mecanique.yaml`, `federation.yaml`,
`processus/lots.md`).

# Lecture stricte

`ameesh.mechanics` (branche `lot/1757-paquets-horaires`, base = tête gelée de
#1616 `53cf2e01`, sans la modifier) :

- `Mechanics.packet_cutoffs : tuple[str, ...]` — `()` explicite quand le bloc
  est absent ; jamais un repli ;
- `Mechanics.packets_on(day) -> ((epoch, "HH:MM"), ...)` — les occurrences du
  jour local, triées par epoch ;
- `Mechanics.last_packet(now)` — la dernière occurrence `<= now` (bornes
  interjour comprises) ; `next_packet(now)` — la première `> now` ; **refus**
  (`MechanicsError`) sans cadence déclarée ;
- bloc présent : clé inconnue, liste vide, heure mal formée ou doublon →
  refus ; surcharge de projet invalide → refus ;
- `canon sync` expose la cadence compilée (`packets` du constat `mechanics`),
  sans jamais fermer le canon.

Calendrier `Europe/Paris` : 24 occurrences un jour ordinaire ; **23** au
passage à l'heure d'été (02:00 n'existe pas, il est sauté) ; **25** au passage
à l'heure d'hiver (02:00 existe deux fois, les deux occurrences sont
distinguées par leur epoch) ; aucun doublon d'epoch, ordre chronologique.

# Carte de raccord (déclarée, aucune activation par ce lot)

| Consommateur | Paramètres compilés | État |
|---|---|---|
| Train #1467 (cx3) | `packet_cutoffs`, `packets_on`, `last_packet`, `next_packet` | contrat fourni (SHA code + canon ci-dessus) ; **non activé** |
| Alertes/sous-emploi, composition horaire | cadence des paquets | hors de ce lot |

L'activation reste un geste de pilote : le moteur unique #1467 garde son paquet
de CI et son bail courants, aucune composition parallèle ni relance forcée à
chaque heure ne sont introduites ici.

# Preuve

- Témoins neufs `tests/test_l1757_paquets_horaires.py` : 14 cas (calendrier
  ordinaire, 23/25 aux passages d'heure, bornes interjour, heure répétée
  distinguée par epoch, absence = vide + refus, surcharge en bloc,
  strictness, constat `canon sync`).
- `tests.test_l1757_paquets_horaires` + `tests.test_l1616_mechanics` : 33
  verts. Famille canon (canon, inject, canons multiples, authentificateurs,
  garde auditeur) : 196 verts.
- Contre-épreuve : les 14 témoins neufs sur le parent `53cf2e01` (code #1616
  sans le bloc) = 1 échec + 10 erreurs, dont l'absence de `packet_cutoffs`.
- Tête code livrée : `501196ee4f7b2ddef3c879080e4ea534f0f9b4c1` (branche
  `lot/1757-paquets-horaires`, base `53cf2e01`) ; commit canon :
  `21d6653922bac554d2d6cd0570e7f6497b215c37`.
- Relecture requise : `mechanics.py` est déclaré sensible par la politique du
  canon (#1616) ; le pilote désigne les relecteurs.
