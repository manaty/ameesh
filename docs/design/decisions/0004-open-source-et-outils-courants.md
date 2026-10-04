---
type: Decision
title: "Alternatives open source et connecteurs vers les outils courants"
description: "ameesh doit tourner sur une pile open source (quitte à créer la brique manquante) et se connecter aux outils courants comme Slack."
status: stable
tags: [open-source, connecteurs]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Contexte

Nexlink ne doit pas être obligatoire pour utiliser ameesh.

# Décision

- ameesh doit fonctionner avec des **alternatives open source à Nexlink**, **quitte à les créer** si elles n'existent pas.
- En plus, des connecteurs vers les **outils les plus courants** pour la communication : **Slack** d'abord ; Teams, Google Chat, e-mail ensuite.

# Conséquences

- Pile open source de référence issue des études : Matrix (messagerie, annuaire), OKF dans git sur Forgejo (canon), ameesh-approve (autorité, à créer), Zammad / Odoo + OCA + Karrio (métiers).
- Slack : une application interne par organisation (conditions d'API) ; le journal de référence n'est jamais Slack.
