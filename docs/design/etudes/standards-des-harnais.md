---
type: Study
title: "Étude — standards des outils d'agents (MCP, skills, plugins, ACP)"
description: "État des standards en octobre 2026, support par harnais, communication entrante, connecteurs existants, porte d'approbation ameesh-gate."
status: draft
tags: [mcp, acp, skills, plugins, gouvernance]
generated: { by: "mesh-design-etude/claude-opus-5-5", at: "2026-10-04T00:10:00+02:00" }
stale_after: 2027-01-04
sources:
  - { resource: "https://modelcontextprotocol.io/specification/2026-07-28/changelog", title: "MCP 2026-07-28" }
  - { resource: "https://aaif.io/projects/", title: "Agentic AI Foundation" }
  - { resource: "https://agentskills.io/specification", title: "Agent Skills" }
  - { resource: "https://agentclientprotocol.com/overview/agents", title: "ACP" }
  - { resource: "https://code.claude.com/docs/en/channels", title: "Claude Code channels" }
  - { resource: "https://github.com/deepseek-ai/deepseek-harness", title: "DeepSeek Harness" }
  - { resource: "https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps", title: "Slack : limites hors Marketplace" }
  - { resource: "https://github.com/agentgateway/agentgateway", title: "agentgateway" }
  - { resource: "https://www.ietf.org/ietf-ftp/internet-drafts/draft-farley-acta-signed-receipts-01.xml", title: "Brouillon IETF de reçus signés" }
---

# Conclusion

Tout le **sortant** passe par **MCP**, que tous les harnais parlent. Aucun
standard ne couvre l'**entrant** de façon portable. ameesh possède donc le
**réveil des agents** et la **porte d'approbation**, exprimés avec les standards
existants, sans nouveau format de connecteur.

# Standards (octobre 2026)

- **MCP 2026-07-28** (AAIF / Linux Foundation) : révision très cassante —
  sans état, plus de requêtes serveur → client (motif MRTR), `subscriptions/listen`,
  extension **Tasks**, OAuth par CIMD. Il faudra servir 2025-11-25 et 2026-07-28.
- **Agent Skills** (SKILL.md) : standard ouvert lancé par Anthropic.
- **ACP** : Zed et JetBrains. **A2A** v1.0 : AAIF. **AGENTS.md** : AAIF.
- **Plugins** : format propre à chaque harnais, contenu commun (skills + config
  MCP + hooks).

# Support par harnais

| Harnais | MCP | Skills | Hooks bloquants | Pilotage headless |
|---|---|---|---|---|
| Claude Code | local/distant, OAuth | oui | PreToolUse (allow/deny/ask/**defer**), PermissionRequest | `-p --resume`, stream-json |
| Codex CLI | local/distant, OAuth+CIMD | oui | PreToolUse (outils MCP compris) | `exec resume` ; **app-server** (`turn/start`, `turn/steer`) |
| DeepSeek Harness | stdio + HTTP (OAuth non documenté) | oui | réutilise les `hooks.json` de Claude/Codex | **ACP natif** ; SDK `--session-id` |
| Gemini CLI | oui | oui | BeforeTool | `-p` ; ACP natif |
| goose, OpenHands | oui | partiel | non vérifié | ACP natif |

# Décrire un harnais : standards existants

- **Registre ACP** (RFD terminée, registre stabilisé en mars 2026) : chaque agent
  publie un manifeste `agent.json` (id, nom, version, version du schéma,
  description, dépôt, licence, capacités = méthodes ACP, options
  d'authentification, distribution) dans un dépôt de registre agrégé en
  catalogue. C'est la base retenue pour les descripteurs de harnais d'ameesh
  (lot L16), étendue par des clés propres à ameesh pour ce que l'ACP ne décrit
  pas (pilotage sans interface et reprise de session hors ACP, jauges et coût,
  hooks, permissions).
- **Agent Card A2A** (`/.well-known/agent.json`, A2A v1.0) : décrit un agent
  **distant** (identité, compétences, points d'accès) — utile si un membre
  agent d'ameesh est exposé à d'autres systèmes, pas pour un harnais local.
- **Registre MCP** (`server.json`) : décrit des serveurs d'outils, pas des
  harnais ; utile pour le catalogue des connecteurs.

# Entrant

Les « channels » de Claude Code existent (preview, propres à Claude, liste
blanche, pas avec MCP 2026-07-28). Le **runner d'ameesh** reçoit l'événement
(Slack Events/Socket Mode, bot Teams, push Gmail, IMAP IDLE, Matrix, Nexlink),
l'inscrit dans le fil et **réveille l'agent** : Claude par `-p --resume`, Codex
par l'app-server, **les autres par ACP**. L'agent lit et répond via un serveur
MCP « **ameesh-fil** ».

# Connecteurs existants

| Système | Serveur MCP | Statut |
|---|---|---|
| Slack | officiel `mcp.slack.com/mcp` | GA février 2026 ; apps internes ou Marketplace |
| Teams, Outlook | Work IQ (Microsoft) | preview, licence M365 Copilot |
| Gmail, Google Chat | officiels Google | Developer Preview |
| GitHub | `github/github-mcp-server` (MIT) | stable |
| Forgejo | `goern/forgejo-mcp` | communautaire |
| IMAP/SMTP | aucun officiel | **à créer** |

# Gouvernance : ameesh-gate

Les passerelles MCP open source (agentgateway, IBM ContextForge, Docker MCP
Gateway, Microsoft MCP Gateway) filtrent et journalisent mais **aucune ne gère
une approbation humaine signée liée à l'action**. ameesh doit être ou embarquer
la porte : elle seule détient les identifiants métier, donc la règle « pas
d'action irréversible sans reçu signé » vaut quel que soit le harnais.

```
empreinte = SHA-256(JCS{serveur, outil, arguments, compte cible, lot,
                        version de politique, nonce, expiration})
```

L'empreinte sert de challenge à ameesh-approve ; la porte la recalcule sur le
`tools/call` réel, vérifie la signature et l'usage unique, exécute, journalise
dans le fil. En attente : erreur « en attente, réf. X » (ou `defer` chez Claude).
Le classement lecture/irréversible/coûteux vit dans le **canon OKF**, pas dans
les annotations MCP (`destructiveHint` n'est qu'un indice). Défense en
profondeur : hooks PreToolUse, sandbox réseau.

# Pièges

- **Slack** : apps hors Marketplace limitées à 1 requête/min et 15 objets sur
  `conversations.history` ; interdiction d'archiver les données d'autres
  organisations et d'entraîner des LLM → **une app interne par organisation** ;
  offre gratuite : 90 jours visibles. Le journal de référence n'est pas Slack.
- **Teams** : API protégées (validation 7 à 10 jours), notifications facturées.
- **Google Chat** : 60 écritures/min par espace, partagées entre apps.
- **`claude -p`** charge `.mcp.json` et les hooks du projet sans confirmation :
  lancer avec `--bare` et une configuration explicite.
- dsh est en developer preview.

# Ce qu'ameesh crée

1. adaptateurs de pilotage (stream-json Claude, app-server Codex, ACP) ;
2. adaptateurs entrants et serveur MCP ameesh-fil ;
3. ameesh-gate et la spécification d'empreinte et de reçu ;
4. générateur canon OKF → SKILL.md, AGENTS.md, politique d'outils, plugin par harnais ;
5. connecteurs manquants (IMAP/SMTP, transporteurs) en serveurs MCP standard.
