---
type: Process
title: acme-work-delivery
description: "Exemple minimal d'une carte Process qui porte un workflow exécutable."
responsible: human:alice
updated: 2026-10-21
workflow:
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

Exemple **fictif** du canon `acme`. Ce bloc `workflow:` n'exécute rien : il
est encodé en document natif `process-workflow/1`, compilé par
`process_workflow.compile_workflow`, puis admis explicitement par la
configuration de l'hôte. Le texte de la carte reste la règle de travail
injectée dans les briefs ; la prose ne porte aucune transition.
