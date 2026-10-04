---
type: Study
title: "Étude — catalogue du travail : vues et propositions sur le canon OKF"
description: "Forgejo, Plane, OpenProject, wikis et CMS git évalués comme vues dérivées ou interfaces de proposition du canon OKF ; outillage OKF existant."
status: draft
tags: [okf, catalogue, forgejo, open-source]
generated: { by: "mesh-design-etude/claude-opus-5-5", at: "2026-10-03T23:45:00+02:00" }
stale_after: 2027-04-03
sources:
  - { resource: "https://codeberg.org/forgejo/forgejo", title: "Forgejo (GPL-3.0)" }
  - { resource: "https://forgejo.org/docs/latest/user/token-scope/", title: "Portées des jetons Forgejo" }
  - { resource: "https://github.com/makeplane/plane", title: "Plane CE" }
  - { resource: "https://github.com/requarks/wiki/blob/v2.5.315/server/helpers/page.js", title: "Wiki.js réécrit le frontmatter" }
  - { resource: "https://github.com/sveltia/sveltia-cms", title: "Sveltia CMS" }
  - { resource: "https://github.com/GoogleCloudPlatform/open-knowledge-format", title: "OKF (référence)" }
  - { resource: "https://github.com/manaty/okf-federation", title: "OKF Federation" }
---

# Cadre

Le canon (équipes, projets, lots, décisions) est en OKF dans git
([décision 0005](../decisions/0005-canon-okf.md)). Les outils ne sont donc que des
**vues dérivées** ou des **interfaces de proposition** (branche, PR). Critères :
synchronisation avec git, suivi de l'état sans recopier la description,
outillage OKF.

# Résultats (licences lues dans les fichiers LICENSE)

| Outil | Licence | Lien avec git/OKF | Verdict |
|---|---|---|---|
| **Forgejo** | GPL-3.0 | natif : dépôt = canon, PR = proposition, wiki = dépôt ; jetons limités par dépôt | **pilier** |
| Gitea | MIT | idem | alternative |
| Plane CE | AGPL-3.0 | aucun ; types, champs perso, workflows en édition commerciale | vue métier possible, jamais source |
| OpenProject | GPL-3.0 | lien PR seulement | trop lourd pour une vue |
| Huly, Focalboard, Taiga | — | — | à l'arrêt ou gelés |
| Outline | BSL 1.1 | — | **pas open source** |
| AFFiNE (backend), AppFlowy (serveur) | licences non libres | — | à écarter |
| Docmost, BookStack | AGPL / MIT | export Markdown | vues documentaires |
| Wiki.js 2 | AGPL-3.0 | synchro git, mais **réécrit le frontmatter** (7 clés fixes) | **détruit les clés OKF** |
| Decap CMS, Sveltia CMS | MIT | formulaires qui éditent Markdown+YAML dans git (Forgejo/Gitea) | interface de proposition métier |
| Backlog.md | MIT | tâches Markdown dans git, kanban, MCP | modèle à imiter |

# Recommandation

1. **Canon et propositions : Forgejo** (ou GitHub). PR
   protégée = `review_policy` de la fédération ; CI = `validate_bundle.py` ; un
   compte par agent, jeton limité à ses dépôts (read/propose, jamais approve).
2. **État d'exécution dans ameesh**, projeté sur **une issue par lot** (labels
   `état/en-cours`, `état/bloqué`, `état/attente-humain`, tableau de projet).
   L'issue pointe vers le fichier du lot, sans recopier sa description. Seules les
   étapes durables (validé, livré) entrent au `log.md` par PR. Le champ OKF
   `status` décrit le cycle du savoir, pas l'avancement d'un lot.
3. **Équipes métier** : formulaire Sveltia/Decap pour proposer, vue générée en
   lecture seule (visualiseur OKF, MkDocs, Quartz). Nexlink joue ce rôle en natif.

# Outillage OKF existant

Spécification et agent de référence : GoogleCloudPlatform/open-knowledge-format
(Apache-2.0 ; la copie `knowledge-catalog/okf` est gelée — le README
d'okf-federation pointe encore vers elle). Bibliothèques : `okf` (Rust), okf/okf-gem
(Ruby). Skills : okf-skills. Serveurs MCP OKF : plusieurs projets individuels
Apache/ISC/MIT, non audités. Éditeur : OWOX Model Canvas. **Aucun ne gère des
lots, des états ou un kanban** : c'est à ameesh ou Nexlink de le faire.

# Non vérifié

Jetons bot de Plane CE ; workflow éditorial Decap avec Forgejo ; partage
Community/Enterprise d'OpenProject ; portées Vikunja ; licence de Tenzu.
