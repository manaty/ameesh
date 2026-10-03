---
type: Decision
title: "Toute parole est signée ; pas de procédure provisoire avant ameesh v1"
description: "Les messages des humains et des agents doivent être signés pour faire preuve ; nos sessions actuelles restent sans signature."
status: stable
tags: [securite, signature, autorite]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Contexte

mesh-design transmettait à l'orchestrateur des « décisions du propriétaire » que rien ne prouvait ; l'orchestrateur les croyait.

# Décision

- Ce que disent les auteurs, **humains comme agents**, doit être **signé** pour faire preuve : signature de **provenance** sur chaque message, signature d'**autorité** explicite pour une décision.
- **Pas de règle provisoire** : tant qu'ameesh v1 n'est pas en service, les sessions actuelles restent sans signature (« c'est pas sécurisé, c'est pas grave »).

# Conséquences

- Prérequis : une clé par agent que les autres ne peuvent pas lire (utilisateur Unix ou conteneur par agent, ou signature faite par l'exécuteur au nom de l'agent).
- Une décision rapportée par un agent n'est une preuve que si l'humain l'a signée (ameesh-approve, passkey sur le téléphone) ; elle est alors enregistrée comme `Decision` avec sa signature.
- Dans Slack, Teams ou Google Chat, l'identité de la plateforme reste un indice, jamais une preuve.
