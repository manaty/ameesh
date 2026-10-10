---
type: Decision
title: "Consommer d'abord la capacité qui expire le plus tôt"
description: "Entre les comptes au forfait d'un même fournisseur, ameesh choisit pour chaque tour le compte dont la capacité inutilisée expire le plus tôt, dans la limite du rythme permis, au lieu de garder les comptes secondaires en secours. Amende 0027."
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
   compte, et à égalité, l'ordre déclaré.
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
