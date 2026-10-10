---
type: Study
title: "Étude — isoler un compte et ses sessions, par harnais (comptes multiples)"
description: "Comment Claude Code, Codex et DeepSeek Harness rangent identifiants et sessions, et si une session peut être reprise sous un autre compte ; base du lot L30."
status: stable
tags: [comptes, forfaits, sessions, budgets]
generated: { by: "codex3-l30/claude-opus-5-5", at: "2026-10-05T10:30:00+02:00" }
stale_after: 2027-01-05
sources:
  - { resource: "decisions/0027-bascule-automatique-entre-comptes.md", title: "Décision 0027" }
  - { resource: "decisions/0019-budgets-et-routage.md", title: "Décision 0019 (garde de rythme)" }
  - { resource: "decisions/0025-une-session-par-lot.md", title: "Décision 0025 (sessions, résumé de reprise)" }
---

# Question

Pour basculer un harnais d'un compte à un autre **entre deux tours**
([0027](../decisions/0027-bascule-automatique-entre-comptes.md)), il faut
savoir, pour chaque harnais :

1. comment un compte est sélectionné (variable d'environnement, dossier) ;
2. où vivent les identifiants et où vivent les sessions ;
3. si une session ouverte sous le compte A peut être reprise sous le compte B.

# Méthode

Sans aucun appel payant ni connexion à un compte :

* `--help` des trois harnais et documentation installée (paquets de
  DeepSeek Harness) ;
* lecture des chaînes du binaire (Claude Code, Codex) pour les noms de
  fichiers et de variables ;
* **inventaire** des dossiers de configuration existants d'un poste de
  travail : noms et droits des fichiers seulement, jamais leur contenu
  (aucun fichier d'identifiants ouvert) ;
* une expérience **hors ligne** avec Claude Code : `HOME` et
  `CLAUDE_CONFIG_DIR` pointant vers des dossiers vides du bac à sable, aucune
  variable de clé, une session factice (deux lignes JSON inventées) — le
  harnais s'arrête à « Not logged in » avant tout appel réseau facturable.

Versions observées : Claude Code 2.1.x, Codex CLI 0.159.x,
DeepSeek Harness (`@deepseek-ai/dsh`) 0.1.5.

# Claude Code

**Sélection du compte : `CLAUDE_CONFIG_DIR`.** Sans elle, le dossier de
configuration est `~/.claude` et le fichier global `~/.claude.json` est à la
racine du dossier personnel ; avec elle, **tout** est sous le dossier
désigné, y compris `.claude.json`. Le chemin doit être absolu.

| Élément | Emplacement |
|---|---|
| identifiants (Linux) | `<dossier>/.credentials.json`, 0600 (macOS : trousseau, clé dérivée du dossier) |
| configuration globale | `<dossier>/.claude.json` (ou `~/.claude.json` par défaut) |
| réglages, hooks | `<dossier>/settings.json` |
| sessions | `<dossier>/projects/<chemin du dossier de travail encodé>/<session>.jsonl` |
| état divers | `sessions/`, `file-history/`, `todos/`, `shell-snapshots/`, `history.jsonl` |

**Expérience de reprise** (hors ligne, voir Méthode) :

| Cas | Résultat |
|---|---|
| `--resume S` sous un dossier B vide | « No conversation found with session ID » |
| session S dans `A/projects`, lancée sous B | « No conversation found » : les sessions sont par dossier |
| idem, `B/projects` → lien symbolique vers `A/projects` | session **trouvée** (`system/init` émis avec l'id S), puis « Not logged in » (B n'a pas d'identifiants) |
| sous A | session trouvée |

**Conclusion.** Le compte et les sessions sont séparables : les identifiants
sont un fichier à part, la session un fichier de `projects/`. Une session est
**reprenable sous un autre compte** si les deux dossiers de configuration
**partagent `projects/`** (lien symbolique). Le prompt de cache du fournisseur
est perdu au changement de compte (le premier tour relit tout le contexte),
mais la conversation continue.

À savoir pour un dossier secondaire : les hooks (`settings.json`) et la
configuration (`.claude.json`) sont propres au dossier ; un dossier neuf n'a
pas les hooks agent-mail. Les relier (lien symbolique vers le `settings.json`
du primaire) ou les copier fait partie de la préparation.

**Jauges.** Le forfait se lit dans le flux `stream-json` du tour
(`rate_limit_event`), pas dans un fichier du dossier : ameesh les attribue au
compte par un **marqueur** écrit dans le flux de l'agent avant chaque tour.

# Codex

**Sélection du compte : `CODEX_HOME`** (défaut `~/.codex`). La base d'état
SQLite peut être déplacée à part (`CODEX_SQLITE_HOME`, ou `sqlite_home` dans
la configuration).

| Élément | Emplacement |
|---|---|
| identifiants | `<CODEX_HOME>/auth.json`, 0600 (ou le trousseau si `cli_auth_credentials_store` le demande) |
| configuration | `<CODEX_HOME>/config.toml`, `hooks.json` |
| sessions | `<CODEX_HOME>/sessions/AAAA/MM/JJ/rollout-…-<id>.jsonl` + index (`session_index.jsonl`) + bases SQLite (`state_*.sqlite`, `thread_history_*.sqlite`) |
| jauges | `rate_limits` (primaire, secondaire) des événements `token_count` des journaux de session |

L'en-tête de chaque session (`session_meta`) porte **l'identifiant du compte
créateur** (`creator_account_id`, `creator_user_id`), repris dans la table
des fils de la base d'état. Une reprise par `codex exec resume <id>` cherche
le fil dans l'index, la base et les journaux du `CODEX_HOME` courant.

**Conclusion.** Partager les sessions entre deux `CODEX_HOME` demanderait de
partager aussi la base d'état SQLite entre deux processus de comptes
différents (risque de corruption, comportement non documenté), pour une
session explicitement rattachée à son compte créateur ; et les jauges, qui
vivent dans ces mêmes journaux, ne seraient plus attribuables à un compte.
Non vérifiable sans appel payant. **Retenu : pas de continuité ; rotation
avec résumé de reprise** (le résumé est produit sous l'ancien compte, qui
seul peut reprendre la session ; mécanisme L11).

# DeepSeek Harness

**Compte = clé d'API**, lue dans cet ordre : environnement du lancement
(`DEEPSEEK_API_KEY`), fichier d'identifiants du harnais
(`$DSH_HOME/.credentials.yaml`, 0600), `.env` du projet, `.env` du dossier du
harnais. La variable d'environnement gagne.

| Élément | Emplacement |
|---|---|
| dossier du harnais | `DSH_HOME` (défaut `~/.dsh`) |
| sessions | `$DSH_HOME/sessions` |
| profils | `$DSH_HOME/profiles/<nom>` |

**Conclusion.** Changer de clé par `DEEPSEEK_API_KEY` ne touche ni au dossier
ni aux sessions : **la même session est reprise** sous l'autre clé. (Les
fichiers téléversés au fournisseur sont indexés par clé : ils seraient
renvoyés, sans incidence sur la conversation.) Pas de jauge de forfait : le
seuil d'un compte payé au token est un **plafond** (dépense de l'heure,
attribuée au compte) ou un **solde** (relevé par compte).

# Synthèse

| Harnais | Sélection | Identifiants | Sessions | Reprise sous un autre compte |
|---|---|---|---|---|
| Claude Code | `CLAUDE_CONFIG_DIR` | `.credentials.json` du dossier | `projects/` du dossier | **oui**, si `projects/` est partagé (lien) ; sinon rotation avec résumé |
| Codex | `CODEX_HOME` | `auth.json` du dossier | `sessions/` + base SQLite du dossier, rattachées au compte | **non** : rotation avec résumé |
| DeepSeek Harness | `DEEPSEEK_API_KEY` | clé d'API | `$DSH_HOME/sessions`, indépendant de la clé | **oui** (même `DSH_HOME`) |

# Conséquences pour ameesh (L30)

* Profils de compte **par hôte** (configuration de l'hôte, clé `accounts`) :
  `config_dir` (dossier, 0700, identifiants présents — présence seulement) ou
  `api_key_env` (variable ou fichier de clé 0600).
* La décision de continuité est mécanique : même stockage de sessions (chemin
  résolu) → reprise ; sinon rotation avec résumé, et le fil le dit.
* Les gestes de connexion restent humains : `CLAUDE_CONFIG_DIR=<dossier> claude`
  puis `/login` ; `CODEX_HOME=<dossier> codex login`. Voir
  [la page d'exploitation](../../COMPTES.md).
* Le respect des conditions d'utilisation de chaque fournisseur pour l'usage
  de plusieurs comptes reste de la responsabilité de l'équipe (0027).

# Amendement (0034, L74)

La bascule au seuil et le retour au primaire (0027 §2–3) sont remplacés par
un choix par échéance : les comptes forment un réservoir, une session garde
son compte tant qu'il est sous son seuil, et une nouvelle session (ou une
rotation) va au compte dont la capacité inutilisée expire le plus tôt —
amendé le 2026-10-10 (L117) : au compte le plus en retard sur son rythme,
l'échéance ne servant plus qu'à départager. Le
tableau de continuité ci-dessus reste la règle de reprise ou de résumé quand
une session change de compte. Voir [la page d'exploitation](../../COMPTES.md).
