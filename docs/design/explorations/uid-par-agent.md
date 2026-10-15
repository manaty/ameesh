---
type: Exploration
status: exploration
title: "One Unix user per agent"
---

# One Unix user per agent

## Problem

Agents sharing a system user can access the same files and secrets. A message signature does not prove reliable provenance if the other agents can read the key.

## Intuitions

Separate system identities, containers or signing by the executor could isolate the keys. These leads require telling message provenance apart from the authority of a human decision.

## Open questions

- Which isolation actually protects a key against the other agents?
- How can a work repository be shared without sharing the secrets?
- How can verifiable provenance be preserved across a change of host or harness?

Existing context: [Decision on signed words](../decisions/0009-paroles-signees.md), [Study of human authority](../etudes/autorite-humaine.md). These references inform the question; they do not promote this card to a study or to decided work.
