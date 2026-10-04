---
type: Decision
title: "Tous les échanges sont lisibles et auditables par les humains"
description: "Les agents se parlent en langage humain, dans des canaux que l'équipe peut lire ; aucun canal caché."
status: stable
tags: [audit, messagerie]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Contexte

« On a encore la chance que les IA soient des LLM et se parlent entre eux en langage humain, profitons-en. » (le propriétaire)

# Décision

Tous les messages, **y compris d'agent à agent**, sont **auditables par les humains** : en langage naturel, dans un fil que l'équipe peut lire, retrouver et auditer.

# Conséquences

- La boîte aux lettres d'ameesh n'est qu'une file ou un cache ; la référence est le fil lisible.
- Le corps d'un message se suffit à lui-même ; les métadonnées structurées s'ajoutent, jamais ne remplacent.
- Un fil par lot.
- Lisible ne veut pas dire autorisé : l'autorité reste prouvée par signature ([0009](0009-paroles-signees.md)).
