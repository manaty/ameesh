---
type: Reference
title: "Questions ouvertes de conception"
description: "Ce qui attend une décision du propriétaire ou une étude, après la session du 2026-10-03."
status: draft
tags: [questions, decisions]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:30:00+02:00" }
---

# Attend le propriétaire

Q1 à Q8 tranchées au 2026-10-04 (voir
les [décisions](decisions/)). Ouvert au 2026-10-05 :

- Persona et harnais ([étude](etudes/persona-et-harnais.md)) : amender
  [0029](decisions/0029-persona-et-session.md) pour faire de la mémoire de
  persona (un dépôt git par persona) un format neutre, géré par un composant
  séparé, qui peut durer sans harnais maison ; mémoires natives des harnais
  désactivées par défaut ; vérification de la règle de visibilité (API de la
  forge ou copie d'essai, administrateurs des hôtes) ; création des
  identifiants limités au dépôt ; qui consolide ; fiche Persona dans L31 ou
  lot à part.

# Attend une étude ou un prototype

- Licences : rédaction du CLA et titularité des contributions produites par
  des agents ; compatibilité, version par version, des dépendances distribuées
  et des intégrations avec l'AGPL-3.0-only ([0013](decisions/0013-licence-agpl.md)).

- Profil ameesh d'OKF Federation : types `Agent`, `Team`, `WorkPackage`,
  `Pipeline` ; liaison `human:<id>` → passkey / clé d'appareil ; `agent:<id>` → compte.
  Une partie pourrait remonter dans okf-federation.
- Isolation des agents (un utilisateur Unix ou un conteneur par agent) : coût,
  compatibilité avec les harnais et leurs sessions.
- File des décisions par humain (R17) : où vit-elle (Nexlink, ameesh, canon) ?
- Allocation des agents partagés entre projets : règle déclarative dans le canon.
- Analyse détaillée des projets homonymes (tlhc/amesh, samchung95/amesh,
  code-rabi/amesh) pour en reprendre les bonnes idées.
