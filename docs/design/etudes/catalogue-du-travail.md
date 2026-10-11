---
type: Study
title: "Study — work catalogue: views and proposals on the OKF canon"
description: "Forgejo, Plane, OpenProject, wikis and git CMSs evaluated as derived views or proposal interfaces of the OKF canon; existing OKF tooling."
status: draft
tags: [okf, catalogue, forgejo, open-source]
generated: { by: "mesh-design-etude/claude-opus-5-5", at: "2026-10-03T23:45:00+02:00" }
stale_after: 2027-04-03
sources:
  - { resource: "https://codeberg.org/forgejo/forgejo", title: "Forgejo (GPL-3.0)" }
  - { resource: "https://forgejo.org/docs/latest/user/token-scope/", title: "Forgejo token scopes" }
  - { resource: "https://github.com/makeplane/plane", title: "Plane CE" }
  - { resource: "https://github.com/requarks/wiki/blob/v2.5.315/server/helpers/page.js", title: "Wiki.js rewrites the frontmatter" }
  - { resource: "https://github.com/sveltia/sveltia-cms", title: "Sveltia CMS" }
  - { resource: "https://github.com/GoogleCloudPlatform/open-knowledge-format", title: "OKF (reference)" }
  - { resource: "https://github.com/manaty/okf-federation", title: "OKF Federation" }
---

# Framework

The canon (teams, projects, lots, decisions) is in OKF in git
([decision 0005](../decisions/0005-canon-okf.md)). Tools are therefore only
**derived views** or **proposal interfaces** (branch, PR). Criteria:
synchronisation with git, tracking state without copying the description,
OKF tooling.

# Results (licences read from the LICENSE files)

| Tool | Licence | Link with git/OKF | Verdict |
|---|---|---|---|
| **Forgejo** | GPL-3.0 | native: repository = canon, PR = proposal, wiki = repository; tokens scoped per repository | **pillar** |
| Gitea | MIT | same | alternative |
| Plane CE | AGPL-3.0 | none; types, custom fields, workflows in the commercial edition | possible business view, never a source |
| OpenProject | GPL-3.0 | PR link only | too heavy for a view |
| Huly, Focalboard, Taiga | — | — | discontinued or frozen |
| Outline | BSL 1.1 | — | **not open source** |
| AFFiNE (backend), AppFlowy (server) | non-free licences | — | to rule out |
| Docmost, BookStack | AGPL / MIT | Markdown export | documentation views |
| Wiki.js 2 | AGPL-3.0 | git sync, but **rewrites the frontmatter** (7 fixed keys) | **destroys OKF keys** |
| Decap CMS, Sveltia CMS | MIT | forms that edit Markdown+YAML in git (Forgejo/Gitea) | business proposal interface |
| Backlog.md | MIT | Markdown tasks in git, kanban, MCP | model to imitate |

# Recommendation

1. **Canon and proposals: Forgejo** (or GitHub). Protected
   PR = the federation's `review_policy`; CI = `validate_bundle.py`; one
   account per agent, token scoped to its repositories (read/propose, never approve).
2. **Execution state in ameesh**, projected onto **one issue per lot** (labels
   `état/en-cours`, `état/bloqué`, `état/attente-humain`, project board).
   The issue points to the lot's file, without copying its description. Only
   durable steps (validated, delivered) enter `log.md` via PR. The OKF field
   `status` describes the knowledge lifecycle, not the progress of a lot.
3. **Business teams**: Sveltia/Decap form to propose, generated read-only
   view (OKF viewer, MkDocs, Quartz). Nexlink plays this role natively.

# Existing OKF tooling

Specification and reference agent: GoogleCloudPlatform/open-knowledge-format
(Apache-2.0; the `knowledge-catalog/okf` copy is frozen — the okf-federation
README still points to it). Libraries: `okf` (Rust), okf/okf-gem
(Ruby). Skills: okf-skills. OKF MCP servers: several individual projects,
Apache/ISC/MIT, not audited. Editor: OWOX Model Canvas. **None of them handles
lots, states or a kanban**: that is for ameesh or Nexlink to do.

# Not verified

Plane CE bot tokens; Decap editorial workflow with Forgejo; OpenProject
Community/Enterprise split; Vikunja scopes; Tenzu licence.
