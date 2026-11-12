---
type: Design
title: "Avatar d'humain : conversation continue, placement à préférence, enfants restreints et attach distant"
description: "Conception commune des tickets manaty/ameesh#5322, #5323, #5324 et #5325 : journal de conversation neutre plutôt que checkpoint natif, déplacement à la frontière de tour, placement ordonné poste puis cloud, hôte partagé multi-tenant, spawn à outils/MCP restreints avec canal enfant → parent, et attach distant par relais authentifié. Découpage en sous-lots, classes, estimations, dépendances et décisions owner."
status: draft
tags: [avatar, conversation, placement, hote-partage, spawn, enfants, attach, identite, vie-privee, concurrence]
generated: { by: "mty-amh-impl-01/deepseek", at: "2026-11-12T05:25:00Z" }
sources:
  - { resource: "manaty/ameesh#5322", title: "Continuité de conversation d'un agent entre deux hôtes du même humain (poste ↔ cloud)" }
  - { resource: "manaty/ameesh#5323", title: "Placement avec préférence (poste d'abord, cloud en repli) et hôte partagé pour les agents inactifs" }
  - { resource: "manaty/ameesh#5324", title: "spawn : outils et MCP restreints pour l'enfant, canal de compte rendu enfant → parent" }
  - { resource: "manaty/ameesh#5325", title: "attach à distance : converser depuis son poste avec un agent épinglé sur un hôte cloud" }
  - { resource: "../HOST-HANDOFFS.md", title: "Handoffs d'hôte, relève élue, brief de reprise (L232a/L165)" }
  - { resource: "../CONVERSATIONS.md", title: "Journal de conversation partagé (L78) et fil lisible (R12)" }
  - { resource: "etudes/persona-et-harnais.md", title: "Étude persona et harnais : sessions, mémoire neutre, canaux par harnais" }
  - { resource: "etudes/4516-managed-persona-memory.md", title: "Mémoire de persona gérée : projections, sessions, retrait" }
  - { resource: "decisions/0029-persona-et-session.md", title: "Persona et session ; jumeau numérique" }
  - { resource: "decisions/0032-persona-roles-and-sessions.md", title: "Persona, rôles et sessions" }
  - { resource: "decisions/0033-isolated-meshes-hosting-and-identity.md", title: "Isolement, hébergement, identité et rôles humains" }
  - { resource: "decisions/0028-ressources-des-hotes-et-repartition.md", title: "Placement dynamique entre hôtes admis" }
  - { resource: "decisions/0035-agent-resident.md", title: "Agent résident, conversation, attach d'exception" }
  - { resource: "decisions/0006-lisibilite-humaine.md", title: "Tous les échanges sont lisibles et auditables" }
---

# Avatar d'humain : conception commune (#5322, #5323, #5324, #5325)

**Lot #7031**, note de conception demandée par mesh-design le 2026-11-12.
Lecture du code à `978819429e` (`main` du 12/11) ; les numéros de ligne cités
sont ceux de ce commit. **Cette note ne contient aucun code, aucune migration
et aucun effet de production.** Les sous-lots proposés sont listés au §6 ; les
quatre lots porteurs existants (#7032 à #7035) attendent cette note.

## 0. Recommandation

1. **#5322 — journal neutre, session native = cache local.** La conversation
   qui traverse les hôtes est celle de `conversation_log` (L78) et de son fil
   lisible R12, pas le transcript natif du harnais. Un déplacement ouvre une
   **session native neuve** sur la cible et lui remet un **segment de
   conversation** borné, construit depuis le journal. Le transcript natif ne
   quitte jamais son hôte ; il reste un cache de reprise locale et la base des
   sauvegardes chiffrées (`session_backup`, L53). Le checkpoint natif en base
   est écarté (§2.1) : formats instables et propres à chaque harnais, taille,
   secrets et sorties d'outils dans la base, aucun chemin entre harnais.
2. **#5323 — placement ordonné `releve: preference`.** L'ordre de la fiche
   `Placement` devient une préférence exécutoire (le texte le dit déjà,
   `specification.md:233`, mais `placement.admitted_hosts` trie encore,
   `placement.py:58-72`). Le bail suit la **présence** (fraîcheur du relevé,
   porte, canon, exécuteur) ; le poste reprend à la **frontière de tour**
   suivante ; le cloud partagé est multi-tenant (un utilisateur système et un
   runner par humain, dossiers de comptes 0700, lecture croisée refusée).
   L'infrastructure de relève élue (`agent_candidates`, `election_brief`,
   epoch) est réutilisée, avec un classement par préférence et non par
   disponibilité.
3. **#5324 — outils et MCP en liste blanche, appliquée par le runner de
   l'enfant.** `spawn` reçoit `--tool` / `--mcp` ; la liste est un
   sous-ensemble de celle du parent, vérifiée à la création **et** à la
   configuration du harnais de l'enfant ; un harnais qui ne sait pas prouver
   l'application refuse le spawn. L'enfant rend compte par un canal
   `child_report` (avancement, question, rapport final, copie des messages
   humains) et reçoit un fichier de contexte borné, filtré et chargé à son
   démarrage.
4. **#5325 — attach distant par relais authentifié.** Le harnais reste sur
   l'hôte de l'agent ; le poste ne reçoit que les octets du terminal. Un
   **grant d'attach** court, lié au humain vérifié, à l'agent, à l'hôte et à
   la clé de l'appareil, autorise le relais ; le bail est pris et rendu comme
   un attach local, sans interrompre un tour en cours. Chiffrement de bout en
   bout : le relais route des trames, il ne lit ni ne stocke la conversation.
5. **Découpage** : 15 sous-lots, environ **67 h** (8 jours-agent), en quatre
   phases. La phase 1 (24 h) livre #5322 et le placement à préférence sur des
   hôtes dédiés ; la phase 2 (21 h) ajoute l'hôte partagé, le déplacement
   supporté et le placement des enfants ; la phase 3 (6 h) le canal de compte
   rendu et le contexte ; la phase 4 (16 h) l'attach distant. Les estimations
   d'origine (6 jours) sont revues à la hausse : l'hôte partagé et le relais
   sont deux lots d'infrastructure, pas des finitions.
6. **Décisions owner** : rétention de la conversation d'un avatar ; comptes
   personnels ou clés d'organisation sur un hôte partagé ; qui peut autoriser
   un attach distant et l'exposition du relais ; pouvoirs de l'avatar ;
   unicité de l'avatar par humain ; coût et placement des enfants ; rétention
   du cache natif de la source après un déplacement. Voir §6.

## 1. Ce qui existe déjà, et ce qui manque

| Besoin | Existant | Manque |
|---|---|---|
| Identité durable | `Persona` (0029/0032), carte `Agent` lue en compatibilité, `responsible`, mémoire neutre | aucun type « avatar » ; aucune règle d'unicité par humain ; l'avatar n'est pas distingué d'une persona de rôle |
| Session | `agent_registry.session_id`, `session_account`, `session_reset_at`, rotations, sessions de lot garées (`agent_lot_sessions`) | la session native ne traverse pas un hôte (HOST-HANDOFFS:518) ; le déplacement `relocate` la jette sauf stockage partagé (`relocation.py:36`, `config.py:596`) |
| Conversation | `conversation_log` (L78) : un journal par agent, seq, auteurs, texte, outils, fins de tour ; fil R12 ; 90 j de détail, 1 an de fil | le journal n'est pas utilisé pour rejoindre une session ; l'attach interactif n'y écrit rien (§2.2) ; pas de table de déplacement |
| Placement | `Placement` multi-hôtes, verdicts `placement_ok`, `agent_candidates` pour les rôles élus, `releve: hote|election`, bail/epoch | l'ordre de préférence est perdu (`placement.py:72`) ; un agent `hote` n'est réclamable que sur l'hôte de sa ligne (`registry.py:1024`) ; la présence n'est pas une notion de claim ; l'hôte partagé n'existe pas |
| Hôte partagé | `HostPolicy` : `max_agents`, `lease_ttl`, `volatile`, `intermittent`, `slot_reservations` ; un exécuteur par hôte | pas de `shared`/`tenants` ; un seul utilisateur Unix par hôte ; `host_resources` et `host_lifecycle` sont par hôte, sans dimension tenant ; `require_isolation` refuse dès qu'une autre persona est active sur l'hôte (`persona_memory.py:61-78`) |
| Enfants | `agent spawn --ttl`, capacités ⊆ {read, propose}, placement recopié du créateur (`canon_sync.py:2732`), TTL, courrier | pas de liste d'outils/MCP, pas de canal enfant → parent, pas de contexte de démarrage, l'enfant partage l'hôte, le compte et l'UID du parent |
| Attach | `ameesh attach` local, bail `attach:…`, refus si l'agent est sur un autre hôte (`runner.py:16259`), chat terminal sur le journal (L79) | aucun attach distant ; `ameesh serve` est lié à 127.0.0.1 (`serve.py:9,77`) ; pas de grant humain, pas de relais |
| Sauvegarde | `session_backup` : archive chiffrée par GPG, S3 ou dossier local, clé `hôte/agent/session/…` | ce n'est pas un chemin de déplacement : c'est une sauvegarde auditée, chiffrée, qui ne restaure pas dans le compte d'un autre humain sans `--allow-other-agent` |

