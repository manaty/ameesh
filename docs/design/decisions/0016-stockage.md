---
type: Decision
title: "Storage: Postgres for v1, SQL behind an interface, SQLite later for personal mode"
description: "Postgres everywhere in v1, installed locally by the installer; all SQL grouped behind a storage interface; a SQLite driver for the local profile in a later version."
status: stable
tags: [stockage, postgres, sqlite, hebergement]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T01:30:00+02:00" }
---

# Context

The ameesh database now only holds the execution state (leases, turns, lots,
registry of consumed approvals) and the mailbox in standalone
mode; it follows the hosting profile ([0011](0011-hebergement-configurable.md)).
For a personal project run locally, a Postgres is one more service to install,
start and back up (measured on the PC: ~7 MB at rest, ~180 MB after a
test suite run, `postgres:17` image of 643 MB). The v1 code uses about 140
Postgres-specific constructs (LISTEN/NOTIFY, row locks, query
timeouts, `jsonb`, advisory lock for migrations).

# Decision

1. **v1: Postgres everywhere.** Locally, it is the **installer** (driven by the
   user's harness) that sets it up — container or system package.
2. **All SQL is grouped behind a storage interface**, so that another
   driver can be added without touching the rest.
3. **SQLite for the local (personal) profile in a later version**, if the
   need is confirmed.

# Consequences

- Change request to be examined with the revised spec: extract the scattered SQL
  accesses (registry, mailbox, authority, lots, runner) into a single storage
  module, with named operations (take a lease, renew,
  deposit, consume a nonce…) rather than queries.
- In single-runner local mode, the SQLite driver will be able to replace LISTEN/NOTIFY
  with a local wake-up and row locks with simple transactions; the
  test suite will have to run on both drivers.
