---
type: Agent
title: orchestre
description: "Orchestrateur du projet acme-web."
responsible: human:alice
team: acme-web
capabilities: [read, report-drift, propose]
harness: claude
model: claude-opus
provider: anthropic
credential_mode: subscription
budget_usd_per_day: 30
tools: [git, "mcp:transport-readonly"]
reviewers: [relecteur]
generated: { by: "humain", at: "2026-10-04T09:00:00+02:00" }
---

# orchestre

Répartit les lots, lit les décisions du canon, ne décide jamais à la place
d'un humain (pas de capacité `approve`).