### 1.1 La carte d'avatar

L'avatar est une **persona** au sens de 0029 §4 (le jumeau numérique d'un
humain), pas un nouveau type d'autorité. Il ne reçoit aucun droit nouveau :
il ne signe pas, n'approuve pas et ne dépense pas à la place de son humain
(0012) ; ses capacités restent celles de sa carte. La proposition est un bloc
`avatar` **données seulement**, plus deux propriétés déjà connues :

```yaml
type: Persona
title: alice-avatar
responsible: human:alice
team: alice
releve: preference          # classement par l'ordre de la fiche Placement
avatar:
  conversation: portable   # local (défaut) | portable
  children: own-host       # own-host (défaut) | pod | none
# harness/model/provider/credential_mode : comme toute persona
```

- `conversation: portable` active le journal de déplacement (§2) ; `local`
  garde le comportement actuel (session jetée, brief de reprise).
- `children: own-host` impose un hôte ou un pod propre à l'enfant (§3.4) ;
  `none` interdit le spawn depuis cet avatar.
- `releve: preference` exige une fiche `Placement` avec au moins deux hôtes
  **ordonnés** (sinon avertissement `avatar-no-fallback` : l'avatar reste
  plaçable, mais sans repli cloud).
- `canon check` refuse (`avatar-duplicate`) deux avatars du même
  `responsible` dans le même canon, sauf décision owner contraire (§6 Q5).
- `canon check` refuse (`avatar-harness-unsupported`) un avatar dont le
  harnais n'a pas de **mode interactif** et de **mode résident** : l'avatar
  doit pouvoir être attaché et rester piloté par l'exécuteur. À ce jour,
  `claude` et `codex` ont les deux ; `deepseek` a un mode résident ACP mais
  `interactive: null` (`harness_descriptors/deepseek.json:17`) : un avatar
  deepseek est refusé tant qu'un mode interactif n'existe pas (décision de
  lot, pas de cette note).
- Le bloc `avatar` ne donne aucune priorité de claim, aucun secret, aucun
  droit SQL. Il est lu par le canon comme les autres champs et journalisé
  avec la fiche.

## 2. #5322 — la conversation survit au déplacement

### 2.1 Les deux options, et le choix

**Option A — checkpoint du transcript natif en base.** L'exécuteur de la
source archive le transcript natif (Claude `projects/<projet>/<session>.jsonl`,
Codex `sessions/…/rollout-*.jsonl` + base d'état SQLite, DeepSeek
`sessions/*/{session}`), le stocke dans la base, puis la cible l'écrit dans le
dossier de session de son compte et reprend par l'identifiant natif.

**Option B — journal neutre réhydraté par la cible, session native = cache.**
Le journal `conversation_log` est la vérité de la conversation. La cible ouvre
une session neuve et reçoit un **segment** borné, construit depuis le journal,
comme premier tour. Le transcript natif reste sur son hôte.

| Critère | A — checkpoint natif | B — journal neutre |
|---|---|---|
| Fidélité | complète (outils, sorties, raisonnement éventuel) | texte humain + texte assistant + fins de tour ; noms d'outils, pas leurs arguments ni sorties |
| Claude | format JSONL propre à la version, chemins `projects/<projet>` liés au `cwd` et au compte ; `--resume` peut refuser un identifiant déplacé ; le transcript contient tout ce que l'agent a lu | le journal est déjà produit par `Recorder` ; le segment traverse les comptes |
| Codex | rollout JSONL **et** base d'état SQLite ; copier le seul JSONL ne suffit pas ; `thread/resume` dépend de l'état local | idem ; le harnais ouvre un thread neuf |
| DeepSeek | format `sessions/` non documenté ; `--session-id` permet d'en nommer un, pas de garantir la reprise ; pas d'interactif | idem ; avatar refusé tant que l'interactif manque |
| Changement de harnais | impossible sans conversion (aucun format commun) | possible : le journal est le contrat |
| Taille / base | dizaines de Mo par session, à chiffrer et à autoriser ; la base devient un stockage de transcripts | quelques Ko par segment, borné |
| Vie privée | sorties d'outils, secrets lus, raisonnements dans la base ; `session_backup` exige déjà un chiffrement et des destinataires (`session_backup.py:70` : « sessions contain everything the agent has read ») | texte de conversation seulement ; pas d'arguments/sorties d'outils ; le contenu reste dans `conversation_log`, déjà soumis à sa politique |
| Robustesse | dépend de la version du harnais, des chemins, du compte et parfois d'un sidecar ; migration de format à chaque mise à jour | dépend du journal et du lecteur, pas du format natif |
| Coût | 4 à 6 h par harnais, plus stockage/chiffrement/authorisation, plus un test de conformité à chaque version | un contrat, un constructeur de segment, un protocole de déplacement ; 12 h environ |

**Choix : B.** La conversation d'un avatar est une conversation, pas une
reprise de travail : le texte humain, les réponses finales et les frontières
de tour suffisent à répondre à une question qui renvoie à un échange
antérieur. Les sorties d'outils et l'état du travail (worktree, commit, lot)
sont déjà couverts par le brief de reprise déterministe (`reprise.py`) et par
le fil du lot ; le segment le dit et invite l'agent à relire l'état plutôt
qu'à l'inventer. La sauvegarde chiffrée `session_backup` reste le chemin
d'audit et de conservation du transcript natif ; elle n'est **pas** utilisée
pour déplacer une session.

**Ce que A aurait coûté, par harnais**, pour mémoire :

- **Claude** : le transcript est un JSONL non versionné, écrit dans
  `$CLAUDE_CONFIG_DIR/projects/<projet>/<session>.jsonl` ; il contient les
  appels d'outils et leurs résultats, donc des secrets lus. Le copier dans un
  autre compte est un acte de copie de données privées ; le rejouer dans un
  `cwd` différent peut casser les références de projet. Coût 4-6 h, risque
  élevé à chaque version.
- **Codex** : le rollout JSONL ne suffit pas ; l'état des threads est dans une
  base SQLite locale (`codex_gauges`, étude persona-et-harnais §Codex). Il
  faudrait copier les deux, avec les identifiants de thread, et espérer que
  `thread/resume` accepte la base restaurée. Coût 5-7 h, risque élevé.
- **DeepSeek** : `sessions/` n'a pas de format public ; le test de reprise
  serait un test de version à chaque mise à jour. Coût 4-6 h, risque élevé.
- **ACP** : `session/load` rejoue l'historique côté agent, mais aucun format
  de transcript n'est exportable de façon standard ; un checkpoint natif
  n'existe pas. Impossible sans extension propriétaire.

### 2.2 Le journal : ce qui est écrit, ce qui ne l'est pas

Le journal est **`conversation_log`** (migration 0368, `conversation.py`), avec
son fil R12 (`fil.record`) :

- déjà écrit par l'exécuteur : `human_message`, `input`, `text` (tranches de
  texte assistant), `tool` (nom + statut seulement), `turn_end`, `state` ;
  texte plafonné à 20 000 caractères par entrée (`conversation.py:23`),
  métadonnées limitées à une liste blanche (`conversation.py:46`) ;
- jamais écrit : arguments d'outils, sorties d'outils, objets de protocole,
  raisonnement, `display` arbitraire d'un adaptateur (`CONVERSATIONS.md:14`) ;
