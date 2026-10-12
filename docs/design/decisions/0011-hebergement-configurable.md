---
type: Decision
title: "Hosting is a configuration, prepared by the user's harness"
description: "No imposed machine: local, Nexlink page or provisioned VPS; installation is driven by the agent harness the user already has."
status: stable
tags: [hebergement, installation, provisionnement]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:40:00+02:00" }
---

# Context

The open question M4 ("first machine besides the PC") assumed an
infrastructure choice made once and for all.

# Decision

- **Where ameesh runs is a configuration, per project**, not an architecture
  decision. Possible profiles: **local** (personal project, one
  machine), **Nexlink page** (hosted by a page server), **provisioned
  VPS** (proposed and created by infrastructure code), and by
  extension an existing server or cluster.
- **The person who launches ameesh locally already has an agent harness** (Claude
  Code, Codex, DeepSeek Harness…): we use it **right away** for
  **intelligent provisioning** — it asks the user where they want to
  host, or whether they stay local for a personal project, then prepares the
  configuration.

# Consequences

- ameesh is also distributed as an **installation skill / plugin** following
  the harness standards ([0007](0007-standards-des-harnais.md)): interview with
  the user, choice of profile, bootstrapping of the canon (OKF root, federation,
  cards of the first agents), deployment of the runner and the database,
  enrolment of the user's passkey in ameesh-approve.
- Provisioning of a VPS with **OpenTofu** (MPL-2.0, Linux Foundation) rather
  than Terraform (BSL since 2023) to stay open source.
- **Any spending** (VPS, domain) is a costly action: it goes through a
  signed approval from the user — the first ameesh-approve ceremony
  serves to approve one's own infrastructure.
- The database question (formerly M3) follows the profile: it lives where the
  runner runs, backed up off the machine as soon as one leaves the local profile.
