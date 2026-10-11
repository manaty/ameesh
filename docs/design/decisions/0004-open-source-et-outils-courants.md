---
type: Decision
title: "Open source alternatives and connectors to common tools"
description: "ameesh must run on an open source stack (even if that means creating the missing piece) and connect to common tools such as Slack."
status: stable
tags: [open-source, connecteurs]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Context

Nexlink must not be mandatory to use ameesh.

# Decision

- ameesh must work with **open source alternatives to Nexlink**, **even if that means creating them** when they do not exist.
- In addition, connectors to the **most common tools** for communication: **Slack** first; Teams, Google Chat, e-mail next.

# Consequences

- Reference open source stack from the studies: Matrix (messaging, directory), OKF in git on Forgejo (canon), ameesh-approve (authority, to be created), Zammad / Odoo + OCA + Karrio (trades).
- Slack: one internal app per organisation (API terms); the reference log is never Slack.
