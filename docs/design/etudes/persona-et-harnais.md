---
type: Study
title: "Étude — la persona à travers les harnais : identité, consignes et mémoire d'une session à l'autre"
description: "Ce que Claude Code, Codex, DeepSeek Harness et un agent ACP offrent pour désigner une persona et lui garder sa mémoire entre sessions, au relais et en parallèle ; spécification de la fiche Persona, des sessions, de la désignation au harnais et d'une mémoire par persona dans son propre dépôt git, neutre, remplaçable et peut-être durable ; règle de visibilité vérifiée au placement."
status: draft
tags: [persona, session, memoire, harnais, visibilite, double-numerique, mcp, acp]
generated: { by: "etude-persona/claude-opus-5-5", at: "2026-10-05T21:00:00+02:00" }
stale_after: 2027-01-05
sources:
  - { resource: "../decisions/0029-persona-et-session.md", title: "Décision 0029 (persona et session)" }
  - { resource: "../decisions/0025-une-session-par-lot.md", title: "Décision 0025 (une session par lot)" }
  - { resource: "../decisions/0027-bascule-automatique-entre-comptes.md", title: "Décision 0027 (comptes, relais)" }
  - { resource: "../decisions/0028-ressources-des-hotes-et-repartition.md", title: "Décision 0028 (placement dynamique)" }
  - { resource: "../decisions/0007-standards-des-harnais.md", title: "Décision 0007 (MCP, skills, AGENTS.md)" }
  - { resource: "../decisions/0021-catalogue-des-harnais.md", title: "Décision 0021 (descripteurs de harnais)" }
  - { resource: "comptes-multiples.md", title: "Étude des comptes multiples" }
  - { resource: "standards-des-harnais.md", title: "Étude des standards des harnais" }
  - { resource: "https://code.claude.com/docs/en/memory", title: "Claude Code : CLAUDE.md et mémoire automatique" }
  - { resource: "https://code.claude.com/docs/en/cli-reference", title: "Claude Code : options, consignes système à la reprise" }
  - { resource: "https://code.claude.com/docs/en/hooks", title: "Claude Code : hooks (SessionStart, compaction)" }
  - { resource: "https://learn.chatgpt.com/docs/agent-configuration/agents-md", title: "Codex : AGENTS.md" }
  - { resource: "https://learn.chatgpt.com/docs/customization/memories.md", title: "Codex : mémoires" }
  - { resource: "https://developers.openai.com/codex/config-reference", title: "Codex : référence de configuration" }
  - { resource: "https://developers.openai.com/codex/hooks", title: "Codex : hooks" }
  - { resource: "https://agentclientprotocol.com/protocol/session-setup", title: "ACP : session/new, session/load" }
  - { resource: "https://agentclientprotocol.com/protocol/extensibility", title: "ACP : _meta et extensions" }
---

# Question

La décision [0029](../decisions/0029-persona-et-session.md) sépare la
**persona** (identité durable, sans position) de la **session** (exécution
éphémère dans un harnais). Elle confie la mémoire au futur harnais maison et
ne laisse à ameesh que la désignation de la persona. En attendant ce harnais —
et peut-être à sa place — il faut savoir :

1. comment une persona garde **sa** mémoire et son identité d'une session à
   l'autre avec les harnais actuels (Claude Code, Codex, DeepSeek Harness
   `dsh`, et tout agent ACP par le pont du lot L16) ;
2. ce qui survit au **relais** (changement de compte ou de harnais,
   [0027](../decisions/0027-bascule-automatique-entre-comptes.md)) et au
   **déplacement** d'hôte ([0028](../decisions/0028-ressources-des-hotes-et-repartition.md)) ;
3. ce qui se passe quand **plusieurs sessions de la même persona** tournent
   en parallèle (une par lot, [0025](../decisions/0025-une-session-par-lot.md)) ;
4. critère ajouté par le propriétaire : une solution intérimaire **neutre
   vis-à-vis du harnais** peut-elle être assez souple pour **durer**, au point
   de se passer d'un harnais maison ?

# Méthode

Comme pour l'[étude des comptes multiples](comptes-multiples.md), sans aucun
appel payant, sans connexion à un compte, sans lire ni afficher de secret :

* `--help` des harnais, `codex features list` ;
* chaînes des binaires (Claude Code, Codex) pour les **noms** de réglages et
  de variables ;
* documentation installée de DeepSeek Harness (README des paquets
  `@deepseek-ai/dsh-*`) et documentation en ligne (voir les sources) ;
* inventaire de la **structure** des dossiers de configuration d'un poste
  (`~/.claude`, `~/.codex`, `~/.dsh`) : noms de fichiers et de dossiers
  seulement, jamais le contenu des identifiants, des conversations ni des
  mémoires ;
* lecture du code d'ameesh (`canon.py`, `adapters.py`, `runner.py`) et de la
  branche du lot L16 (descripteurs, pont ACP), en relecture.

Versions observées : Claude Code 2.1.286, Codex CLI 0.159.3, DeepSeek Harness
0.1.5-rc.2.

**Niveaux de certitude** utilisés dans les tableaux :

* **vérifié** : constaté sur le poste (aide, chaîne du binaire, structure de
  dossier, README installé) ;
* **documenté** : écrit dans la documentation officielle, non éprouvé ici ;
* **supposé** : déduit, à vérifier dans un lot (banc de conformité L17).

