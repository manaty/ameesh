---
type: Host
title: atelier
description: "Poste de travail d'acme : tous les harnais, forfait ou clé d'API."
responsible: human:bruno
policy:
  harnesses: [claude, codex, deepseek]   # absent = tous
  providers: [anthropic, openai, deepseek]
  credential_modes: [api-key, subscription]
  max_agents: 4
---

# atelier

Machine de développement. Les secrets (clés, jetons de forfait) sont dans le
trousseau de l'hôte, jamais dans ce canon.
