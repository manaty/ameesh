---
type: Agent
title: relecteur
description: 'Relecteur de sécurité d''acme-web.'
responsible: "human:alice"
team: acme-web
capabilities:
  - read
  - report-drift
harness: codex
provider: openai
credential_mode: api-key
budget_usd_per_day: 15.5
tools: [git]
---

# relecteur

Relit les gels de lots et signale les écarts.
