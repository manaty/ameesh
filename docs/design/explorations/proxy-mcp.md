---
type: Exploration
status: exploration
title: "Gate exposed as an MCP proxy"
---

# Gate exposed as an MCP proxy

## Problem

An agent's tools can expose actions that require approval. An MCP facade in front of the gate raises the question of preserving the action contract and the signed proof.

## Intuitions

A proxy could present the tools while linking each call to a durable action and to its policy. A tool's description and the arguments received could remain distinct from the authorization to execute.

## Open questions

- How can the executed arguments be bound to the approved content?
- How can an unknown result and an idempotent resume be exposed to an MCP client?
- How can the capabilities be exposed without widening the agent's rights?

Existing context: [Specification of actions and the gate](../specification.md), [Study of human authority](../etudes/autorite-humaine.md). These references inform the question; they do not promote this card to a study or to decided work.
