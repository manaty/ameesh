---
type: Decision
title: "Nom : ameesh, logo carriole, dépôt manaty/ameesh"
description: "Le paquet agent-mesh devient ameesh ; renommage complet immédiat ; dépôt privé puis public à la première release."
status: stable
tags: [nom, depot, release]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Contexte

« amesh » est pris par trois projets d'agents (tlhc/amesh, samchung95/amesh, code-rabi/amesh) et par le paquet PyPI amesh (radar de pluie de Tokyo). « agent-mesh » est le nom d'un produit commercial (Solace Agent Mesh). « ameesh » est libre sur PyPI, npm, crates.io, Homebrew, AUR et Docker Hub ; c'est par ailleurs un prénom indien, sans projet logiciel homonyme.

# Décision

- Nom : **ameesh** (prononcé comme *amish*). Logo : une **carriole amish** (buggy noir à capote).
- Portée du renommage : **tout le projet** — dépôt, module Python, commandes (une commande `ameesh` à sous-commandes), variables d'environnement ; `agent-mail` reste un alias car les hooks des harnais l'appellent.
- Moment : **immédiat**, sans attendre la fin de la revue de sécurité en cours.
- Dépôt : **github.com/manaty/ameesh**, **privé** maintenant, **public après la première release**.

# Conséquences

- Dépôt créé privé le 2026-10-03 (main à c4d11d5).
- Avant le passage public : retirer le DSN de banc avec mot de passe codé en dur, passer l'historique au détecteur de secrets, choisir une licence (décision du propriétaire à venir).
