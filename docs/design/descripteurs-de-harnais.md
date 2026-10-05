# Descripteurs de harnais (L16, R22)

Un **harnais** n'est plus une entrée d'une liste codée en dur : c'est un
**descripteur** JSON, au format du manifeste `agent.json` du registre ACP,
étendu par un espace de clés `ameesh` pour ce que l'ACP ne décrit pas.

* Décision : [0021 — catalogue des harnais](decisions/0021-catalogue-des-harnais.md),
  [0007 — standards](decisions/0007-standards-des-harnais.md).
* Étude : [standards des harnais](etudes/standards-des-harnais.md).
* Commandes : `ameesh harness list|show|check` ; validation par
  `ameesh canon check` pour les harnais cités par le canon.

## Où vivent les descripteurs

| Source | Emplacement | Remarque |
|---|---|---|
| **paquet** | `ameesh/harness_descriptors/*.json` (`claude`, `codex`, `deepseek`) | livrés avec le code, jamais absents |
| **hôte** | `$AMEESH_HARNESSES_DIR` (alias `AGENT_MESH_HARNESSES_DIR`), sinon `~/.config/ameesh/harnesses/` | épinglage local ; `*.json` ou la disposition du registre `<id>/agent.json` |

Pour un même `id`, **l'hôte prime** sur le paquet : `harness list` et
`canon check` le signalent (`harness-host-overrides`).

Le dossier de l'hôte est un **point de chaîne d'approvisionnement** (décision
0021) : il doit appartenir à l'utilisateur de l'exécuteur, être privé (0700) et
ne pas être atteint par un lien (le dossier lui-même, un sous-dossier ou un
fichier). La racine est ouverte **composant par composant depuis la racine du
système**, sans suivre aucun lien (`O_NOFOLLOW`) : **ses ancêtres** aussi sont
donc protégés, et un lien nulle part dans son chemin fait échouer la lecture.
Chaque descripteur est ensuite ouvert en `O_NOFOLLOW` composant par composant
(`dir_fd`), et ses attributs (type, propriétaire, modes) sont vérifiés par
`fstat` **sur le descripteur effectivement acquis**, jamais par un `stat`
préalable qui pourrait porter sur un autre objet. Le dernier composant est
ouvert en `O_NONBLOCK` : un type non régulier (FIFO, périphérique) est refusé
**sans attendre un écrivain**, et seul un fichier régulier est repassé en
bloquant pour la lecture. Les octets lus **une seule fois** sont à la fois
empreintés (SHA-256) et analysés, et toute modification entre la construction
de la commande et le lancement du pont ACP est refusée (l'empreinte est
épinglée par `--descriptor-sha256`). Un dossier ou un fichier non conforme est
**ignoré**, avec un diagnostic (`harness-host-insecure`) remonté par
`harness list` et `canon check`. Chaque descripteur chargé est journalisé avec
sa provenance et son empreinte SHA-256 (`AgentWorker`, et le pont ACP). La
signature et l'installation à version épinglée restent le lot L17.

## Manifeste (premier niveau)

Champs du registre ACP repris tels quels :