- rétention : 90 jours de détail, 1 an de fil par défaut (0033 §9,
  `CONVERSATIONS.md:111,119`) ; un fil expiré est tronqué, jamais réécrit ;
- un `event_id` déduplique une entrée rejouée ; `conversation_heads`
  sérialise les écrivains et les séquences suivent l'ordre de commit ;
- le fil est la copie lisible et auditable (0006) ; le détail est la copie
  d'affichage et de reprise.

**Gap à combler : l'attach interactif n'écrit pas dans le journal.** Le
chemin `run_attach` lance le harnais directement (`runner.py:16202`), sans le
`Recorder` des tours pilotés ; le transcript natif reste sur l'hôte et le
journal ne voit ni le prompt humain ni la réponse. Sans ce correctif, un
déplacement après un échange en attach perdrait précisément l'échange que le
ticket #5322 demande de retrouver. Le sous-lot A0 ajoute une **capture par
hook** : le hook `agent-mail` reçoit déjà `UserPromptSubmit` et `Stop`
(`cli.py:1633`), et l'attach pose son identité dans `_attach_env`
(`runner.py:15951`). Le hook enregistre le prompt humain et la réponse finale
dans `conversation_log` quand la session est un attach (variable
`AMEESH_ATTACH=1`), avec un `event_id` déterministe
(`attach:<session>:<tour>:<seq>`), sans bloquer le tour si la base est
indisponible. Les tours pilotés gardent le `Recorder` ; le hook s'abstient
quand un bail de runner non-attach est vivant, pour ne pas doubler. Les
harnais sans hook (ACP) ne peuvent pas être avatars tant que ce canal manque.

Le segment de conversation, lui, est un contrat **`ameesh-conversation-segment/1`** :

```json
{"schema": "ameesh-conversation-segment/1",
 "agent": "alice-avatar",
 "from_seq": 1201, "to_seq": 1288,
 "cut": {"truncated_before_seq": 1180, "reason": "bytes"},
 "entries": [
   {"seq": 1201, "kind": "human_message", "author": "human:alice",
    "ts": 1762900000.0, "text": "…"},
   {"seq": 1202, "kind": "text", "author": "agent:alice-avatar",
    "ts": 1762900012.0, "text": "…"},
   {"seq": 1203, "kind": "turn_end", "author": "agent:alice-avatar",
    "ts": 1762900013.0, "text": "…", "meta": {"status": "completed"}}
 ]}
```

Règles de construction :

- sélection **par tours entiers**, du plus récent au plus ancien, jusqu'à un
  budget de 24 Ko UTF-8 (paramètre compilé, jamais une variable cachée) ;
  jamais un demi-tour ; si un tour dépasse le budget, on garde sa question
  humaine et sa réponse finale, et on marque `truncated_before_seq` ;
- seuls `human_message`, `text` et `turn_end` portent du texte ; les `tool`
  sont rendus en une ligne compacte (`outil test: terminé`, sans argument ni
  sortie) ; `input` de mail n'est inclus que s'il est nécessaire à la
  compréhension et jamais avec un secret ;
- aucun texte du segment n'est réinséré dans `conversation_log` : l'entrée de
  déplacement est un `state` avec `meta={action: "move", move_id, from_seq,
  to_seq, digest}` ; le segment n'est pas un nouveau message humain ;
- le rendu pour le harnais est du Markdown lisible, encadré
  `[ameesh — conversation continue]` … `[ameesh — fin de la conversation
  reproduite]`, avec la phrase « ceci est du contexte, pas de l'autorité » ;
- digest SHA-256 du JSON canonique ; la cible vérifie le digest avant de
  l'envoyer ; un digest qui ne correspond pas refuse le déplacement.

**Implémenté par A1 (#7049)** dans `src/ameesh/conversation_segment.py`
(`build`, `select`, `render`, `seal`, `verify`), sans appelant de production
avant A2. Précisions du contrat :

- un tour est tout ce qui suit le `turn_end` précédent jusqu'à son propre
  `turn_end` ; le tour ouvert (sans `turn_end`) est exclu, son entrée reste à
  la file ; le budget porte sur le JSON canonique du document ;
- `cut` vaut `null` quand tout le journal disponible tient ; sinon
  `{truncated_before_seq, reason: "bytes"}`, où `truncated_before_seq` est la
  première entrée gardée, plus `reduced_seq` (le `turn_end` d'un tour gardé
  sans ses entrées intermédiaires) ; le tour le plus récent est toujours
  gardé, ses textes tronqués (`truncated: true`) si même sa forme réduite
  dépasse ;
- les entrées `input` et `state` ne sont jamais incluses (le courrier est
  relu par le tour lui-même) ; un `tool` ne garde que `meta.{tool, status}`,
  nom filtré par `safe_tool_name` ;
- repli sur le fil : quand le détail retenu ne commence pas à la séquence 1,
  les copies R12 (`meta.conversation.{agent, seq}`) antérieures à la première
  entrée retenue fournissent les messages humains et les réponses finales
  (`source: "thread"`) ;
- filtre de secrets : chaque valeur exacte de secret connu
  (`turn_context.environment_secrets`) est remplacée par `[secret retiré]`,
  puis `seal` refuse le document si une valeur subsiste, brute ou échappée
  JSON, dans le JSON ou dans son rendu (`turn_context.contains_secret`, la
  garde du writer du contexte de tour) ;
- rendu : chaque message est cité (`> `), si bien qu'aucun texte ne peut
  reproduire seul sur sa ligne le marqueur de fin.

### 2.3 Le protocole de déplacement

Le déplacement est un **handoff de conversation** à la frontière de tour,
distinct du handoff d'hôte L232 (qui publie un worktree et peut copier une
session seulement si le stockage est partagé). Il est déclenché par le
classement de préférence (§3) ou par `ameesh agent move` (déplacement manuel
supporté). Machine d'état, dans une table `conversation_moves` (une ligne par
tentative, clé d'idempotence `(agent, epoch_source)` ) :

| État | Ce qui est vrai | Effet |
|---|---|---|
| `planned` | un meilleur hôte est présent et prêt ; la source finit son tour en cours ; aucun nouveau tour n'est ouvert ; les entrées humaines restent à la file | la source ne sert plus de nouveau tour ; le bail n'est pas encore rendu |
| `prepared` | la cible a vérifié harnais, compte, dossier, binaire, mode résident et interactif ; **aucun appel modèle** | si la préparation échoue : `failed`, la source reprend son service, rien n'a bougé |
| `switched` | à la fin du tour source, le bail est rendu (epoch++) et la cible réclame en mode `move` | la ligne du registre passe à la cible ; `session_reset_at` n'est **pas** posé tant que la cible n'a pas réussi son premier tour |
| `rehydrating` | la cible ouvre une **session native neuve** (identifiant neuf), construit le segment, l'envoie en premier tour, avec le `move_id` | la session source reste intacte sur la source ; la cible n'écrit pas encore `session_id` dans le registre |
| `verified` | le premier tour cible s'est terminé (réponse finale ou usage enregistré) | la cible écrit `session_id`, `session_account`, `session_harness` ; la source devient `stale-cache` ; le déplacement est clos |
| `failed` | la cible n'a pas pu préparer, lancer ou terminer son premier tour | la cible rend le bail (epoch++) sans consommer les entrées ; la source, si elle est encore présente, réclame au rang 0 et reprend **sa** session native ; si la cible a déjà commencé un tour, la source rejoue la conversation depuis le journal et l'agent est averti qu'un tour a pu partiellement s'exécuter |
| `recovered` | la source est morte (poste éteint) ; la cible réclame après expiration du bail | pas de source à conserver ; la cible ouvre une session neuve et rehydrate ; l'entrée humaine en cours est rejouée une fois (idempotence `event_id`) |

Points de sûreté :

- **Frontière de tour** : la source ne commence aucun tour après `planned` ;
  un tour en cours n'est jamais interrompu. Une entrée `!urgent` pendant le
  déplacement attend le tour cible.
- **Session provisoire** : la cible ne remplace `agent_registry.session_id`
  qu'à `verified`. Tant que ce n'est pas fait, la ligne garde la session de la
  source et `session_reset_at` n'est pas bougé : un échec ne détruit donc pas
  la reprise locale de la source. C'est la différence avec la relève élue, qui
  remet la session à zéro dès le claim (`HOST-HANDOFFS.md:518`) parce qu'elle
  n'a pas de conversation à préserver.
