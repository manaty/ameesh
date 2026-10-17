---
type: Exploration
status: exploration
title: "SQLite driver for a personal profile"
---

# SQLite driver for a personal profile

## Problem

A Postgres service can weigh down a personal installation on a single machine. The storage interface exists, but Postgres guarantees do not transpose automatically to SQLite.

## Intuitions

A local profile with a single executor could replace some wake-ups and locks with local mechanisms. The storage decision already makes this lead conditional on confirming the need.

## Open questions

- Which personal uses justify a second driver?
- How can leases, transactions and single consumption of receipts be preserved?
- Which profile limits must stay visible when several processes access the database?

Existing context: [Decision on storage and the personal lead](../decisions/0016-stockage.md), [Specification of the storage interface](../specification.md). These references inform the question; they do not promote this card to a study or to decided work.
