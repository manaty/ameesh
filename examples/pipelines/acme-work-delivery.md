---
type: Pipeline
title: acme-work-delivery
name: acme-work-delivery
description: "Exemple de pipeline déclaratif : la livraison de travail du canon acme."
responsible: human:alice
updated: 2026-10-30
pipeline:
  schema: process-workflow/1
  initial: draft
  types: [bug, evolution, docs, investigation, governance, promotion, human_operation]
  stages:
    draft: {core: intake, status: Draft}
    backlog: {core: intake, status: Backlog}
    sprint: {core: intake, status: Sprint}
    implementation: {core: build, status: In Progress}
    review: {core: qa, status: Dev review}
    tests: {core: qa, status: Dev tests}
    done: {core: promoted, status: Done}
    stopped: {core: closed, status: Stopped}
  transitions:
    - {from: [draft, backlog, sprint], to: backlog, guard: planning}
    - {from: [draft, backlog, sprint], to: sprint, guard: planning}
    - {from: [draft, backlog, sprint], to: implementation, guard: start_work}
    - {from: implementation, to: review, guard: review_requested}
    - {from: review, to: tests, guard: qa_started}
    - {from: [review, tests], to: implementation, guard: qa_repair}
    - {from: tests, to: done, guard: work_completed}
    - {from: [draft, backlog, sprint, implementation, review, tests], to: stopped, guard: stop_work}
  projection:
    project: acme
    status_field: Stage
    product_field: Product
    issue_open: [draft, backlog, sprint, implementation, review, tests]
    issue_completed: [done]
    issue_not_planned: [stopped]
---

# acme work delivery

Exemple **fictif** du canon `acme`, écrit comme une **carte Pipeline** : un
pipeline déclaré en données, sans code Python. Le bloc `pipeline:` ci-dessus a
exactement la forme du bloc `workflow:` d'une carte Process ; `canon check` le
compile en document natif `process-workflow/1`, `ameesh modules` l'expose comme
module `acme-work-delivery`, et un lot de type couvert peut l'adopter :

```bash
ameesh module acme-work-delivery adopt 42
ameesh module acme-work-delivery advance 42 --to backlog --guard planning
ameesh module acme-work-delivery hold 42 --hold blocked
ameesh module acme-work-delivery release 42
```

La carte n'est **pas une règle de travail** : elle n'est jamais injectée dans
un brief, et sa prose ne porte aucune transition. L'exemple Process équivalent
reste [`examples/processus/acme-work-delivery.md`](../processus/acme-work-delivery.md),
témoin de la forme historique où le workflow vivait dans une carte Process.

Le guide [Écrire un pipeline](../../site/docs/guides/write-a-pipeline.md)
décrit la forme complète, l'admission et le test de bout en bout.
