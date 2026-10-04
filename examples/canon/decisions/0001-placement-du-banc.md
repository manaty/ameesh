---
type: Decision
title: "Le banc n'accepte que des agents à la clé d'API"
description: "Exemple de fiche d'un autre type : ameesh l'ignore (seuls Agent, Host, Placement et Member sont lus)."
status: stable
tags: [placement, infrastructure]
decided_by: human:bruno
decision_date: 2026-10-04
sources:
  - { resource: "../hotes/banc.md", title: "Hôte banc" }
  - resource: "../placements/ouvrier-banc.md"
    title: "Placement de l'ouvrier"
generated: { by: "humain", at: "2026-10-04T09:30:00+02:00" }
---

# Contexte

Le banc est partagé ; sa dépense doit rester mesurable.

# Décision

Seuls les agents facturés à la clé d'API y sont placés.
