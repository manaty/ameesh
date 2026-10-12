---
type: Decision
title: "ameesh names no client; it publishes the interfaces that allow building its front ends"
description: "No client name in the ameesh project; public interfaces to develop applications (including native mobile ones) without imposing Nexlink."
status: stable
tags: [confidentialite, interfaces, mobile, nexlink]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T01:20:00+02:00" }
---

# Context

Question Q6 was about a native mobile application for a client
organisation: a generic ameesh application or integration into the applications of
that organisation.

# Decision

- **The ameesh project mentions no client**: no name, no internal system,
  no repository of a client organisation, in the code, the documentation or the
  examples.
- **Nexlink**, a Manaty product, is not a client: ameesh names it and
  integrates it natively ([0003](0003-ameesh-et-nexlink.md)).
- ameesh **does not develop an application for a client**: it **provides the
  interfaces** that will allow an organisation to develop its own
  (notably a native mobile application), **so as not to have to impose
  Nexlink**.

# Consequences

- Public, documented interfaces, at the same level as the Nexlink connector:
  - **approval**: request and receipt format, verification by front end
    (WebAuthn, ES256 device key…), enrolment of an authenticator in the
    canon registry;
  - **thread and notifications**: read and post in a lot's thread, receive the
    requests waiting for a human (R17);
  - **identity**: binding of a `human:<id>` member to their authenticators.
- Nexlink is one front end among others, integrated natively; a third-party
  application on these interfaces has the same capabilities.
- Examples and tests use fictitious organisations.
- **Going public through a snapshot** (owner decision, formerly Q8):
  the git history of the private repository, which contains earlier mentions of a
  client, stays private; the public repository starts from a clean snapshot at the
  first release. No rewriting of the history shared by the agents.
- **SQL migrations**: their comments are covered by a checksum
  stored in the database and cannot be modified without breaking existing
  schemas. Temporary exception to this rule: since no production
  database exists, the migrations will be **consolidated into a clean base**
  at the time of the public snapshot.