- **Source gardée** : le fichier de session natif de la source n'est ni copié
  ni supprimé avant `verified` ; il est marqué `stale-cache` ensuite et purgé
  selon la rétention décidée (§6 Q7). Aucun fichier d'identifiants ne quitte
  l'hôte : le segment ne contient que du texte, et la cible utilise **son**
  compte local.
- **Pas de reprise de la session native à travers l'hôte** : la cible ouvre
  toujours un identifiant neuf, même si le harnais est le même. Un retour
  ultérieur sur la source n'ouvre pas l'ancien fichier : il rehydrate depuis
  le journal, qui contient les tours faits ailleurs.
- **Idempotence** : `move_id` (UUID) est écrit avant le premier effet ; un
  doublon de claim, de construction de segment ou de premier tour relit la
  ligne et ne rejoue pas ; le digest du segment est stable ; un `event_id`
  identique pour l'entrée `state` et pour l'entrée humaine rejouée.
- **Deux porteurs jamais** : le claim reste le seul arbitre (CAS + epoch) ;
  la cible ne sert aucun tour avant d'avoir le bail ; la source ne renouvelle
  plus ; le test « deux porteurs » est explicite.
- **Base indisponible** : pas de déplacement (fail closed) ; la source
  continue si elle a la base pour ses tours ; sinon elle s'arrête, comme
  aujourd'hui.

### 2.4 Livraison au harnais, sans `argv`

Le segment peut contenir une conversation privée : il ne doit pas apparaître
dans `argv` (visible par `ps` et `/proc/<pid>/cmdline`, y compris par un autre
utilisateur d'un hôte partagé). Il est donc envoyé :

- pour un avatar **résident** (0035) : comme premier `send(text)` dans le
  tuyau du processus résident (Claude stream-json, Codex app-server, ACP) ;
  c'est déjà un canal privé, le processus est sur l'hôte de l'agent ;
- pour un tour par événement de secours : par `stdin` du harnais, ou par un
  fichier 0600 dans l'état de l'agent dont le chemin est passé dans
  `AMEESH_TURN_CONTEXT` ; jamais dans la ligne de commande ;
- `turn_context` gagne un champ **additif** `conversation` :
  `{move_id, from_seq, to_seq, digest, source_host, source_session}` — jamais
  le texte ; le document est 0600 (`turn_context.py:38-39`) et le writer
  refuse déjà les secrets connus.

Le premier tour cible est un tour normal pour le `Recorder` : il enregistre
les entrées du tour (la réponse), pas le segment. L'entrée `state` du
déplacement porte le digest et les bornes ; le journal ne duplique pas
l'ancienne conversation.

### 2.5 Ce que voit l'humain

Le client d'attach (local ou distant) affiche, avant de rendre le terminal au
harnais, la dernière page du journal partagé (`conversation.history`) et un
repère « conversation continuée depuis `<source>` » ; le client `ameesh chat`
lit le même journal. Le harnais, lui, reçoit le segment comme contexte. Ainsi
l'humain voit **une** conversation, même si le terminal natif change de
session ; le transcript natif n'est pas la référence (0006 : le fil est la
référence).

### 2.6 Acceptation et témoins

- Attach avant et après le déplacement : le journal a une séquence continue,
  sans trou ni doublon ; le client affiche la même conversation.
- Question sur un échange antérieur au déplacement : le segment contient la
  question et la réponse, l'agent répond correctement ; témoin avec un
  échange fait **en attach** (A0) et un échange fait par mail.
- Aucun fichier d'identifiants ni de session native ne quitte l'hôte :
  vérifier qu'aucune ligne de `conversation_moves` ou du segment ne porte de
  chemin de credential ou de transcript, et qu'aucun fichier n'est écrit
  hors de l'hôte cible ; test négatif avec un faux transcript piégé.
- Échec de déplacement : la cible ne peut pas lancer (binaire absent, compte
  sans crédential, tour en erreur) ; la source reste en service et répond au
  tour suivant ; la ligne de session de la source est intacte.
- Frontière de tour : un tour long sur la source retarde le déplacement ; il
  n'est pas tué ; aucun tour cible ne démarre avant la fin.
- Deux porteurs : un seul bail vivant, un seul `turn.started` par transition,
  l'epoch ne recule pas.
- Rétention : un segment construit après 90 jours de détail mais avec un fil
  encore vivant utilise le fil pour les messages humains et les réponses
  finales ; le test le vérifie (ou la rétention est prolongée, §6 Q1).

## 3. #5323 — placement à préférence et hôte partagé

### 3.1 Le manque actuel, précisément

- Le texte du canon dit déjà « hôtes admis (ordre = préférence) »
  (`specification.md:233`), mais `placement.admitted_hosts` trie les noms
  (`placement.py:58-72`) et `canon sync` recopie ce tri dans
  `agent_registry.admitted_hosts` (`canon_sync.py:2034`). L'ordre est perdu
  avant le claim.
