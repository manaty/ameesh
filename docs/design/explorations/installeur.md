---
type: Exploration
status: exploration
title: "Installer driven by the user's harness"
---

# Installer driven by the user's harness

## Problem

An installation requires linking the canon, the storage, the services and the approvers. The harness the person already uses could guide this preparation, but its rights and its errors remain to be examined.

## Intuitions

A readable walkthrough could explain the choices and the prerequisites, then verify the resulting state. Installation and human enrolment could remain two distinct operations.

## Open questions

- How can the existing state be detected and resumed without overwriting the configuration?
- Which operations require an explicit user action?
- How can the installation be verified on different machines without depending on a particular harness?

Existing context: [Decision on local storage and the installer](../decisions/0016-stockage.md), [Existing cutover plan](../bascule-v1.md). These references inform the question; they do not promote this card to a study or to decided work.
