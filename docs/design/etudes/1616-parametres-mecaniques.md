---
type: Study
title: "Étude — paramètres mécaniques du processus compilés depuis le canon (ameesh #1616)"
description: "Contrat YAML versionné des règles mécaniques (coupures et fuseau, plafond de lots en build, CI par paquet et incident, familles et indépendance), lecteur strict `ameesh.mechanics`, compilation signalée par `canon sync`, première consommation par le train #1467 ; les autres consommateurs restent déclarés, non activés."
status: draft
tags: [canon, mecanique, train, revue, ci, parametres]
generated: { by: "mty-amh-impl-07/deepseek", at: "2026-10-15T12:50:00+02:00" }
stale_after: 2027-01-15
sources:
  - { resource: "https://github.com/manaty/home/pull/86", title: "canon §7 : le mécanique est du code (owner, 15/10/2026 11:50)" }
  - { resource: "https://github.com/manaty/home/pull/85", title: "risk_classes et train_reviews déclarés (mesh-design, 15/10/2026)" }
  - { resource: "../../src/ameesh/mechanics.py", title: "lecteur strict et compilation" }
  - { resource: "../../src/ameesh/canon_sync.py", title: "canon sync : constat `mechanics`" }
---

# Contexte

Le canon §7 (owner, 15/10/2026 11:50, home PR86) remplace les gestes
mécaniques des agents par de l'outillage : « les paramètres de ces gestes
viennent du canon. Chaque règle mécanique a ici un bloc lisible par machine,
d'où l'outillage est compilé ou configuré. » Le lot ameesh #1616, sous-lot de
#1606, livre ce lecteur et ce contrat pour le premier consommateur : le train
d'intégration #1467 (coupures et politique de revue).

# Contrat du canon

Un document, un bloc par règle, dans `manaty/home` :

- `processus/mecanique.yaml`, schéma `ameesh-mechanics/1` :
  - `timezone` (IANA) et `cutoffs` (`"HH:MM"` locaux) : les coupures du train ;
  - `build.max_in_build` : plafond de lots en développement par agent ;
  - `ci.packet` (`full` | `impacted` | `none`) et `ci.incident_label` :
    la suite par paquet, et l'étiquette qui autorise la CI d'un lot ;
  - `reviews.families`, `reviews.exclude_author_family`,
    `reviews.distinct_families` : familles de modèle et indépendance ;
  - `projects.<nom>` : surcharge d'un bloc pour un projet (le bloc présent
    remplace le bloc global ; aucune activation implicite).
- `federation.yaml`, au **même commit** :
  - `review_policies.risk_classes.projects.<projet>` : les classes sensibles ;
  - `review_policies.train_reviews.projects.<projet>.rules`, schéma
    `ameesh-train-reviews-policy/1` : `(paths, reviews 1|2)` par chemin.

Les classes et les nombres d'avis ne sont pas répétés dans `mecanique.yaml` :
le compilateur les lit à la source (home PR85), comme le fait déjà
`train_policy.Versioned` de #1467.

# Lecteur strict

`ameesh.mechanics` est pur (aucune base) et ne connaît pas le train :

- `load(repo, commit, project) -> Mechanics` : commit exact obligatoire
  (jamais une branche ou un tag), fichier régulier à ce commit, document
  global + surcharge du projet + politiques de revue du projet.
- `load_global(repo, commit) -> Document` : le document seul, pour le constat
  de `canon sync`.
- Aucun repli : schéma inconnu, clé inconnue, bloc manquant, heure ou fuseau
  invalide, projet non déclaré, motif d'avis mal formé, chemin non léger sans
  nombre d'avis → refus (`MechanicsError`).
- `Mechanics.cutoff(now)` et `next_cutoff(now)` : la coupure locale la plus
  récente / la suivante, fuseau IANA et heure d'été comprises (même contrat
  que `train.cutoff`).
- `Mechanics.reviews_for(paths) -> ReviewNeed(classe, avis)` : la classe et le
  nombre d'avis d'un changement, refus si un chemin non léger n'est pas couvert.

`canon sync` compile le document global au commit appliqué et le **signale**
(`mechanics` du rapport : `ok`, `absent`, `invalid`, `untrusted`). Un document
absent ou invalide n'est jamais un constat bloquant : c'est le consommateur
raccordé qui refuse, jamais l'outil qui devine.

# Carte de raccord (déclarée, aucune activation par ce lot)

| Consommateur | Paramètres compilés | État |
|---|---|---|
| Train #1467 | `cutoffs`, `timezone`, `ci.*`, `reviews.*`, `rules` | port à faire par cx3, contrat transmis ; **non activé** |
| Désignation #1617 | `reviews.families`, `exclude_author_family`, `distinct_families`, `commit` | port à faire par impl-02 ; **non activé** |
| Déclencheurs CI #1606(2) | `ci.packet`, `ci.incident_label` | raccord à ouvrir ; **non activé** |
| Plafond 2 lots en build | `build.max_in_build` | consommateur à désigner ; **non activé** |
| Staging #1606(5), comptes #1606(6) | blocs à déclarer plus tard | hors périmètre |

Le refus par la CI d'un paramètre codé en dur qui ne vient pas du canon est un
geste de CI distinct (#1606 point 2/7) : ce lot ne le pose pas.

# Preuve attendue

Le lot n'est livré que par la consommation réelle : une coupure sans agent
avec le train raccordé, la mesure avant/après (durée de coupure, incidents).
La préparation seule — ce document, le lecteur, ses témoins — ne vaut pas
activation.