- Un agent `releve: hote` n'est réclamable que sur l'hôte de sa ligne
  (`registry.py:1024` : « un agent `hote` ignore `host` et ne peut être
  réclamé que sur l'hôte de sa ligne ») ; la relève croisée n'existe que pour
  `releve: election`, avec un classement par disponibilité sur sept jours,
  une hystérésis d'une heure et une reprise de session remise à zéro
  (`election.py:97-105`, `HOST-HANDOFFS.md:510-534`). Ce n'est pas la
  préférence demandée : un poste qui revient ne reprend pas au tour suivant,
  il attend une heure de marge.
- `relocate` ne conserve la session que si `shared_sessions` est vrai
  (`relocation.py:317-331`, `config.py:596`) ; sinon il jette la session et
  ouvre un brief de reprise (`FALLBACK_SUMMARY`, `relocation.py:36`). Un
  avatar ne peut pas accepter cette perte.
- La présence n'est pas définie comme une condition de claim : un hôte peut
  être « admis » et absent, ou présent et sans exécuteur ; le bail expire
  selon un TTL, mais aucun classement ne dit « le poste est allumé, donc il
  doit reprendre ».
- Aucun hôte partagé : `HostPolicy` n'a ni `shared` ni `tenants`
  (`canon.py:1094-1151`) ; `host_resources` et `host_lifecycle` sont par hôte
  (`0029_ressources_placement.sql:25`, `0380_host_lifecycle.sql:3`) ; un seul
  utilisateur Unix `ameesh` porte les runners. `require_isolation` refuse dès
  qu'une autre persona active est sur l'hôte (`persona_memory.py:61-78`).

### 3.2 `releve: preference` : ordre, présence, claim

**Ordre.** `admitted_hosts` conserve l'ordre de déclaration : hôtes nommés
dans l'ordre de `hosts`, puis hôtes d'étiquettes dans l'ordre du canon, sans
doublon ; le tri n'est appliqué qu'à l'affichage et aux diagnostics. La
colonne `agent_registry.admitted_hosts` garde cet ordre ; un nouveau champ
`preference_rank` (ou l'index dans la liste) est écrit dans
`agent_candidates` pour chaque hôte candidat. `releve: preference` exige que
tous les hôtes de la liste soient des candidats évalués ; un hôte non admis
est une erreur `agent-preference-host-not-admitted`.

**Présence.** Un hôte est **présent** pour un agent si, au moment du claim :

1. son dernier relevé `host_resources` est frais selon son propre
   `staleness_s` (`host_lifecycle` ; `election.freshness_s` fait déjà ce
   calcul) ;
2. `host_lifecycle` est `available` (ni `draining`, ni `lost`, ni tenu par un
   `host_hold`) ;
3. sa porte est ouverte (`ameesh_host_claims_open`) et son `canon_state` est
   `ok` ;
4. son verdict de placement pour l'agent est `ok` (profil courant) ;
5. un exécuteur **du tenant** est vivant (`runner_presence`, §3.4) ;
6. pour un poste intermittent, le silence est normal : `intermittent: true`
   ne bloque pas la présence, il supprime seulement l'alerte ; la présence
   reste la fraîcheur du relevé.

La présence n'est **pas** l'activité humaine : un poste allumé et joignable
est présent même si son humain est absent, c'est le besoin du ticket. Un
poste éteint ou hors réseau n'est pas présent ; son bail cesse d'être
renouvelé et expire.

**Claim ordonné.** Un candidat de rang `r` peut réclamer si aucun candidat de
rang `< r` n'est présent et prêt, ou après le délai de rang
`r × RANK_DELAY_S` (20 s, valeur réutilisée d'`election.py:97`) depuis qu'il
a vu le bail libre. Le CAS de `registry.claim` reste le seul arbitre ; le
délai est une politesse côté client. Le rang 0 (le poste) claim immédiatement
s'il est présent ; le cloud attend 20 s, ce qui évite de prendre le bail
pendant que le poste revient.

**Retour au poste.** Quand un candidat de rang meilleur devient présent :

1. il doit rester présent `PREFERENCE_DWELL_S` (proposé : 120 s) sans trou ;
2. le détenteur actuel finit son tour en cours, puis libère le bail à la
   frontière (`release` + epoch++), en écrivant un `preference_handover` ;
3. le meilleur candidat claim au rang 0 ; l'ancien détenteur ne réclame pas
   avant `PREFERENCE_HOLDOFF_S` (proposé : 120 s + délai de rang), pour ne
   pas reprendre le bail si le candidat meurt juste après ;
4. si le meilleur candidat ne claim pas, l'ancien reprend après le holdoff :
   le rôle ne reste pas libre.

**Absence du détenteur.** Le détenteur cesse de renouveler ; le bail expire
selon `HostPolicy.lease_ttl` (un avatar devrait avoir un TTL court : 90-120 s,
au lieu des 300 s par défaut). Le prochain candidat présent claim. Un tour en
cours sur un hôte qui disparaît n'est pas « interrompu par le claim » : il est
perdu avec l'hôte ; son entrée humaine est rejouée une fois sur la cible, avec
la mention « le tour précédent a été interrompu ». Le journal conserve la
question ; l'agent ne doit pas supposer que le tour a abouti.

Le mécanisme de claim croisé, l'`election_brief`, l'epoch et le fencage de
l'ancien détenteur sont réutilisés ; seul le **classement** change
(préférence au lieu de disponibilité) et le brief devient le segment de
conversation (§2.3), avec la session provisoire.

### 3.3 Ce qui n'est pas `election`

`releve: election` reste pour les rôles de gouvernance : disponibilité sur
sept jours, hystérésis d'une heure, brief d'élection déterministe, pas de
conversation à préserver. `releve: preference` est pour les avatars (et, par
extension, toute persona qui veut un ordre explicite) : l'ordre est déclaré,
le retour est au tour suivant, la conversation est conservée. Les deux
partagent le claim, `agent_candidates`, le bail et l'epoch ; ils ne partagent
ni le classement ni le brief. Un agent ne peut pas être les deux : `canon
check` refuse `releve: preference` avec `candidats` (le champ `candidats`
reste réservé à l'élection) — la préférence vient de l'ordre du `Placement`.

### 3.4 Hôte partagé multi-tenant

**Modèle.** Un hôte physique ou une VM cloud peut héberger plusieurs
**tenants** (un par humain). Chaque tenant a :

- un **utilisateur système** propre (`ameesh-<membre>`, ou un identifiant
  dérivé non secret), sans groupe partagé, home 0700, `linger` activé pour
  ses unités utilisateur ;
- un **runner** propre (`ameesh-runner-tenant@<membre>.service`) sous cet
  utilisateur, avec son `AMEESH_RUNNER_ID` et son `agents_filter` limité aux
  agents dont `responsible = human:<membre>` ;
- ses **dossiers de comptes** de harnais sous son home (0700), avec ses
  profils `accounts` (`accounts.py:227-248`) ; aucun dossier de compte n'est
  partagé entre tenants ;
- une ligne `runner_presence(host, tenant, runner_id, seen_at, version)`, et
  une identité DB `tenant:<membre>` vérifiée par l'hôte.

**Superviseur d'hôte.** Un service d'hôte (utilisateur système dédié, sans
harnais d'agent) possède l'état **machine** : `host_resources`,
`host_lifecycle`, les portes, la capacité et le ménage. Les runners de tenant
ne publient pas `host_resources` ; ils publient leur présence et prennent les
baux. Cela évite plusieurs écrivains concurrents sur les mêmes lignes
`host_resources`/`host_lifecycle` et donne un point unique pour la capacité
de l'hôte.

**Claim et isolation.** Le prédicat de claim d'un hôte partagé ajoute
`agent_registry.responsible = <tenant>` (ou une colonne `tenant` écrite par
le superviseur) : même si le fichier de configuration d'un runner était
modifié, la base refuse de lui donner l'agent d'un autre humain. Le
`agents_filter` reste une optimisation et un diagnostic, jamais la garde.
`require_isolation` compare désormais des **UID vérifiés** (le tenant de
l'agent, écrit par le superviseur) et non « une autre persona active sur
l'hôte » : deux personas de deux tenants différents sont isolées ; deux
personas du même tenant (ou un UID inconnu) restent refusées. C'est la couche
1 de L155 (un utilisateur Unix par persona) appliquée à l'hôte partagé.

**Provisionnement.** Un outil `ameesh host tenant add|list|remove` (source
only, aucune exécution réelle dans le lot) crée l'utilisateur, le home, les
profils de comptes, l'unité et la ligne de présence ; les secrets sont
installés par l'humain ou son tenant, jamais dans le canon. La suppression
d'un tenant est refusée s'il a un bail vivant, un tour ou des agents non
déplacés. C'est un geste d'infrastructure : bureau, jamais un agent.

**Limite honnête** : le runner d'un tenant a la chaîne de connexion de la
base du mesh ; il peut lire les lignes des autres si son code est modifié.
La séparation demandée par le ticket est celle des **dossiers de comptes**
(filesystem, 0700, UID distincts) ; l'isolation SQL par rôle est un travail
distinct (0033/L155) et n'est pas revendiquée ici. Le test d'acceptation est
donc : un runner de tenant A ne peut pas lire le dossier de comptes de B, et
la base refuse à A le claim d'un agent de B.

### 3.5 Déplacement supporté entre partagé et dédié

Deux chemins, tous deux sans copie de session ni de credential :

1. **Par le canon** (chemin normal) : une PR change l'ordre ou la liste de la
   fiche `Placement` ; `canon sync` réévalue ; le prochain retour de tour
   déclenche le handoff de préférence (§3.2) et le segment (§2.3). C'est le
   chemin supporté pour un changement durable.
2. **Par `ameesh agent move`** (chemin immédiat) : la commande accepte un
   avatar `releve: preference` et un hôte cible ; elle vérifie la présence,
   le tenant, le compte, le harnais, l'absence de tour, l'absence de handoff
   ouvert et de `host_hold` ; elle écrit `agent.moved` et
   `conversation_moves` en une transaction, libère le bail source et laisse
   la cible claim. Elle ne demande ni root ni accès SSH au poste ; un humain
   responsable ou un pilote l'exécute. Un déplacement vers un hôte dont le
   tenant n'a pas de profil de compte est refusé avant tout effet
   (`move-account-missing`).

Dans les deux cas, le compte et les credentials de la cible sont **locaux** :
le poste garde les siens, le cloud garde les siens ; aucun fichier
d'identifiants ne traverse. C'est la différence avec `relocate` quand
`shared_sessions` est vrai : ici le stockage des sessions n'est jamais
partagé.

### 3.6 Placement des enfants

`agent spawn` doit placer l'enfant sur **son propre hôte ou pod**, jamais sur
l'hôte, l'UID ou le runner du parent. Proposition :

- `avatar.children: own-host` (défaut) : l'enfant est un agent éphémère
  (`agent spawn --ttl`) avec sa propre fiche de placement, choisi parmi les
  hôtes admis du parent **autres que l'hôte du parent**, ou sur un hôte
  dédié aux enfants ; son runner est un runner de tenant (ou un runner
  d'enfant) sous son propre UID ; son worktree et son compte sont neufs.
- `avatar.children: pod` : un pod par enfant sur un hôte qui déclare
  `policy.sandbox` (le placement Kubernetes de #2202), avec un système de
  fichiers et un compte propres ; le pod exécute un runner d'enfant, pas un
  Job d'un seul tour, pour que l'enfant puisse vivre plusieurs tours et
  rendre compte. C'est un lot d'infrastructure distinct (B5b, hors des 15
  sous-lots estimés ici), après l'hôte dédié.
- `avatar.children: none` : `spawn` refuse (`spawn-forbidden`).

Si aucun placement propre n'est disponible, `spawn` **refuse** au lieu de
replier l'enfant sur le parent : un enfant au même UID peut lire les
credentials du parent et contourner sa liste d'outils (§4.1). Le message dit
l'hôte/pod manquant et ce qu'il faut provisionner.

## 4. #5324 — enfants restreints, canal de compte rendu, contexte

### 4.1 Outils et MCP : de la donnée à l'application

La carte `Agent`/`Persona` porte déjà `tools` (`canon.py:1045`,
`canon.py:4054`) ; la spécification donne l'exemple
`tools: [git, mcp:transport-readonly]` (`specification.md:129`). Ce champ est
lu et recopié dans le profil de placement, mais **aucun chemin du runner ne
l'applique** aujourd'hui : `grep` ne trouve pas de consommation dans
`runner.py` ou `adapters.py`. C'est donc une donnée de politique, pas encore
une garde.

Proposition :

- **Vocabulaire** : `tools` reste une liste de noms de politique ; un serveur
  MCP s'écrit `mcp:<nom>` (forme déjà utilisée par la spécification). Pas de
  nouveau champ `mcp`, pour ne pas créer deux vocabulaires.
- **Parent** : la liste effective du parent est `tools` de sa carte. Un
  enfant ne peut recevoir que `--tool`/`--mcp` dont chaque nom est dans cette
  liste ; `spawn` refuse un nom absent (`spawn-tool-not-in-parent`). Un
  enfant ne peut pas élargir sa liste plus tard ; un petit-enfant est borné
  par la liste de son parent (transitivité).
- **Application par le runner de l'enfant** : un bloc de descripteur
  `ameesh.spawn` déclare, par harnais, comment exprimer la liste :
  - Claude Code : `--allowedTools` / `--disallowedTools` et
    `--mcp-config` + `--strict-mcp-config` ; l'étude persona-et-harnais
    §Claude vérifie `--mcp-config` par `--help`, les deux premiers restent à
    qualifier par un test du descripteur (preuve d'application, sinon refus) ;
    un fichier de réglages privé par enfant,
    écrit sous son état, jamais dans le `CLAUDE_CONFIG_DIR` du compte ;
  - Codex : `-c` / profil de session pour `mcp_servers` et les outils
    (documenté, à qualifier), sans écrire dans le `CODEX_HOME` partagé ;
  - DeepSeek : `--patch` (`dsh-mcp-client`, outils seulement) et la
    politique de permission du profil ; à qualifier ;
  - ACP : `mcpServers` à `session/new`/`resume`, et `ameesh.permissions` pour
    les kinds ; aucun nom d'outil standard, donc un agent ACP sans extension
    déclarée ne peut pas être un enfant restreint.
- **Preuve, pas promesse** : le descripteur déclare un contrat
  `ameesh-spawn-tools/1` et un harnais de qualification (faux harnais qui
  énumère ses outils/MCP). Le lot refuse un harnais qui ne prouve pas que la
  liste est appliquée (`spawn-tools-unsupported`) ; il ne se contente pas
  d'écrire un fichier de configuration. Le profil effectif (liste, digest du
  fichier généré) est écrit dans une ligne `spawn_profiles` et dans le
  `turn_context` de l'enfant.
- **Limite de sécurité** : un enfant sous le même UID que le parent peut lire
  ses credentials et contourner la liste. La liste est une **garde contre
  l'erreur et l'injection**, pas une frontière contre un enfant malveillant.
  La frontière vient du placement propre (§3.6) ; sans lui, le lot le dit et
  ne prétend pas isoler.

### 4.2 Canal enfant → parent

Un canal asynchrone, idempotent, borné, sans autorité :

- **Rapports** : `ameesh child report --kind progress|question|final
  [--body-file F]` depuis la session de l'enfant. Le corps est borné
  (8 Ko), scanné par les motifs de secrets, et porte un `event_id`
  (`child:<enfant>:<kind>:<seq>`). Le rapport est écrit :
  1. dans le journal du parent (`conversation_log`, kind `input`, auteur
     `agent:<enfant>`, `meta={child, report_kind, child_seq}`) ;
  2. dans le fil lisible du parent (`fil.record`, comme un message) ;
  3. par un courrier au parent (`agent-mail send`), qui est le réveil ; le
     corps du rapport est autosuffisant (0006).
- **Automatique** : le runner de l'enfant écrit `started` à l'ouverture et
  `final` à la fermeture/expiration, avec le dernier texte final et le statut
  (`completed|failed|expired`) ; un enfant qui meurt sans rapport laisse un
  `final` d'expiration, jamais un silence.
- **Copie des messages humains** : quand un humain vérifié parle à l'enfant
  (attach ou chat), le runner de l'enfant ajoute au journal du parent une
  copie `input` avec l'auteur humain et `meta={child, copied_from: <seq>}`.
  La copie ne porte aucune autorité ; elle est bornée et idempotente. Elle
  n'est faite que si le parent et l'enfant ont le même `responsible` (ou un
  humain explicitement autorisé, §6 Q3) : on ne recopie pas la conversation
  privée d'un humain vers l'avatar d'un autre.
- **Question** : une question est un rapport ; le parent répond par courrier
  à l'enfant. Aucun RPC bloquant : l'enfant peut finir son tour et attendre
  le réveil, ou continuer s'il n'a pas besoin de la réponse.
- **Fin** : le rapport final de l'enfant parvient au parent même si l'enfant
  expire ; le parent le voit dans son journal et son courrier.

### 4.3 Fichier de contexte

- `agent spawn --context FILE` (un seul fichier, ou un répertoire refusé) :
  lecture par le parent, borne 16 Ko UTF-8, refus des motifs de secrets
  (`turn_context.environment_secrets` et les motifs existants), digest
  SHA-256.
- Stockage : une ligne `spawn_contexts(child, parent, digest, bytes, text)`
  ou un blob dans la ligne de l'enfant, lisible seulement par le runner du
  parent et celui de l'enfant ; le contenu est supprimé avec l'enfant (ou à
  l'expiration, après le rapport final). Pas de fichier partagé entre hôtes.
- Chargement : le runner de l'enfant écrit un fichier 0600 sous l'état de
  l'enfant, le passe par `AMEESH_TURN_CONTEXT` (champ `spawn_context`) et
  l'inclut dans le premier tour, ou le laisse lire par le harnais via le
  chemin ; le contenu n'est jamais dans `argv`.
- L'enfant ne reçoit **que** cet extrait, pas la mémoire complète du parent
  (0029 : la mémoire est un dépôt neutre, un extrait est une copie bornée).
- Le fichier est refusé si le parent n'a pas le droit de le lire, s'il est
  modifié entre la lecture et le spawn (digest), ou s'il contient un secret.

### 4.4 Acceptation et témoins

- Un outil hors de la liste est refusé à l'enfant : faux harnais qui tente
  l'outil, configuration générée inspectée, `spawn-tools-unsupported` pour un
  harnais non qualifié.
- Un nom demandé hors de la liste du parent est refusé à la création ; un
  petit-enfant ne peut pas élargir.
- Un message humain envoyé directement à l'enfant apparaît dans le journal
  du parent (auteur humain, `meta.child`), sans autorité.
- Le rapport final de l'enfant parvient au parent (journal + courrier) ;
  l'expiration produit un rapport final.
- Le fichier de contexte est chargé au démarrage, son digest est vérifié, un
  secret le fait refuser, et il est supprimé avec l'enfant.
- Un enfant sans placement propre est refusé, pas replié sur le parent.

## 5. #5325 — attach distant

### 5.1 Le manque

`ameesh attach` lance le harnais **en local** et refuse un agent épinglé
ailleurs (`runner.py:16259`). `ameesh serve` est lié à `127.0.0.1`
(`serve.py:9,77`) ; un collaborateur sans shell sur le cluster ne peut donc
pas converser avec un avatar cloud. `ameesh chat` lit le journal et envoie du
courrier, mais ce n'est pas la session native : on ne peut pas y lire un
prompt interactif, y répondre en temps réel, ni utiliser les commandes du
harnais.

### 5.2 Identité et autorisation

- **Qui** : le `responsible` de l'agent, ou un humain **explicitement
  autorisé** par une liste du canon (proposée : `avatar.attach_authorized:
  [human:…]` sur la carte, ou une carte `Member` avec
  `attach_for: [agent…]`). Un agent, même de la même famille, n'est jamais
  autorisé. Un deputy (0033 §7) agit sous sa propre identité et doit figurer
  dans la liste, ou être le `responsible`.
- **Preuve** : un **grant d'attach** court (5 min), à usage unique, signé par
  le mécanisme d'identité humaine existant (`ameesh-approve`, WebAuthn) ou,
  pour un déploiement OIDC, par un jeton OIDC vérifié ; le grant lie
  `{agent, host, tenant, human, device_key, expiry, nonce}`. Le poste
  enrôlé possède une clé d'appareil (pas un mot de passe) ; le grant est
  vérifié par le relais et par l'hôte de l'agent. Un grant absent, expiré,
  rejoué ou pour un autre agent est refusé ; tout refus est journalisé.
- **Exposition** : le relais est un service public (ou exposé à l'extérieur
  du mesh) ; c'est une nouvelle surface. Décision owner (§6 Q3) :
  l'exposer, avec TLS, quota, journaux sans contenu, ou le réserver au réseau
  du mesh. Le service ne stocke ni credential ni session ; il route des
  trames.

### 5.3 Transport et confidentialité

Deux options :

- **Base** : le poste écrit des trames chiffrées dans une table, l'hôte les
  lit par `LISTEN`/`NOTIFY`. La base est déjà authentifiée et disponible,
  mais le poste d'un collaborateur n'a pas de credential base ; il faudrait
  une API publique, donc un relais de toute façon. Latence de polling,
  charge de base, et les agents partagent souvent le même rôle SQL : sans
  chiffrement de bout en bout, ils pourraient lire les trames.
- **Relais authentifié** (choix) : un petit service de relais, avec une
  connexion **sortante** de l'hôte de l'agent (pas de port entrant sur le
  poste ni sur le cloud), et une connexion du poste. Les deux extrémités
  établissent une clé de session (X25519 + AES-GCM) ; le relais ne voit que
  des trames chiffrées et des métadonnées de routage (`session_id`, tailles,
  horodatages). TLS protège le transport ; l'E2E protège le contenu même
  contre le relais. Le relais ne conserve rien après la session ; les
  journaux ne portent que l'identité, l'agent, la durée, les octets.
- **PTY** : le runner de l'hôte ouvre un pseudo-terminal pour le harnais
  interactif, transmet les octets, la taille de fenêtre, les signaux
  (`SIGINT`, `SIGWINCH`) ; le poste met son terminal en mode brut et restaure
  à la sortie. Les trames sont bornées et acquittées ; une perte de connexion
  est un échec de session, pas un gel.

### 5.4 Bail, tour et journal

- Le runner de l'hôte reçoit la demande, vérifie le grant, attend la fin du
  tour en cours (`--wait` ou refus, comme l'attach local), prend le bail
  `attach:<tenant>:<appareil>`, endort le résident à la frontière, lance le
  harnais interactif sur la **session existante** de l'agent (celle du cache
  local) et relie le PTY au relais.
- À la sortie ou à la déconnexion : le harnais est arrêté, le bail rendu, le
  runner résident reprend la session (native, sur le même hôte). Si le poste
  disparaît, le bail expire selon son TTL court ; le runner de l'hôte voit la
  déconnexion du relais et arrête le harnais ; le résident reprend.
- Un tour en cours n'est **jamais** interrompu par l'attach : la demande
  attend ou est refusée ; `!urgent` reste le seul chemin d'interruption, et
  il est déjà borné par `AMEESH_INTERRUPT_SENDERS`.
- La session est enregistrée **comme un attach local** : `lease_owner`
  `attach:…`, événement `agent.attach`, entrées du journal (A0), note dans
  le fil. Aucun fichier de session ni credential n'est envoyé au poste ; le
  terminal ne reçoit que des octets. Un copier-coller est du texte ; le
  transfert de fichiers n'est pas dans ce lot.
- Si l'humain s'attache à un **enfant**, les messages qu'il y écrit sont
  recopiés au parent (§4.2) ; le parent reste responsable.

### 5.5 Acceptation et témoins

- Depuis un poste enrôlé, `ameesh attach <avatar>` ouvre une session sur un
  agent épinglé sur un autre hôte ; le harnais tourne sur l'hôte de l'agent,
  le poste ne reçoit ni credential ni copie de session.
- La conversation reste continue avec les tours du runner cloud (journal,
  affichage de la dernière page avant le terminal).
- Un humain non autorisé est refusé (grant absent, autre humain, agent,
  grant expiré/rejoué) ; le refus est journalisé.
- Un tour en cours n'est pas interrompu : `--wait` attend, sans `--wait`
  c'est un refus ; le bail est rendu à la sortie.
- Une coupure du relais : le harnais est arrêté, le bail expire, le runner
  résident reprend ; aucun tour fantôme, aucun second porteur.
- Le journal contient les messages de l'attach (A0), y compris ceux tapés
  depuis le poste distant.

## 6. Découpage en sous-lots

Les quatre lots porteurs (#7032 à #7035) restent les porteurs des tickets ;
les sous-lots ci-dessous sont les unités de construction proposées, dans
l'ordre. Les estimations sont en heures-agent, à réestimer par le bureau ; la
classe suit le canon §1 et `ameesh review-class` pour les chemins réels.

### Phase 1 — conversation et préférence (24 h)

| Sous-lot | Contenu | Porteur | Classe | Est. | Dépend de |
|---|---|---|---|---|---|
| **A0** | Capture de l'attach dans `conversation_log` : hook `UserPromptSubmit`/`Stop`, `AMEESH_ATTACH=1`, `event_id`, non-bloquant, pas de doublon avec le `Recorder` ; témoin d'un échange en attach | #7032 | sensible (vie privée) | 4 h | — |
| **A1** | Contrat `ameesh-conversation-segment/1` : sélection par tours, budget 24 Ko, rendu Markdown, digest, repli sur le fil, tests de bornes et de non-fuite | #7032 | sensible (vie privée) | 4 h | A0 |
| **A2** | `conversation_moves`, machine d'état, claim `move`, session provisoire, segment au premier tour, échec → source, idempotence, `turn_context.conversation`, canal privé | #7032 | sensible (SQL, concurrence) | 6 h | A1 |
| **A3** | Qualification par harnais : Claude/Codex, refus DeepSeek/ACP sans interactif ; test de non-copie de session/credential ; test de reprise | #7032 | sensible (exécuteur) | 2 h | A2 |
| **B1** | Ordre d'admission conservé, `releve: preference`, `preference_rank` dans `agent_candidates`, présence (fraîcheur/porte/canon/runner), délai de rang | #7033 | sensible (concurrence) | 4 h | — |
| **B2** | Retour au poste : dwell/holdoff, libération en fin de tour, TTL court, hôte absent, rejeu du tour interrompu, deux porteurs | #7033 | sensible (concurrence) | 4 h | B1 |

### Phase 2 — hôte partagé, déplacement, enfants (21 h)

| Sous-lot | Contenu | Porteur | Classe | Est. | Dépend de |
|---|---|---|---|---|---|
| **B3** | `policy.shared`/`tenants`, superviseur d'hôte, runner par utilisateur système, `runner_presence`, claim par `responsible`, `require_isolation` par UID, provisionnement source only | #7033 | sensible (droits, comptes, infra, SQL) — **deux familles** | 8 h | B1 |
| **B4** | `agent move` pour `releve: preference`/avatar : préflight, bail, journal, partagé ↔ dédié, pas de copie | #7033 | sensible (concurrence) | 3 h | B2, B3 |
| **B5** | Placement des enfants sur hôte dédié (pod en B5b), jamais l'UID du parent, refus sinon | #7033 | sensible (droits, comptes) | 4 h | B3 |
| **C1** | Outils/MCP : sémantique `tools`/`mcp:`, sous-ensemble, contrat de descripteur, configuration par enfant, refus si non prouvé, `spawn_profiles` | #7034 | sensible (droits, exécuteur) — **deux familles** | 6 h | B5 |

### Phase 3 — compte rendu et contexte (6 h)

| Sous-lot | Contenu | Porteur | Classe | Est. | Dépend de |
|---|---|---|---|---|---|
| **C2** | Canal enfant → parent : `child report` progress/question/final, copie des messages humains, journal + fil + courrier, idempotence, rapport d'expiration | #7034 | sensible (vie privée) | 4 h | C1 |
| **C3** | `--context` : borne 16 Ko, filtre secrets, stockage, chargement 0600, digest, suppression | #7034 | sensible (vie privée) | 2 h | C1 |

### Phase 4 — attach distant (16 h)

| Sous-lot | Contenu | Porteur | Classe | Est. | Dépend de |
|---|---|---|---|---|---|
| **D1** | Grant d'attach : identité (passkey/OIDC), `responsible`/autorisé, grant court lié à l'appareil, audit, refus | #7035 | sensible (identité, droits) — **deux familles** | 4 h | — |
| **D2** | Relais : service, connexion sortante de l'hôte, PTY, trames, E2E X25519/AES-GCM, quotas, journaux sans contenu | #7035 | sensible (infra, relais) — **deux familles** | 8 h | D1 |
| **D3** | Attach distant : bail, attente de tour, reprise, journal (A0), copie enfant, coupure | #7035 | sensible (concurrence) | 4 h | D1, A1 |

**Ordre recommandé** : phase 1 (A0, A1, A2, A3, B1, B2) → phase 2 (B3, B4,
B5, C1) → phase 3 (C2, C3) → phase 4 (D1, D2, D3). Les phases 2 et 4 peuvent
se chevaucher sur des agents différents, mais B3 et D2 touchent deux
infrastructures distinctes : ne pas les mettre sur le même hôte de build.

**Total** : 67 h, soit environ 8 jours-agent. L'estimation d'origine des
quatre tickets (6 jours) est donc revue de +2 jours ; l'écart vient de
l'hôte partagé (B3) et du relais (D2), qui sont des lots d'infrastructure et
de sécurité, pas des branchements.

## 7. Décisions owner

Les tickets ne tranchent pas ces points ; ils changent le coût ou les
droits. Recommandation entre parenthèses.

1. **Q1 — Rétention de la conversation d'un avatar.** Garder les défauts de
   0033 (90 j de détail, 1 an de fil) en construisant le segment depuis le
   détail puis, au-delà, depuis le fil (recommandé) ; ou allonger le détail
   d'un avatar (1 an, ou sans limite), avec le coût de stockage et la
   sensibilité des données personnelles.
2. **Q2 — Comptes sur un hôte partagé.** Le ticket #5323 dit « son propre
   dossier de comptes » ; 0033 §3 dit que les personas serveur utilisent les
   clés de l'organisation. Sur un hôte cloud partagé, autorise-t-on les
   comptes personnels (abonnement, dossier 0700) ou seulement les clés
   d'organisation (recommandé : clés d'organisation sur le cloud, abonnement
   personnel sur le poste) ? Qui paie et qui audite ?
3. **Q3 — Attach distant.** Qui peut autoriser : le `responsible` seul, ses
   deputies, le pilote du projet, une liste de cartes `Member` (recommandé :
   `responsible` + liste explicite, le deputy sous sa propre identité) ?
   Quel mécanisme d'identité : `ameesh-approve`/WebAuthn (recommandé, déjà
   là) ou OIDC ? Et expose-t-on le relais hors du mesh (nouvelle surface,
   décision owner) ?
4. **Q4 — Pouvoirs de l'avatar.** L'avatar parle, propose et exécute ses
   tâches, mais ne signe pas et n'engage pas son humain (recommandé, 0012 et
   0029 §4) ; ou l'owner veut qu'il réponde en son nom sur certains sujets,
   ce qui demande une décision séparée et une preuve d'autorité.
5. **Q5 — Un avatar par humain ?** Un seul avatar par `responsible` et par
   canon (recommandé : le ticket parle d'« un agent permanent » ; cela
   simplifie l'unicité du bail et des comptes) ; ou plusieurs (par projet,
   par usage), avec plusieurs baux et une règle de routage des messages.
6. **Q6 — Coût et placement des enfants.** L'enfant est-il payé par le
   budget/compte du parent (recommandé), par un budget de mesh, ou par
   l'humain ? Un enfant peut-il tourner sur un hôte partagé (pod dédié) ou
   seulement sur un hôte dédié (recommandé pour la première tranche) ?
7. **Q7 — Cache natif de la source après un déplacement.** Le garder marqué
   `stale-cache` pendant 7 jours (recommandé : audit et retour arrière
   manuel), ou le supprimer dès `verified` (vie privée, disque) ? La
   suppression n'est jamais une condition du déplacement.

## 8. Alternatives écartées

- **Checkpoint natif en base** (§2.1) : formats propres à chaque harnais,
  taille, secrets et sorties d'outils dans la base, aucun chemin entre
  harnais, tests de version à chaque mise à jour. Rejeté.
- **Restaurer une sauvegarde `session_backup` sur la cible** : archive
  chiffrée, destinataires GPG, restauration dans un home de compte, pas de
  reprise si le compte/chemin diffère ; c'est un outil d'audit, pas de
  déplacement. Rejeté.
- **Un seul utilisateur Unix sur l'hôte partagé, avec `--allowedTools`** :
  ne satisfait pas l'acceptation « le runner d'un humain ne peut pas lire le
  dossier de comptes d'un autre » ; un enfant au même UID lit les
  credentials du parent. Rejeté pour la frontière ; gardé seulement comme
  garde d'erreur.
- **Attach distant par la base seule** : le poste d'un collaborateur n'a pas
  de credential base ; il faut une API/relais public de toute façon. Rejeté
  comme transport principal ; la base reste le rendez-vous et le journal.
- **Attach distant par SSH sur l'hôte** : les collaborateurs n'ont pas de
  shell sur le cluster ; exclu par le ticket. Rejeté.
- **Utiliser `ameesh chat` comme avatar** : le chat est `externe`, local, sans
  runner ni bail ; il ne peut pas être l'agent permanent disponible. Il reste
  le client de conversation sur le journal. Rejeté comme substitut.
- **Classement par l'élection existante** : disponibilité sur sept jours,
  hystérésis d'une heure, session remise à zéro ; un poste qui revient ne
  reprend pas au tour suivant. Rejeté pour les avatars ; l'élection reste
  pour les rôles de gouvernance.
- **Un pod Kubernetes par enfant dès la première tranche** : le placement
  Kubernetes de #2202 est un Job d'un tour, pas un runner durable ; un pod
  par enfant est un lot d'infrastructure (B5b), après l'hôte dédié.

## 9. Limites et non-objectifs

- **Aucune action de production** dans ce lot : pas de déploiement, pas
  d'hôte partagé réel, pas de migration, pas de secret copié.
- **Aucun nouveau pouvoir** : l'avatar n'approuve pas, ne signe pas, ne
  dépense pas ; une entrée humaine reste du contexte, jamais de l'autorité
  (0012, 0006).
- **Pas de transfert de transcript natif ni de credential** : le segment est
  du texte de conversation ; le cache natif reste sur son hôte.
- **Pas de garantie sur les journaux du fournisseur** : le modèle peut
  conserver ses propres traces ; le lot ne les contrôle pas.
- **Pas de promesse d'isolation SQL** : l'hôte partagé isole les dossiers de
  comptes par UID ; un rôle DB par tenant est un travail L155 distinct.
- **Pas de support DeepSeek/ACP pour l'avatar** tant que l'interactif et le
  canal de conversation ne sont pas qualifiés ; le lot le refuse
  explicitement au lieu de dégrader.
- **Pas de transfert de fichiers** dans l'attach distant ; le copier-coller
  est du texte.
- **Pas de décision sur les pouvoirs de l'avatar** : Q4 reste ouverte.

## 10. État

Note de conception, **brouillon** : aucun code, aucune migration, aucun
serveur, aucun secret touché. Elle attend la relecture d'une autre famille
(conversation/placement/spawn/identité : classes sensibles) et les réponses
owner du §7. Les quatre lots porteurs #7032 à #7035 restent en attente de
cette note ; les sous-lots du §6 sont à créer par mesh-design après revue.
