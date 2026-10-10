---
type: Decision
title: "Consommer d'abord la capacité qui expire le plus tôt"
description: "Entre les comptes au forfait d'un même fournisseur, ameesh choisit pour chaque nouvelle session le compte le plus en retard sur son rythme (amendement du 2026-10-10), à égalité celui dont la capacité inutilisée expire le plus tôt, dans la limite du rythme permis, au lieu de garder les comptes secondaires en secours. Amende 0027."
status: stable
tags: [budgets, forfaits, comptes, routage]
decided_by: human:smichea
decision_date: 2026-10-10
attestation: "décision du propriétaire dans sa conversation avec claude3 (mesh-design) ; non signée"
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-10T08:00:00+02:00" }
---

# Contexte

[0027](0027-bascule-automatique-entre-comptes.md) traite les comptes d'un
fournisseur comme une liste ordonnée : le primaire sert seul, et le suivant
ne prend le relais qu'au seuil. Le 2026-10-10, le compte Codex primaire était
à 20 % de sa fenêtre de 7 jours, pour un rythme permis de 52 %. Pendant ce
temps, la fenêtre de 5 h du compte secondaire, inutilisée, se remettait à
zéro sans avoir servi. Un forfait non consommé avant sa remise à zéro est
perdu. Le même jour, une estimation montrait que les forfaits dormaient
pendant que des modèles payés au token faisaient le travail.

S'y ajoutait un défaut : un relevé de jauge dont la fenêtre était échue
gardait sa dernière valeur (93 %). Le compte paraissait donc saturé, et
ameesh ne l'aurait jamais choisi.

# Décision

1. **Les comptes au forfait d'un fournisseur forment un réservoir**, et non
   une liste de secours. Pour chaque tour, ameesh choisit, parmi les comptes
   utilisables (identifiants présents, sous leur seuil de rythme de
   [0019](0019-budgets-et-routage.md)), celui dont **la capacité inutilisée
   expire le plus tôt**. Il retient la fenêtre la plus contraignante du
   compte, et à égalité, l'ordre déclaré. *Amendé le 2026-10-10, voir
   ci-dessous : le critère premier est désormais le retard sur le rythme.*
2. **Le rythme reste la garde.** Un compte n'est jamais poussé au-delà de
   `min(90 %, part écoulée + 10 points)` de sa fenêtre. La pause ne survient
   que lorsque tous les comptes sont au seuil.
3. **Un relevé périmé ne bloque pas.** Un relevé dont la fenêtre est échue
   compte pour 0 %. Un compte sans relevé récent est **relu** avant d'être
   écarté, ou essayé, puisque le premier tour rapporte sa jauge.
4. **Continuité des sessions.** On ne change pas de compte en cours de
   session pour un gain marginal. Une session reste sur son compte tant que
   celui-ci est sous son seuil. Le choix se fait à l'ouverture d'une session
   (0025 : une session par lot) et à la rotation, avec les règles de
   reprise ou de résumé de 0027 §5. Une session Codex n'est jamais portable
   d'un compte à l'autre.
5. **Transparence.** Chaque choix de compte est journalisé avec sa raison
   (« expire dans 52 min, 0 % utilisé »). `ameesh accounts list` montre, pour
   chaque compte, la capacité qui sera perdue à la prochaine remise à zéro si
   rien ne change.
6. **Forçage humain inchangé.** `ameesh accounts use` continue de primer, et
   `ameesh accounts auto` rend la main à cette règle.

# Conséquences

- Amende 0027 §2–3 : « bascule au seuil » et « retour au primaire » sont
  remplacés par ce choix. La pause quand tous les comptes sont au seuil, la
  journalisation et la continuité restent.
- Lot L74 (implémentation), après le correctif des relevés périmés (L71).
- Le respect des conditions d'utilisation de chaque fournisseur pour l'usage
  de plusieurs comptes reste de la responsabilité de l'équipe (0027).

# Amendement du 2026-10-10 : répartir entre tous les comptes

Accord du propriétaire le 2026-10-10 ; lot L117.

## Constat

Le soir même, `ameesh accounts list` montrait le compte Claude primaire à
37 % de sa fenêtre de 5 h (rythme 83 %) et à 35 % de sa fenêtre de 7 jours
(rythme 55 %). Les comptes secondaire et tertiaire affichaient « — » : jamais
utilisés. Leurs forfaits hebdomadaires étaient perdus, alors que trois
abonnements sont payés.

La règle du §1 en était la cause. Le primaire a toujours une fenêtre ouverte,
donc une capacité « qui expire », et il reste sous son rythme. Un compte sans
fenêtre ouverte n'a rien qui expire et passait après. Le primaire gagnait donc
toujours, et la continuité de session (§4) figeait la situation.

## Règle amendée (§1)

**Un compte jamais utilisé ne doit pas rester inutilisé.** Pour une nouvelle
session, ou à la rotation, ameesh retient, parmi les comptes utilisables
(identifiants présents, sous leur seuil de rythme dans **toutes** leurs
fenêtres), celui qui a **le plus de retard sur son rythme** :

- le retard d'un compte est le rapport entre l'utilisé et l'autorisé à cet
  instant de la fenêtre, `utilisé / min(90 %, part écoulée + 10 points)`. Le
  dénominateur est le plafond de la garde de [0019](0019-budgets-et-routage.md),
  celui qu'affiche la colonne « rythme ». Le plus petit rapport l'emporte ;
- le rapport est pris sur la **fenêtre la plus contraignante** du compte (le
  plus grand rapport parmi ses fenêtres), comme le §1 le faisait déjà. C'est la
  distance réelle du compte à la garde, qui l'écarte dès qu'une seule fenêtre
  atteint son plafond. Quand la fenêtre de 7 jours est la plus avancée, ce qui
  est le cas où un forfait hebdomadaire se perd, c'est elle qui décide. Une
  session neuve n'est pas envoyée vers un compte dont la fenêtre de 5 h touche
  presque son seuil, même si sa semaine est en retard ;
- un compte sans fenêtre ouverte (relevé échu) ou sans relevé compte pour 0 %
  utilisé. Il passe donc en premier, et son premier tour rapporte sa jauge ;
- à égalité de rapport, la capacité inutilisée qui **expire le plus tôt** (la
  règle du §1 d'origine), puis l'ordre déclaré.

Chaque nouvelle session ajoute de l'usage au compte choisi, qui recule ainsi
dans le classement. Les sessions se répartissent entre les comptes au lieu de
s'accumuler sur le primaire.

## Inchangé

Le forçage (`ameesh accounts use`, §6), la continuité tant que le compte de la
session est sous son seuil (§4), la pause quand tous les comptes sont au seuil
(§2) et la règle de Codex, dont une session n'est jamais portable d'un compte à
l'autre.

## Transparence (§5)

La raison journalisée dit le retard du compte choisi, puis celui des comptes
qui le suivent : « compte claude : secondaire — sans relevé : 0 % utilisé, le
plus en retard sur son rythme ; avant : tertiaire sans relevé : 0 % utilisé,
primaire seven_day 35 % utilisé (rythme 55 %) ». `ameesh accounts list` donne
la même raison pour le prochain choix d'une nouvelle session.
