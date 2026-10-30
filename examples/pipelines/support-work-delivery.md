---
type: Pipeline
title: support-work-delivery
name: support-work-delivery
description: "Exemple non-dev : une demande de support après-vente, de la réception à la réponse."
responsible: human:alice
updated: 2026-10-30
pipeline:
  schema: process-workflow/1
  initial: new
  types: [docs, investigation, governance-tracking, human_operation]
  stages:
    new: {core: intake, status: New}
    triage: {core: intake, status: Triage}
    draft: {core: build, status: Draft}
    approval: {core: qa, status: Approval}
    sent: {core: promoted, status: Sent}
    closed: {core: closed, status: Closed}
  transitions:
    - {from: new, to: triage, guard: planning}
    - {from: triage, to: draft, guard: start_work}
    - {from: draft, to: approval, guard: review_requested}
    - {from: approval, to: draft, guard: qa_repair}
    - {from: approval, to: sent, guard: work_completed}
    - {from: [new, triage, draft, approval], to: closed, guard: stop_work}
  projection:
    project: support
    status_field: Stage
    product_field: Queue
    issue_open: [new, triage, draft, approval]
    issue_completed: [sent]
    issue_not_planned: [closed]
---

# support work delivery

Exemple **fictif** non-dev du canon `acme` : une demande de support après-vente
suit un pipeline déclaré en données, sans code Python. Les types couverts
(`docs`, `investigation`, `governance-tracking`, `human_operation`) sont
non-code : la garde `work_completed` exige donc un résultat propre au type (la
référence `outcome`), pas une livraison logicielle.

- `new` : la demande est reçue, non triée.
- `triage` : la demande est lue, qualifiée et planifiée.
- `draft` : une réponse est rédigée et vérifiée.
- `approval` : un relecteur indépendant relit la réponse (`review_requested`),
  et peut la renvoyer en rédaction (`qa_repair`).
- `sent` : la réponse a été envoyée au client ; la demande est terminée.
- `closed` : la demande est abandonnée ou remplacée (`stop_work`), jamais
  rouverte : un cœur terminal ne se rouvre pas.

La projection ne classe que des étapes : `new`, `triage`, `draft` et
`approval` restent ouvertes, `sent` est terminée, `closed` n'est pas planifiée.
La même carte se copie pour un autre projet, en changeant `project`,
`status_field`, `product_field` et les libellés.

Le guide [Écrire un pipeline](../../site/docs/guides/write-a-pipeline.md)
décrit la forme complète, l'admission et le test de bout en bout.
