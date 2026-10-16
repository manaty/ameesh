---
type: Exploration
status: exploration
title: "Signals and the self-repair loop"
---

# Signals and the self-repair loop

## Problem

An error signal may be duplicated, misinterpreted or disappear before its cause is established. Automatic repair can amplify this defect if it turns a signal into an authorization.

## Intuitions

Reproducing the defect before changing anything, correlating the signals and bounding the attempts could make a repair loop observable. Diff limits and human escalation could remain explicit.

## Open questions

- How can a confirmed defect be told apart from a feature request?
- Which signals prove that the cause is fixed rather than hidden?
- When should the loop stop and hand the evidence to a human?

Existing context: [Study of the self-repair loop](../etudes/boucle-auto-reparation.md). These references inform the question; they do not promote this card to a study or to decided work.
