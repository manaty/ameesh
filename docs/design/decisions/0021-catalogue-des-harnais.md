---
type: Decision
title: "Un service centralisé évalue les harnais et les met à disposition, au lieu de les coder en dur"
description: "Les harnais (Claude Code, Codex, DeepSeek Harness, et les suivants) deviennent des entrées d'un catalogue central : descripteurs déclaratifs, adaptateurs génériques, banc de conformité, évaluation harnais × modèle × effort."
status: stable
tags: [harnais, catalogue, evaluation, adaptateurs, acp, mcp]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T02:55:00+02:00" }
sources:
  - { resource: "../etudes/standards-des-harnais.md", title: "Étude des standards des harnais" }
---

# Contexte

L'exécuteur connaît aujourd'hui trois harnais codés en dur (`claude`, `codex`,
`deepseek`), avec trois lignes de commande et trois formats de flux. De
nouveaux harnais apparaissent (Gemini CLI, goose, OpenHands, SDK d'agents…),
et chacun évolue (versions, options, protocoles).

# Décision

Un **service centralisé** **évalue les différents types de harnais** et **les
met à disposition** d'ameesh, **au lieu de les coder en dur**. Il complète le
catalogue des modèles ([0020](0020-catalogue-et-evaluation-des-modeles.md)).

# Conséquences

- **Descripteur de harnais** (concept `Harness` du canon ou du catalogue
  central) : comment le lancer sans interface, reprendre une session, lire son
  flux, injecter le courrier (hooks), quels protocoles il parle (MCP et version,
  ACP, app-server), modèle de permissions, source de ses jauges et de son coût,
  modèles et efforts acceptés, méthode d'installation et version épinglée.
- **Adaptateurs génériques** dans l'exécuteur : un adaptateur **ACP** (tous les
  harnais qui le parlent), un adaptateur **piloté par descripteur** pour les
  harnais en ligne de commande, et des adaptateurs spécifiques seulement quand
  c'est indispensable (Claude stream-json, Codex app-server) ; plus aucune
  liste fermée de harnais dans le code ou le schéma.
- **Banc de conformité** : une suite qui vérifie qu'un harnais sait faire un
  tour, reprendre la session, recevoir le courrier, respecter la politique
  d'outils et de permissions, rapporter son coût — d'où un niveau de
  conformité publié dans le catalogue.
- **Évaluation** harnais × modèle × effort sur les tâches de référence de 0020,
  avec les mêmes garde-fous (action coûteuse sous la porte, forfaits).
- **Service centralisé = point sensible de chaîne d'approvisionnement** : un
  hôte n'installe un harnais que depuis une entrée **signée**, à **version
  épinglée** et **empreinte vérifiée**, admise par la politique de l'hôte
  ([0014](0014-orchestrateurs-a-tours-et-placement.md)) ; l'installation est une
  action soumise à la porte.
- Exigence R22 ; lots L16 (descripteurs et adaptateurs génériques, dans
  l'exécuteur) et L17 (catalogue central, banc de conformité, évaluation des
  harnais).
