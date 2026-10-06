---
type: Host
title: banc
description: "Serveur de test : DeepSeek et Codex seulement, clé d'API seulement."
responsible: human:alice
policy:
  harnesses:
    - deepseek
    - codex
  providers: [deepseek, openai]
  credential_modes: [api-key]
  work_roots: {acme-web: /srv/acme/acme-web}
  max_agents: 2
---

# banc

Serveur partagé. Son responsable n'y admet que des agents facturés à la clé
d'API, pour que la dépense reste mesurable.
