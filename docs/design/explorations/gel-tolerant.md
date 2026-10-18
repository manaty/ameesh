---
type: Exploration
status: exploration
title: "Gel tolérant aux changements sans effet"
---

# Gel tolérant aux changements sans effet

## Problème

Un lot en file de fusion est lié à son commit gelé (`frozen_sha`). Au
passage du radar, une branche dont la tête n'est plus exactement ce commit
est éjectée (`merge_queue.py`, « branch moved past its freeze ») : son agent
doit regeler, faire relire, puis remettre le lot en file, et il perd son
rang. La règle ne regarde pas ce qui a changé : corriger une faute dans un
commentaire coûte autant qu'un changement de logique.

La politique de revue (`ameesh review-class`, `review_policies` du canon)
classe déjà un changement en `light`, `normal` ou `sensitive`, mais **par
chemin de fichier** : elle dit où un changement a lieu, pas de quelle
nature il est.

## Intuitions

- **Classer le delta entre le gel et la nouvelle tête**, pas le lot entier.
  Le gel avancerait sur le nouveau commit sans éjection quand le delta est
  sans effet, et ce prolongement serait écrit en base avec sa
  justification (par exemple « commentaires seuls, AST identique »).
- **Distinguer ce qui se prouve de ce qui se juge.**
  - Prouvable mécaniquement : commentaires seuls ou formatage pur (l'arbre
    syntaxique est identique ; en Python, `ast` ignore les commentaires),
    documentation seule dans des chemins `light`, renommage d'une variable
    locale (arbre identique au renommage près, dans sa portée).
  - Non prouvable : refactoring localisé (extraire une fonction,
    réordonner). Plutôt qu'une éjection, une revue du seul delta, le lot
    gardant son rang.
  - Tout le reste : éjection, comme aujourd'hui.
- **Deux dimensions pour la revue** : la classe du chemin (existante) et la
  nature du changement. C'est l'idée des règles d'approbation sémantiques
  décrites par Anthropic pour son outil de revue (voir Contexte).

## Questions ouvertes

- Combien d'éjections « branch moved past its freeze » le radar
  prononce-t-il (raison dans `merge_queue.reason`, note dans le journal du
  lot), et quelle part a un delta fait uniquement de commentaires, de
  formatage ou de documentation ? Combien de temps coûte chaque éjection
  (regel, revue, retour en file, rang perdu) ?
- Certains commentaires sont des directives : `# type: ignore`, `# noqa`,
  `# pragma: no cover`, `# fmt: off`, shebang, déclaration d'encodage.
  Faut-il une liste fermée de commentaires qui comptent comme du code ?
- Les docstrings font partie de l'arbre syntaxique Python : faut-il une
  règle à part pour elles ?
- Quelle preuve pour chaque langage (SQL, shell, TypeScript, YAML) ? Les
  migrations SQL, classées `sensitive`, doivent-elles toujours éjecter ?
- Un commentaire modifié dans un fichier `sensitive` peut-il prolonger le
  gel, ou la classe du chemin l'emporte-t-elle toujours ?
- La CI certifiée sur le commit gelé reste-t-elle valable pour un delta
  prouvé sans effet, ou faut-il la relancer sur la nouvelle tête
  (`ci_required`) ?
- Pour la revue du seul delta : même réviseur que pour le gel, ou
  n'importe quel réviseur ?

Contexte : [Conditions d'atterrissage](../../LANDING-CONDITIONS.md),
[Orchestrateur, file de fusion par cible](../../ORCHESTRATEUR.md),
[Radar de conflits](../etudes/l160-radar-de-conflits.md), et
[Redesigning source control at Anthropic](https://www.anthropic.com/engineering/redesigning-source-control)
(Scion et l'outil de revue Antique, règles d'approbation sémantiques). Ces
références éclairent la question ; elles ne promeuvent pas cette fiche en
étude ou en travail décidé.
