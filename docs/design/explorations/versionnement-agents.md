---
type: Exploration
status: exploration
title: "Versionnement pour agents : dépendances explicites et attestation par changement"
---

# Versionnement pour agents : dépendances explicites et attestation par changement

## Problème

Des lots verts séparément peuvent se contredire une fois composés : le
conflit n'est pas textuel mais sémantique (#794 ; #1266 contre
`test_l161f` ; #928 contre `test_l56`), des tests dépendent du nombre de
modules chargés. Un revert peut annuler le contenu d'une fusion tout en
conservant son ascendance : une tête ancêtre ne prouve donc plus que son
contenu est présent (#1825). Git mémorise des instantanés et leur
ascendance ; il ne prouve ni leur compatibilité sémantique, ni la cause
d'un test rouge.

Le diagnostic se paie donc en `bisect --first-parent` sur des paquets
horaires déjà fusionnés (#1293, #1308, #1346), alors que la preuve de test
reste attachée au commit et non au contenu du changement. La carte
test → code mesurée (L161e) rapproche déjà chaque test des blocs qu'il
exerce, mais elle ne relie pas cette mesure à la composition des lots.

## Intuitions

- **Changements à dépendances explicites qui commutent.** Pijul ou Darcs
  montrent qu'une unité de changement peut déclarer ce dont elle dépend et
  ne s'appliquer que si ses dépendances sont présentes : la composition
  devient un calcul de commutation, et un désaccord devient une
  incompatibilité nommée plutôt qu'un marqueur de conflit. Ces garanties
  portent sur les patches : elles ne détectent pas seules les dépendances
  sémantiques entre lots, comme le précise la
  [motivation de Darcs](https://darcs.net/Theory/Motivation). La
  [théorie de Pijul](https://pijul.org/manual/theory.html) distingue aussi
  les dépendances structurelles des dépendances sémantiques à ajouter.
  Le pilote
  Mergiraf (L160g) a mesuré la fusion syntaxique et ne l'a pas adoptée ;
  l'intuition porte sur l'unité composée, pas sur un meilleur recollement
  ligne à ligne.
- **Attestation de tests attachée au contenu d'un changement.** Une preuve
  (tests verts, portée, environnement) voyagerait avec le contenu qu'elle
  couvre ; après une commutation ou une recomposition, elle serait rejouée
  ou invalidée explicitement, au lieu d'être supposée par héritage du
  commit.
- **Désignation directe du changement fautif.** La carte test → code L161e
  mesure les blocs exercés par chaque test. Croisée avec les blocs modifiés
  par les lots et leurs attestations, elle pourrait réduire les candidats
  d'un rouge de paquet. Distinguer un lot fautif d'une interaction entre
  lots demande encore une preuve ; la couverture seule ne le démontre pas.
- **Composition horaire sans revert.** Un paquet horaire deviendrait une
  sélection de changements qui commutent, dont le retrait serait exact et
  n'obligerait ni revert de fusion ni rejeu complet.

## Questions ouvertes

- Comment représenter une dépendance entre changements sans imposer un DAG
  complet à chaque auteur ?
- Que devient une attestation dont le contenu a commuté depuis : rejouer,
  invalider, ou redemander ?
- Quel contexte de dépendances, de données et d'environnement une
  attestation doit-elle couvrir pour rester valable après recomposition ?
- Comment un agent sait-il qu'un test rouge vient de son changement plutôt
  que d'un voisin composé dans le même paquet ?
- Le retrait d'un changement déjà composé peut-il être exact, et que
  deviennent les attestations de ses voisins ?
- Quelles garanties d'audit et de signature si l'unité de preuve n'est
  plus le commit ?
- Que garde-t-on de git — revue, PR, historique — et qu'est-ce qui change
  de nature ?

Contexte : [Étude tests par impact](../etudes/l161-tests-par-impact.md)
(carte test → code), [Radar de conflits](../etudes/l160-radar-de-conflits.md),
[Pilote Mergiraf](../etudes/l160g-mergiraf-pilot.md), et le
[processus de lot](https://github.com/manaty/home/blob/main/processus/lots.md)
(paquets horaires). Ces références éclairent la question ; elles ne
promeuvent pas cette fiche en étude ou en travail décidé.
