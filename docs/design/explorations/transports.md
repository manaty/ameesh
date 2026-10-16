---
type: Exploration
status: exploration
title: "Additional transports for threads"
---

# Additional transports for threads

## Problem

Teams also use chat and e-mail. Adding these transports raises questions of readability, duplicates and provenance without changing the meaning of the reference thread.

## Intuitions

A common interface could preserve the same guarantees for every transport. Platform identity could remain a clue distinct from the proof of provenance and authority.

## Open questions

- How can order be preserved and a duplicate delivery recognized?
- What should be done with unreadable contents or attachments?
- How can a reply be attached to its thread without making the platform the source of authority?

Existing context: [Study of messaging and the directory](../etudes/messagerie-annuaire.md), [Decision on open connectors](../decisions/0004-open-source-et-outils-courants.md). These references inform the question; they do not promote this card to a study or to decided work.