Légende des colonnes : **compte** = l'emplacement change quand on change de
compte (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`) ; **dossier** = il dépend du
dossier de travail ; **L / É / I / R** = ameesh peut le **l**ire, l'**é**crire,
l'**i**njecter au lancement, le **r**écupérer après la session.

# Claude Code

## Mécanismes

| Mécanisme | Emplacement | Compte ? | Dossier ? | L / É / I / R | Certitude |
|---|---|---|---|---|---|
| CLAUDE.md utilisateur, `rules/` utilisateur | `<config>/CLAUDE.md`, `<config>/rules/` (`<config>` = `~/.claude` ou `CLAUDE_CONFIG_DIR`) | **oui** | non | oui / oui / oui / — | documenté ; lien au dossier de configuration supposé (tout y est rangé, voir l'étude des comptes) |
| CLAUDE.md de projet, `.claude/rules/` | `./CLAUDE.md`, `./.claude/CLAUDE.md`, ancêtres ; sous-dossiers à la demande | non | **oui** | oui / oui (mais versionné dans le dépôt) / — / — | documenté |
| CLAUDE.local.md | racine du projet, non versionné | non | **oui** | oui / oui / — / — | documenté ; **sa présence empêche la lecture d'AGENTS.md** par défaut |
| AGENTS.md | dossier de travail et ancêtres, si aucun CLAUDE.md | non | oui | oui / oui / — / — | documenté (≥ 2.1.277) |
| Imports `@chemin` | dans un CLAUDE.md, quatre niveaux | — | — | — | documenté ; un import hors du dossier demande une approbation interactive la première fois (**inutilisable sans humain**) |
| Dossier additionnel | `--add-dir D` + `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` charge `D/CLAUDE.md`, `D/.claude/rules/` | non | non | oui / oui / **oui** / — | documenté ; variable vérifiée dans le binaire |
| Consigne système ajoutée | `--append-system-prompt[-file]` | non | non | — / — / **oui** / — | vérifié (`--help`) ; **figée au premier tour** et réutilisée à la reprise jusqu'à la compaction (`--system-prompt-snapshot`, documenté) |
| Consigne système remplacée | `--system-prompt[-file]` | non | non | — / — / oui / — | documenté ; perd les consignes d'outils et de sécurité par défaut : déconseillé |
| Mémoire automatique | `<config>/projects/<projet>/memory/MEMORY.md` + un fichier par souvenir | **oui** | **oui** (clé = dépôt git, partagée entre worktrees) | oui / oui / — / oui | documenté ; structure vérifiée (`memory/MEMORY.md` présent dans plusieurs projets) |
| Emplacement de la mémoire automatique | réglage `autoMemoryDirectory`, accepté **depuis `--settings`** | non | non | — / — / **oui** / — | documenté ; nom vérifié dans le binaire |
| Désactivation | `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `autoMemoryEnabled: false`, `--bare` | — | — | — / — / oui / — | documenté ; variable vérifiée |
| Nom du dossier de projet | `CLAUDE_CODE_PROJECT_DIR_NAME` (avec `CLAUDE_CONFIG_DIR`) | oui | non | — / — / oui / — | documenté (≥ 2.1.234) ; vérifié dans le binaire |
| Mémoire d'un sous-agent | champ `memory: user/project/local` → `agent-memory/<nom>/` | user : oui ; project/local : oui (dossier) | selon portée | oui / oui / — / oui | documenté ; chaînes vérifiées |
| Hook `SessionStart` | réglages (`<config>/settings.json`, projet, ou `--settings`) | selon le fichier | selon le fichier | — / — / **oui** (`additionalContext`, 10 000 caractères, au-delà : fichier + aperçu) / — | documenté ; déclencheurs `startup`, `resume`, `clear`, `compact`, `fork` |
| Hooks `PreCompact` / `PostCompact`, `Stop`, `SessionEnd` | idem | — | — | — / — / oui (`Stop` peut relancer avec contexte) / — | documenté ; noms vérifiés |
| Skills | `<config>/skills/`, `.claude/skills/`, `--plugin-dir` | selon | selon | oui / oui / oui / — | documenté ; `--plugin-dir` vérifié |
| Serveurs MCP | `--mcp-config` (+ `--strict-mcp-config`) | non | non | — / — / **oui** / — | vérifié (`--help`) ; les `instructions` d'un serveur MCP sont montrées au modèle (constaté dans une session de ce poste) |
| Transcription de session | `<config>/projects/<projet>/<session>.jsonl` | oui | oui | oui / — / — / oui | vérifié (étude des comptes) |

## Conclusions pour Claude Code

* **La mémoire native est rangée par compte et par dépôt, pas par persona.**
  Deux personas qui travaillent dans le même dépôt partagent une mémoire ; une
  persona qui change de dépôt en retrouve une autre ; un changement de compte
  (autre `CLAUDE_CONFIG_DIR`) la perd, sauf si `projects/` est partagé par
  lien, comme le fait déjà L30 pour reprendre les sessions — ce lien partage
  alors aussi la mémoire, pour tout le monde.
* **ameesh peut la rediriger** sans toucher aux fichiers du compte :
  `--settings '{"autoMemoryDirectory": "<dossier de la persona>"}'`. Il peut
  aussi la couper (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`).
* **Désignation sans écrire dans le dépôt** : `--append-system-prompt-file`
  (identité, rôle, règles), `--add-dir` sur un dossier de persona contenant un
  `CLAUDE.md` généré (avec la variable ci-dessus), hook `SessionStart` passé
  par `--settings` pour injecter l'index de mémoire, y compris **après une
  compaction** (`matcher: compact`).
* La consigne système est **figée à la création de la session** : c'est
  compatible avec une session par lot ; une persona modifiée au canon ne
  touche que les sessions suivantes.
* `--bare` coupe d'un coup CLAUDE.md, hooks, skills et mémoire : utile pour
  un harnais entièrement piloté, mais il coupe aussi les hooks agent-mail.

# Codex

| Mécanisme | Emplacement | Compte ? | Dossier ? | L / É / I / R | Certitude |
|---|---|---|---|---|---|
| AGENTS.md global | `$CODEX_HOME/AGENTS.override.md`, puis `$CODEX_HOME/AGENTS.md` | **oui** | non | oui / oui / oui / — | documenté ; `AGENTS.override.md` vérifié dans le binaire |
| AGENTS.md de projet | de la racine du dépôt au dossier courant (`AGENTS.override.md`, `AGENTS.md`, noms de repli) | non | **oui** | oui / oui (versionné) / — / — | documenté ; limite `project_doc_max_bytes` (32 Kio) |
| Instructions de développeur | clé `developer_instructions` (injectée avant AGENTS.md), passable par `-c` | non | non | — / — / **oui** / — | documenté ; clé vérifiée dans le binaire ; `-c` vérifié (`--help`) ; **effet à la reprise (`exec resume`) supposé** |
| Instructions de base remplacées | `model_instructions_file` | non | non | — / — / oui / — | documenté ; déconseillé (perd les consignes du harnais) |
| Profils | `-p <nom>` → `$CODEX_HOME/<nom>.config.toml` | **oui** | non | oui / oui / oui / — | vérifié (`codex exec --help`) |
| Mémoires | `$CODEX_HOME/memories/` (`memory_summary.md`, `MEMORY.md`, `raw_memories.md`, `rollout_summaries/`) ; réglages `[memories]` : `generate_memories`, `use_memories`, `disable_on_external_context`, `min_rate_limit_remaining_percent` | **oui** | non (globale au `CODEX_HOME`) | oui / risqué / — / oui | fonction `memories` **stable mais désactivée** sur ce poste (vérifié) ; noms de fichiers vérifiés dans le binaire ; une base `memories_1.sqlite` existe (vérifié) ; génération **en arrière-plan, après inactivité**, par le harnais (documenté) ; indisponible dans certaines régions au lancement (documenté) |
| Hooks | `$CODEX_HOME/hooks.json`, `config.toml` `[hooks]`, `<dépôt>/.codex/hooks.json` | oui (global) / oui (projet) | — | — / — / **oui** (`SessionStart` → `additionalContext`, message de développeur) / — | documenté ; `SessionStart`, `PreCompact`, `PostCompact` vérifiés dans le binaire ; ameesh lance déjà avec `--dangerously-bypass-hook-trust` |
| `notify` | commande appelée en fin de tour | oui | — | — / — / — / oui | nom vérifié ; détail supposé |
| Serveurs MCP | `config.toml` `[mcp_servers]`, passables par `-c` | oui (fichier) / non (`-c`) | — | — / — / oui / — | documenté ; `codex mcp` vérifié |
| Skills | `$CODEX_HOME/skills/` | oui | — | oui / oui / oui / — | structure vérifiée |
| Sessions | `$CODEX_HOME/sessions/…`, base d'état SQLite, rattachées au compte créateur | oui | — | oui / — / — / oui | vérifié (étude des comptes) |

## Conclusions pour Codex

* **La mémoire native est globale au `CODEX_HOME`, donc au compte** — et L30
  fait du `CODEX_HOME` le compte. Une persona qui passe au compte secondaire
  perd sa mémoire native ; deux personas sur le même compte la partagent. Elle
  est produite en arrière-plan par le harnais, sur ses propres critères : ni
  sa date ni son contenu ne sont maîtrisés. **À laisser désactivée** pour les
  sessions lancées par ameesh.
* **Désignation** : `-c developer_instructions="…"` à chaque tour (texte
  généré depuis la fiche, borné), hook `SessionStart` pour l'index de mémoire,
  `-c mcp_servers.…` pour un serveur MCP. Ne rien écrire dans `CODEX_HOME`
  (dossier d'un compte, partagé entre personas).
* À vérifier : que `developer_instructions` passé par `-c` s'applique bien
  après `exec resume`, et la taille admise sur la ligne de commande (sinon un
  fichier de profil propre à la session).

# DeepSeek Harness (`dsh`)

| Mécanisme | Emplacement | Compte ? | Dossier ? | L / É / I / R | Certitude |
|---|---|---|---|---|---|
| Instructions d'espace de travail | `$DSH_HOME/AGENTS.md` puis la chaîne du projet (`AGENTS.md`, `CLAUDE.md`, et `*.local.md`), budget `maxBytes` (65 536 par défaut) | non (le compte est une clé) | oui (projet) | oui / oui / oui / — | vérifié (README `dsh-agent-instructions`) |
| Persona du déploiement | `dsh-system-prompt` : `personaPrefix`, `personaSuffix` (gabarits) | non | non | — / — / **oui** par `--patch` | vérifié (README) ; ameesh écrit déjà un `--patch` pour le modèle |
| Persona d'un agent | rangée `dsh-persona` (`prefix`, `suffix`, `complete`) dans un préréglage | non | non | — / — / oui | vérifié (README) ; à monter dans un préréglage, pas globalement |
| Profils, patches | `$DSH_HOME/profiles/<nom>/` (`cordis.patch.yml`), `$DSH_HOME/cordis.patch.yml`, `--patch` | non | non | oui / oui / oui / — | vérifié (README, structure) |
| Hooks | `$DSH_HOME/hooks.json` relu par les ponts `dsh-hooks-claude-code` / `dsh-hooks-codex` ; `SessionStart` ajoute du contexte ; commandes seulement | non | — | — / — / oui / — | vérifié (README, structure) |
| Mémoire native | **aucun** paquet de mémoire ; la documentation renvoie à des serveurs MCP de mémoire tiers | — | — | — | vérifié (liste des paquets) |
| Serveurs MCP | `dsh-mcp-client` : **outils seulement** (ni ressources ni prompts MCP) | non | — | — / — / oui / — | vérifié (README) |
| Sessions | `$DSH_HOME/sessions`, indépendantes de la clé | non | — | oui / — / — / oui | vérifié (étude des comptes) |

## Conclusions pour dsh

* Pas de mémoire native : rien à perdre au relais, tout à fournir.
* C'est le harnais le plus **ouvert** : la consigne système se compose par
  patch (préfixe et suffixe de persona, contexte d'exécution, ordre des
  outils), la compaction est un greffon configurable. ameesh peut y désigner
  la persona proprement (`personaSuffix` par patch de session) — **supposé**,
  à éprouver.
* Un serveur MCP de mémoire n'y est vu qu'à travers ses **outils** : ne pas
  compter sur les ressources ni les prompts MCP.

# ACP (pont ameesh, lot L16)

| Élément | Ce que le protocole transporte | Ce que le pont ameesh peut fournir | Certitude |
|---|---|---|---|
| `session/new` | `cwd`, `mcpServers` | dossier de la session ; **serveurs MCP** (ameesh-fil, mémoire) — le pont envoie aujourd'hui `mcpServers: []` | documenté ; code L16 lu |
| `session/load` / `session/resume` | `sessionId`, `cwd`, `mcpServers`, `additionalDirectories` (si annoncé) ; `load` **rejoue** l'historique | mêmes serveurs MCP **redéclarés** à chaque reprise | documenté |
| Consigne système, persona | **aucun champ standard** | seulement le **premier message** (`session/prompt`) et les fichiers d'instructions du `cwd` (AGENTS.md, si l'agent les lit) | documenté |
| `_meta` | extension libre (`traceparent` etc. réservés) | `_meta` portant l'identité de persona et de session — **informatif** : un agent peut l'ignorer (dsh déclare n'en lire aucun) | documenté ; README dsh vérifié |
| `fs/*` côté client | lecture/écriture de fichiers dans le `cwd` | bornées au dossier de la session par le pont | code L16 lu |

## Conclusions pour ACP

Le seul canal **portable** pour la persona est le contenu : le **brief** du
premier message (identité, consignes, index de mémoire) et les **serveurs
MCP** déclarés à `session/new` et à chaque reprise. Tout le reste (consigne
système, hooks, compaction) appartient à l'agent. La persona d'un agent ACP
est donc « au mieux » : énoncée, pas imposée.

# MCP, voie commune

Les quatre harnais parlent MCP ; un serveur **« mémoire de persona »** fourni
par ameesh est donc atteignable partout :

| Harnais | Déclaration par session sans toucher au compte | Instructions du serveur vues par le modèle | Outils | Certitude |
|---|---|---|---|---|
| Claude Code | `--mcp-config` | oui | oui | vérifié |
| Codex | `-c mcp_servers.<nom>…` | supposé | oui | documenté / supposé |
| dsh | `--patch` (`dsh-mcp-client`) ou ACP `mcpServers` | non (outils seulement) | oui | vérifié (README) |
| ACP | `mcpServers` de `session/new`/`resume`/`load` | selon l'agent | oui | documenté |

Limites : MCP **n'injecte rien au démarrage** — le modèle doit appeler un
outil pour lire ; la lecture initiale passe donc encore par la consigne ou un
hook. Chaque outil coûte des jetons à chaque requête. Et la révision
MCP 2026-07-28, sans état, oblige à servir deux versions un temps
([étude des standards](standards-des-harnais.md)).

# Synthèse par harnais

| | Claude Code | Codex | dsh | ACP |
|---|---|---|---|---|
| Mémoire native | par compte × dépôt, redirigeable | par compte, arrière-plan, non maîtrisée | aucune | celle de l'agent, opaque |
| Perdue au changement de compte | oui (sauf `projects/` partagé) | oui | — | selon l'agent |
| Perdue au changement de harnais | oui | oui | — | oui |
| Consigne système de persona | ajout par option, figée au 1er tour | `developer_instructions` par `-c` | patch (préfixe/suffixe) | aucune : premier message |
| Injection au démarrage | hook `SessionStart` (aussi après compaction) | hook `SessionStart` | hook `SessionStart` (pont) | premier message |
| MCP par session | oui | oui | oui (outils) | oui |
| Sessions parallèles | oui (une transcription par session) | oui (verrous d'écriture) | oui | selon l'agent |

**Constat principal** : aucune mémoire native n'est rangée **par persona** ;
toutes sont attachées au compte, au dépôt ou à l'agent, et aucune ne passe
d'un harnais à l'autre. Une persona qui doit survivre au relais ne peut donc
pas confier sa mémoire au harnais du moment.

# Spécification proposée

## 1. Fiche `Persona` au canon (remplace `Agent`)

```yaml
type: Persona
title: redacteur                 # nom (grammaire du registre), identité stable
responsible: human:alice         # REQUIS (R14)
team: acme-web
description: "Rédige et tient la documentation du produit."
capabilities: [read, report-drift, propose]   # `approve` interdit (R8)
tools: [git, mcp:ameesh-fil]
reviewers: [relecteur]
budget_usd_per_day: 20
session_policy: par-lot          # par-lot | taille | jamais (0025)
harnesses:                       # préférences ordonnées, pas une position
  - { harness: claude, model: opus, effort: high }
  - { harness: codex, model: "gpt-…", effort: high }
  - { harness: deepseek }
credential_modes: [subscription, api-key]
memory:
  mode: neutral                  # neutral | native | none (§4)
  repository: "git@forge.example:equipe/persona-redacteur.git"   # SEUL renseignement : l'emplacement
  path: ""                       # sous-dossier si le dépôt est partagé par un groupe de personas
instructions: []                 # autres fiches du canon à joindre (facultatif)
---
Corps Markdown : la voix et les règles propres de la persona.
```

* Le **corps** de la fiche devient la source des consignes de la persona
  (ameesh ne lisait jusqu'ici que le frontmatter) ; il est revu comme le reste
  du canon.
* Aucune position : ni hôte, ni dossier, ni compte, ni identifiant de
  session. Les hôtes admis relèvent de l'**admission** (ex-`Placement`, sans
  `cwd`), déjà prévue au lot L31.
* `harness`/`model`/`provider` au singulier deviennent la liste ordonnée
  `harnesses` : c'est elle que le relais parcourt.
* `memory.repository` ne donne que l'**emplacement** du dépôt de la persona
  (§4) ; ni droits, ni identifiants, ni contenu au canon. Ce même dépôt sert
  à la **règle de visibilité** (§1 bis).
* Pas de champ propre au double numérique : un double est une persona dont le
  dépôt est privé à son humain (§5).

**Migration.** Un temps, le chargeur lit les deux formes :

* `type: Agent` est lu comme `Persona` (`kind: agent`), avec un constat
  `persona-legacy-agent` (avertissement) ; `harness`/`model`/`provider`
  donnent une liste `harnesses` d'un élément ;
* `type: Placement` est lu comme admission ; un `cwd` présent est **ignoré**
  avec un constat `admission-cwd-ignored`, puis refusé à la fin de la
  période ;
* le registre (`agent_registry`, clé `name`) reste indexé par le nom de la
  persona ; l'état de session en sort (§2).

## 1 bis. Visibilité : la règle unique, vérifiée au placement

Règle de [0029](../decisions/0029-persona-et-session.md) : **une persona ne
tourne sur la machine d'un humain que si cet humain a accès au dépôt de la
persona** ; pour un serveur ou un hôte de cluster, la règle porte sur son
responsable (`responsible` de la fiche `Host`) et sur ses administrateurs.

Proposition de mise en œuvre, dans le prédicat de placement (L31) :

* **Qui doit avoir accès** : l'ensemble `H(hôte)` = responsable de l'hôte +
  administrateurs déclarés dans la fiche `Host` (nouvelle clé `admins`, liste
  de `human:<id>`).
* **Comment le vérifier**, au choix de la configuration de l'hôte :
  1. **par l'hébergeur git** : l'API de la forge dit, pour chaque humain de
     `H(hôte)` lié à son compte de forge (liaison `human:<id>` → compte de
     forge, à porter par la fiche `Member`), s'il a au moins la lecture sur le
     dépôt ; l'appel se fait avec un jeton d'ameesh limité à la lecture des
     droits ;
  2. **par une copie d'essai** faite avec les identifiants de l'humain
     responsable de l'hôte (`git ls-remote` suffit) : si elle échoue, l'accès
     n'est pas établi. Cette voie ne prouve rien pour les administrateurs ;
     elle convient à un poste personnel (un seul humain).
* **Résultat** : hôte admis, ou **écarté** avec un constat lisible
  (`persona-hidden-from-host`) ; jamais d'admission par défaut quand la
  vérification échoue (fail closed). Le résultat est mis en cache avec une
  durée courte et revérifié à chaque ouverture de session et à chaque
  déplacement (0028).
* **Révocation** : un accès retiré au dépôt écarte l'hôte pour les sessions
  suivantes ; la session en cours finit son tour puis est arrêtée, et sa
  copie locale est poussée puis effacée (§4).

## 2. Enregistrement d'une session

Une table de sessions (migration à numéroter le moment venu), une ligne par
exécution :

| Champ | Contenu |
|---|---|
| `id` | identifiant ameesh (UUID), distinct de celui du harnais |
| `persona` | nom de la persona |
| `work_item` | le lot (0025) ; `NULL` pour une session longue d'orchestrateur |
| `harness`, `harness_version` | descripteur L16 et version épinglée |
| `model`, `effort`, `tier` | réglages effectifs |
| `account` | **nom** du profil de compte (L30), jamais un secret |
| `host`, `runner_id`, `lease_epoch` | où et sous quel bail |
| `cwd` | dossier de travail (état d'exécution, 0029) |
| `harness_session_id` | identifiant natif (`--resume`, `exec resume`, `--session-id`, ACP) |
| `parent_session` | session précédente de la chaîne (rotation L11, relais, déplacement) |
| `relay_reason` | `rotation`, `compte`, `harnais`, `hôte`, `restart` |
| `started_at`, `ended_at`, `status`, `end_reason` | cycle de vie (fusion du lot, abandon…) |
| `memory_journal` | chemin du journal de mémoire de la session, dans la copie éphémère (§4) |
| `memory_commit` | commit du dépôt de la persona à l'ouverture, puis commit poussé à la fermeture |

* **Sessions parallèles** : au plus une session **active** par
  `(persona, work_item)` ; plusieurs lots, plusieurs sessions. Le bail passe
  de la persona à la session. L'identité posée dans l'environnement du
  harnais reste `AGENT_MAIL_NAME=<persona>` (les outils existants continuent
  de marcher), complétée de `AMEESH_SESSION=<id>`.
* **Courrier** : un message adressé à la persona et porteur d'un lot va à la
  session de ce lot ; sans lot, à la session longue si elle existe, sinon il
  attend (décision d'acheminement à préciser dans le lot).
* Le relais et le déplacement **ferment** une session et en ouvrent une
  autre liée par `parent_session` ; la reprise native n'est qu'un cas où
  `harness_session_id` est conservé.

## 3. Désignation de la persona au harnais

ameesh **génère** depuis la fiche un texte de persona (identité, rôle,
responsable, règles, interdits, outils, lot) et un **index de mémoire**
(§4), puis les transmet par le canal le plus fort de chaque harnais, **sans
jamais écrire dans le dépôt de travail ni dans le dossier d'un compte** :

| Harnais | Identité et consignes | Index de mémoire au démarrage | Après compaction | Outils |
|---|---|---|---|---|
| Claude Code | `--append-system-prompt-file <session>/persona.md` | hook `SessionStart` (`startup`, `resume`) passé par `--settings` | même hook, `matcher: compact` | `--mcp-config` |
| Codex | `-c developer_instructions=…` (ou profil propre à la session) | hook `SessionStart` | hook `PostCompact` ou rappel au tour suivant (à éprouver) | `-c mcp_servers…` |
| dsh | `--patch` de session : `personaSuffix` de `dsh-system-prompt` | hook `SessionStart` (pont de hooks) | supposé : greffon de compaction | `--patch` (`dsh-mcp-client`) |
| ACP | en tête du **premier message** ; `_meta` informatif | dans le premier message | rien de garanti : rappel par ameesh au tour suivant | `mcpServers` à chaque `new`/`resume`/`load` |

Le texte généré est le même pour tous les harnais ; seuls les canaux
diffèrent (champ du descripteur L16, par exemple `ameesh.persona`). Les
fichiers de session vivent dans le dossier d'état d'ameesh
(`~/.local/state/ameesh/sessions/<id>/`), pas dans le worktree.

## 4. Mémoire en attendant le harnais maison

### Trois voies comparées

| | (a) Mémoire native de chaque harnais | (b) Dossier de mémoire par persona, neutre | (c) Serveur MCP de mémoire |
|---|---|---|---|
| Portée | compte × dépôt (Claude), compte (Codex), rien (dsh), opaque (ACP) | **la persona**, où qu'elle tourne | la persona |
| Relais de compte ou de harnais | **perdue** (sauf Claude avec `projects/` partagé) | conservée | conservée |
| Sessions parallèles | écritures concurrentes non maîtrisées | règle explicite (journal par session) | règle explicite (côté serveur) |
| Lecture au démarrage | automatique | injectée par hook ou consigne | **non** : il faut un outil appelé, ou une injection en plus |
| Écriture | décidée par le modèle et le harnais | convention + tour de mémoire en fin de lot | outil appelé par le modèle |
| Lisible et relisible par un humain | oui (Markdown), mais éparpillée | oui (Markdown, git) | selon le stockage |
| Coût d'exploitation | nul | un dossier (git) par dépôt de mémoire | un service à faire tourner sur chaque hôte ou en réseau |
| Remplaçable par le harnais maison | non | **oui** : le format est le contrat | oui, si le stockage est (b) |

### Format neutre proposé (b)

```
<dépôt de la persona>/[<path>/]
├── MEMORY.md            # index : une ligne par souvenir, ≤ 200 lignes
├── souvenirs/<sujet>.md # un souvenir ou un sujet ; frontmatter : type, modified, source
└── journal/<session>.md # ajouts d'UNE session, en ajout seul
```

* Mêmes conventions que la mémoire automatique de Claude Code (index court,
  un fichier par sujet, `type` dans le frontmatter : `user`, `feedback`,
  `project`, `reference`) : la plus aboutie des mémoires observées, et
  lisible par les autres harnais comme simple Markdown. Le frontmatter suit
  OKF, ce qui laisse le canon et l'outillage existants le lire.
* **Un dépôt git par persona** (ou par groupe de personas qui peuvent se
  voir, chacune dans son sous-dossier `path`). La fiche n'indique que son
  emplacement (`memory.repository`).
* **Accès limité** : une session ne reçoit qu'un **identifiant d'accès limité
  à ce dépôt** (clé de déploiement ou jeton à portée d'un seul dépôt, en
  écriture), fourni par l'hôte au lancement et jamais au canon ; elle ne voit
  aucune autre mémoire.
* **Copie locale éphémère** : à l'ouverture, clonage dans le dossier d'état
  de la session (`~/.local/state/ameesh/sessions/<id>/memoire/`, droits 0700,
  privé pendant la session) ; à la fermeture (fin de lot, relais,
  déplacement, arrêt), commit, **push**, puis **effacement** de la copie. Un
  push refusé garde la copie et lève une alerte au lieu d'effacer. Ainsi la
  mémoire suit la persona d'un hôte à l'autre (0028) sans rester sur aucun.

### Règle d'écriture concurrente

1. **Une session n'écrit que son journal** `journal/<session>.md`, en ajout
   seul : deux sessions parallèles n'écrivent jamais le même fichier.
2. **Consolidation par un seul écrivain** : en fin de lot (ou au relais),
   ameesh lance un **tour de mémoire** dans la session — comme le tour de
   résumé de la rotation L11 — qui propose les changements de `souvenirs/` et
   de `MEMORY.md` ; l'écriture se fait par commit sur la copie puis push :
   le dépôt distant sert de verrou (un push refusé oblige à reprendre sur le
   dernier commit). Un conflit sur `souvenirs/` est un signal, jamais résolu
   en silence ; les journaux, un fichier par session, ne se heurtent pas.
3. Les journaux consolidés sont archivés (pas effacés) : on peut refaire une
   consolidation.
4. Aucun secret : la consolidation passe un filtre de secrets et refuse le
   commit en cas de doute.

Pour Claude Code, une variante à éprouver : `autoMemoryDirectory` pointé sur
un dossier **propre à la session**, pré-rempli d'une copie de l'index ; la
mémoire automatique écrit alors là, et la consolidation compare. Elle garde
le comportement natif d'écriture du modèle sans concurrence sur l'index.

### Ce que le relais doit transférer

| Élément | De Claude Code à Codex (exemple) |
|---|---|
| Identité et consignes | régénérées depuis la fiche, canal du nouveau harnais |
| Mémoire de la persona | poussée par l'ancienne session, reclonée par la nouvelle (même dépôt), index réinjecté |
| Journal de la session en cours | fichier déjà neutre ; repris dans la nouvelle session |
| Résumé de reprise du lot (L11) | produit sous l'ancien harnais/compte, placé dans le brief |
| Transcription native | **non transférable** entre harnais ; conservée pour l'audit |
| Mémoire native du harnais quitté | ignorée (désactivée en mode `neutral`) |

### Recommandation pour l'intérim

* **(b) comme stockage et contrat**, mémoire native **désactivée** pour les
  sessions lancées par ameesh en mode `neutral` (Claude :
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` ou redirection ; Codex : `memories`
  laissée désactivée), pour n'avoir qu'une mémoire par persona et ne pas en
  laisser une seconde diverger dans un compte.
* **(c) plus tard, comme façade** sur les mêmes fichiers (outils
  `memoire.lire`, `memoire.chercher`, `memoire.noter`) quand un harnais n'a
  pas de hook ; il travaille sur la copie éphémère de la session, avec le
  même identifiant limité.
* **ameesh ne devient pas propriétaire de la mémoire** : ni table, ni format
  propre en base. La gestion du dossier (injection, journal, consolidation)
  vit dans un **composant séparé** (« mémoire de persona »), appelé par le
  runner aux trois points fixes (ouverture, relais, fin de lot) ; le cœur
  d'ameesh ne fait que **désigner** la persona et l'emplacement de son
  dépôt, comme le veut [0029](../decisions/0029-persona-et-session.md). Le
  harnais maison, s'il vient, lit le même format et remplace ce composant.

## 4 bis. La solution intérimaire peut-elle durer ?

### Ce qu'elle couvre durablement

* L'**identité** d'une persona dans n'importe quel harnais (texte généré
  depuis le canon, canal le plus fort de chacun).
* Une **mémoire par persona**, indépendante du compte, du dépôt, du harnais et
  de l'hôte, relisible et versionnée.
* Le **relais** et le **déplacement** : la persona survit, seule la
  transcription native se perd, compensée par le résumé de reprise.
* Les **sessions parallèles** par la règle « un journal par session, un seul
  consolidateur ».
* Les **abonnements** utilisables seulement par les outils de leur
  fournisseur : rien n'oblige à quitter Claude Code ou Codex.

### Ce qui manquerait par rapport à un harnais maison

| Besoin | Claude Code | Codex | dsh | ACP |
|---|---|---|---|---|
| Contrôle de la consigne système | ajout seulement (remplacer coûte les consignes d'outils) ; figée au 1er tour | ajout (`developer_instructions`) ou remplacement complet | **complet** (patch) | aucun |
| Compaction, contexte | décidée par le harnais ; fenêtre réglable (`--autocompact`) ; réinjection par hook `compact` | décidée par le harnais ; hooks de compaction (à éprouver) | greffon configurable | opaque |
| Garantie d'écriture de la mémoire | aucune pendant la session ; garantie aux points fixes d'ameesh (tour de mémoire) | idem | idem | idem |
| Récupération en cours de session | hooks, lecture de fichiers, MCP : à l'initiative du modèle | idem | idem | MCP seulement |
| Observabilité | flux `stream-json`, hooks (`InstructionsLoaded`) | JSONL, journaux de session | JSON, télémétrie OTel | `session/update` |
| Sessions parallèles | oui | oui | oui | selon l'agent |
| Relais | par résumé (reprise native seulement entre comptes Claude à `projects/` partagé) | par résumé | même session (même `DSH_HOME`) | selon l'agent |

### Conditions pour que ce manque reste acceptable

1. **Sessions courtes** (une par lot, 0025) : la compaction devient rare, et
   avec elle le principal angle mort.
2. **Écritures de mémoire aux points fixes** d'ameesh (ouverture, relais, fin
   de lot), pas seulement à l'initiative du modèle.
3. **Banc de conformité** (L17) qui vérifie, par harnais et par version, que
   la persona est bien désignée, que l'index est bien injecté (y compris après
   compaction) et que le journal est bien écrit ; versions de harnais
   épinglées.
4. **Budgets d'injection** respectés : index ≤ 200 lignes, contexte de hook
   ≤ 10 000 caractères chez Claude, instructions ≤ 32 Kio chez Codex, 64 Kio
   chez dsh.
5. **Aucune dépendance aux internes non documentés** des harnais (variables
   d'expérimentation, bases SQLite, formats de transcription).
6. Accepter qu'un agent ACP ne reçoive la persona qu'**énoncée**.

**Conclusion.** Pour des agents de construction, de relecture et des doubles
numériques (des personas comme les autres), dont les besoins sont « une identité, des notes durables, une
session par lot », la solution intérimaire **peut durer** sans harnais
maison. Un harnais maison ne se justifierait que pour un contrôle fin du
contexte en cours de session (politique de rappel, compaction pilotée par la
persona, écriture transactionnelle de la mémoire à chaque tour), ou si les
harnais tiers fermaient les canaux utilisés ici. Recommandation : **construire
l'intérim comme une solution permanente mais remplaçable** — le format de
mémoire et le texte de persona sont le contrat ; le harnais maison devient un
harnais de plus qui lit ce contrat, non un prérequis.

## 5. Double numérique

Plus de règle particulière : **un double numérique est une persona dont le
dépôt est privé à son humain**. La règle de visibilité (§1 bis) le fait donc
tourner seulement sur les machines de cet humain (ou sur un hôte dont il est
le seul responsable et administrateur) ; sa mémoire, dans ce dépôt, n'est
vue que de lui. Il n'approuve jamais, comme toute persona
([0012](../decisions/0012-autorite-par-ameesh-approve.md)) : `approve` est
refusé au chargement et ameesh-gate n'accepte que la passkey de l'humain.
Restent à décider ses pouvoirs (0029). Un import de la mémoire native
existante de l'humain (par exemple sa mémoire automatique Claude Code) dans
le dépôt du double se fait seulement à sa demande, en copie.

## 6. Lots proposés

Numéros de lot et de migration à attribuer par l'orchestrateur ; aucun n'est
réservé ici.

| Lot | Contenu | Dépend de | Risques |
|---|---|---|---|
| P1 — fiche Persona et visibilité | type `Persona` (+ corps), `harnesses` ordonnés, `memory.repository`, lecture transitoire d'`Agent` et `Placement`, constats ; clé `admins` des fiches `Host` ; liaison humain → compte de forge ; **contrôle de visibilité dans le prédicat de placement** (forge ou copie d'essai, hôte écarté sinon, cache court, révocation) | L2 ; à fondre dans L31 (0029 l'y place) | double lecture prolongée ; L31 déjà chargé ; dépendance à l'API de la forge (indisponible = hôte écarté) |
| P2 — sessions multiples | table des sessions, bail par session, `AMEESH_SESSION`, acheminement du courrier par lot, `ameesh sessions` ; visibilité revérifiée à chaque ouverture de session | L26, L30, L31 | refonte du runner (un worker par nom aujourd'hui) ; le plus risqué |
| P3 — désignation au harnais | générateur « persona.md » depuis le canon ; champ de descripteur `ameesh.persona` (canaux par harnais) ; hooks `SessionStart` par `--settings`/config de session ; serveurs MCP par session dans le pont ACP | L16, P1 | effet de `-c` à la reprise (Codex) ; patch dsh non éprouvé |
| P4 — mémoire de persona (composant séparé) | format neutre, dépôt par persona, identifiant d'accès limité au dépôt fourni par l'hôte, copie éphémère (clone, push, effacement), journal par session, injection de l'index, tour de mémoire en fin de lot et au relais, consolidation sous verrou, filtre de secrets, désactivation des mémoires natives en mode `neutral` | P2, P3, L11 | conflits de push entre sessions parallèles ; copie non effacée après un push refusé ; qualité des consolidations |
| P5 — façade MCP de mémoire (facultatif) | serveur MCP sur les mêmes fichiers, deux versions de MCP | P4 | coût en jetons ; outils seulement chez dsh |
| P6 — conformité mémoire et persona | cas du banc L17 : désignation, injection après compaction, journal écrit, relais Claude → Codex | L17, P4 | appels payants sous la porte |
| P7 — pouvoirs du double numérique | seulement si la décision sur ses pouvoirs ajoute des règles ; sinon rien à faire (un double = une persona à dépôt privé) | P1, P4 ; décision du propriétaire | données personnelles |

Ordre : P1 avec L31, puis P3 (utile dès une seule session par persona), P2,
P4 ; P5 et P6 ensuite ; P7 seulement si une décision l'exige.

# Points à trancher par le propriétaire

Tranchés le 2026-10-05 : un dépôt git par persona (ou par groupe de personas
qui peuvent se voir), seul son emplacement au canon, accès limité à ce dépôt,
copie locale éphémère ; règle de visibilité unique (0029), double numérique
sans règle particulière.

Restent :

1. **Amender 0029 ?** L'intérim recommandé fait du format de mémoire et du
   texte de persona un **contrat neutre**, géré par un composant séparé, et
   rend le harnais maison facultatif. À confirmer, ou à garder strictement
   comme pont temporaire.
2. **Mémoires natives** désactivées par défaut pour les sessions lancées par
   ameesh (proposé), ou laissées actives à côté ?
3. **Vérification de la visibilité** : par l'API de la forge (proposé pour
   les serveurs et le cluster, couvre les administrateurs) ou par copie
   d'essai avec les identifiants du responsable (suffisant pour un poste
   personnel) ; et où déclarer les administrateurs d'un hôte (clé `admins`
   de la fiche `Host`, proposé).
4. **Identifiant limité au dépôt** : qui le crée et le renouvelle (l'humain
   responsable de la persona, une application de forge d'ameesh), et où
   l'hôte le garde.
5. **Qui consolide** : la session elle-même (tour de mémoire, proposé), un
   relecteur, ou l'humain responsable pour certains types de souvenirs
   (`feedback` par exemple) ?
6. **P1 dans L31** comme le dit 0029, ou lot à part pour ne pas alourdir L31 ?
7. **Pouvoirs du double numérique** (déjà ouvert par 0029).
