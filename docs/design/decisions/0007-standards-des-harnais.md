---
type: Decision
title: "Les intégrations reprennent les standards des outils d'agents"
description: "Pas de format de connecteur propre à ameesh : MCP, skills, plugins des harnais."
status: stable
tags: [mcp, skills, integration]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Contexte

Claude Code, Codex et DeepSeek Harness parlent tous MCP (vérifié localement : `codex mcp`, `codex plugin`, dsh embarque les SDK MCP et ACP).

# Décision

Les intégrations avec les **systèmes métier** et la **communication** (Slack, Teams…) reprennent **les standards des outils et plugins des agents** (Codex, Claude, DeepSeek…) ; ameesh n'invente pas son propre format.

# Conséquences

- Sortant : serveurs MCP standard (officiels quand ils existent : Slack, Microsoft, Google, GitHub).
- Entrant : aucun standard portable ; le runner d'ameesh reçoit l'événement et réveille l'agent (Claude `-p --resume`, Codex app-server, ACP pour les autres).
- ameesh fournit un serveur MCP « ameesh-fil », une porte MCP « ameesh-gate » (approbation signée liée à l'empreinte des arguments), et un générateur canon OKF → skills, AGENTS.md, politique d'outils, plugin par harnais. Voir [l'étude](../etudes/standards-des-harnais.md).