| Clé | Requis | Contenu |
|---|---|---|
| `id` | oui | identifiant unique : `^[a-z][a-z0-9-]*$` (c'est le `harness` du canon) |
| `name`, `version`, `schema_version`, `description` | oui | textes non vides |
| `capabilities` | non | méthodes ACP parlées : objet `méthode → booléen`, ou liste |
| `authentication` | non | options d'authentification (`{"methods": [{"id", "type"}]}`) |
| `distribution` | non | `binary` (`<os>-<arch>` → `archive`, `cmd`, `args`, `env`), `npx`, `uvx` |
| `repository`, `license`, `authors`, `icon` | non | métadonnées |
| `ameesh` | oui | espace de clés ci-dessous |

Un document invalide n'est **pas** un harnais connu : il ne peut pas être
réclamé, et `harness check` explique pourquoi. Les clés inconnues et un
`schema_version` majeur différent sont des avertissements, pas des refus.

## Espace `ameesh`

### Pilotage sans interface

| Clé | Contenu |
|---|---|
| `protocol` | `cli` (piloté par descripteur) ou `acp` (pont ACP générique) |
| `binary` | nom (résolu par `AMEESH_<HARNAIS>_BIN`, alias `AGENT_MESH_*`, `AMEESH_BIN_DIR`, puis le PATH) ou chemin |
| `binary_env` | variables de surcharge du binaire, dans l'ordre |
| `launcher` | arguments du **lanceur**, avant les options (ex. `--profile agent`) |
| `command` | arguments de l'application (ex. `--json`), après les options |
| `interactive` | arguments d'`ameesh attach` ; `null` = attachement refusé (défaut en ACP) |
| `session` | reprise hors ACP : `{"flag": ["--resume"]}` (inséré juste avant le texte) |
| `stream` | lecteur de flux : `claude-stream-json`, `codex-json`, `dsh-json`, `acp-json`, `text` |
| `env` | variables posées au lancement du harnais (`cli`) ou de l'agent (`acp`) |

Ordre de la ligne de commande (`cli`) :

```
<binaire> <launcher> <options model/effort/tier> <command> <session.flag session> <texte>
```

Cet ordre n'est pas cosmétique : `--patch` de DeepSeek est une option du
**lanceur** et doit précéder `--json`. Un réglage livré par fichier
(`model.file.template`) écrit le fichier au chemin `{path}` fourni par
l'exécuteur, puis insère son drapeau (`--patch {path}`) à la place des options.

| Clé | Contenu |
|---|---|
| `model`, `effort`, `tier` | `{"flag": ["--model", "{model}"], "default": "…"}` ou `{"file": {"template": "…{model}…{effort}…"}, "flag": ["--patch", "{path}"]}` ; placeholders `{model}`, `{effort}`, `{tier}`, `{path}`, `{id}` |

Un seul des trois réglages peut être livré par fichier.

### Hooks, jauges et coût, comptes, ACP

| Clé | Contenu |
|---|---|
| `hooks` | déclaratif (généré en L17) : ex. `{"mail": {"command": ["agent-mail", "hook", "claude"], "events": [...]}}` |
| `cost` | `{"source": "stream"\|"api"\|"none", "gauges": "transcript"\|"sessions"\|"balance"\|"none", "paid_per_token": bool}` — `paid_per_token` alimente le plafond horaire de 0019 §2 |
| `permissions` | politique de permission ACP : `{"default": "deny"\|"allow", "allow_kinds": [...], "deny_kinds": [...]}` (kinds ACP : `read`, `edit`, `delete`, `move`, `search`, `execute`, `think`, `fetch`, `switch_mode`, `other`) |
| `acp` | `{"auth_method": "…", "config": {"model": "…", "effort": "…", "tier": "…"}}` : méthode d'authentification et identifiants d'options de configuration |
| `accounts` | comptes multiples (L30) : `{"config_env", "default_home", "session_store", "credentials", "key_env"}` |
| `install` | note déclarative d'installation (la distribution signée est L17) |

### Protocole `acp`

L'adaptateur ACP est **générique** : il n'y a rien à coder pour un agent qui
parle ACP, seulement un descripteur.

`ameesh.adapter_for(<id>).command()` lance le pont
`python -m ameesh.acp run --descriptor <fichier> --agent <binaire> …` dans le
groupe de processus de l'agent, avec la consigne en dernier argument. Le pont :

1. lance l'agent (`<binaire> <launcher> <command>`) et parle JSON-RPC 2.0 sur
   stdio, un objet par ligne (transport ACP stdio) ;
2. `initialize` (version 1, `clientCapabilities.fs` en lecture/écriture,
   `clientInfo`) ; `authenticate` si `ameesh.acp.auth_method` est déclaré ;
3. reprend la session par `session/resume` si l'agent annonce
   `sessionCapabilities.resume`, sinon par `session/load` s'il annonce
   `loadSession` (le rejeu n'est pas affiché) ; sinon `session/new` — l'exécuteur
   a alors mis le résumé de reprise (L11, 0018) dans la consigne ;
4. applique `model`, `effort`, `tier` par `session/set_config_option` quand
   l'agent expose une option (`ameesh.acp.config`, sinon la catégorie `model`,
   `thought_level` ou `model_config`) ; au mieux, sans faire échouer le tour ;
5. `session/prompt` puis lit les `session/update` jusqu'à la réponse
   (`stopReason`) ;
6. à SIGTERM/SIGINT : répond `cancelled` aux permissions en attente, envoie
   `session/cancel`, attend `--cancel-grace` (2 s, sous le SIGKILL de
   l'exécuteur), puis tue l'agent (SIGTERM, puis SIGKILL).

**Permissions** : toute `session/request_permission` non couverte par
`ameesh.permissions` est **refusée par défaut** (`reject_once` si l'agent le
propose, sinon l'issue `cancelled`) et journalisée. Aucun accès n'est accordé
par omission.

**Méthodes client** : `fs/read_text_file` et `fs/write_text_file` sont rendues
dans le **dossier de travail du tour** uniquement. Ce dossier est ouvert **une
fois au début du tour**, composant par composant depuis la racine du système,
sans suivre aucun lien : son descripteur est **épinglé pour tout le tour** et
n'est jamais rouvert par son nom, donc remplacer le chemin après l'acquisition
ne déplace pas la frontière. Chaque opération part de ce descripteur et ouvre
chaque composant par `dir_fd` + `O_NOFOLLOW` — aucun `realpath` suivi d'une
ouverture par nom, la course entre contrôle et ouverture est impossible. Un
chemin contenant un composant `..` est refusé **avant** toute normalisation, et
**tout** lien est refusé, même un lien vers l'intérieur ; chaque refus est
journalisé. `terminal/*` et `elicitation/*` ne sont pas rendues : la réponse est
une erreur JSON-RPC, et une méthode client inattendue met fin au tour en erreur.

**Robustesse** : l'enveloppe JSON-RPC est validée (types d'`id`, `method`,
`params`, `result`/`error`), et **toute** exception du fil de lecture est
enregistrée comme panne fatale, qui débloque la requête en vol et conclut le
tour en erreur : un message illisible, trop grand (4 Mio), un `id` non
conforme, une réponse à une requête inconnue ou une notification inattendue ne
laissent jamais le tour attendre le délai. Délais : `--init-timeout` (120 s)
pour l'initialisation et la session, `--idle-timeout` (1800 s) sans aucun
message de l'agent pendant un tour, `--timeout` (aucun) pour le tour entier.

### Correspondance `session/update` → événements JSONL

Le pont écrit un objet JSON par ligne, que `adapters.AcpStream` relit pour
l'exécuteur (`events.jsonl`) :

| Événement du pont | Source ACP | Effet pour l'exécuteur |
|---|---|---|
| `{"type":"session","sessionId":…,"resumed":"resume"\|"load"}` | `session/*` | session du tour (reprise ou neuve) |
| `{"type":"text","text":…}` | `agent_message_chunk` | affiché, accumulé dans le texte final |
| `{"type":"thought","text":…}` | `agent_thought_chunk` | affiché (réflexion) |
| `{"type":"tool","title","kind","status"}` | `tool_call`, `tool_call_update` | affiché |
| `{"type":"permission","decision","reason"}` | `session/request_permission` | affiché et journalisé |
| `{"type":"usage","usage":…,"costUsd":…}` | `usage_update` | jauges de session |
| `{"type":"final","stopReason","text","usage","costUsd"}` | réponse `session/prompt` | fin du tour, coût du tour |
| `{"type":"error","message":…}` | panne ou `stopReason` inattendu | tour en échec |

Le coût ACP est **cumulé** sur la session : le pont rend la différence entre le
premier et le dernier relevé du tour (le dernier seul s'il n'y en a qu'un). Les
jetons de contexte (`used`) sont rendus au plus haut du tour dans un événement
`status`/`step_end` — celui que lit le grand livre des harnais hors
Claude/Codex (0019 §3) : un harnais ACP déclaré `cost.paid_per_token` alimente
donc le plafond horaire. Les jauges se lisent par la source déclarée
(`transcript` et `sessions` sont les lecteurs connus ; `balance` renvoie au
relevé de solde de L26).

## Canon

`harness` d'une fiche `Agent` et `policy.harnesses` d'une fiche `Host`
acceptent **tout identifiant de descripteur connu** (paquet + hôte). Un
identifiant non vide inconnu est une **erreur bloquante** de `canon check`
(`agent-harness-unknown`, `host-harness-unknown`) ; un `harness` absent reste un
avertissement (`agent-harness-missing`). Aucun harnais n'est cité en dur dans le
code : la liste des connus vient de `harnesses.known_ids()`.
