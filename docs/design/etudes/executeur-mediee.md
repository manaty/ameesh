---
type: Study
title: "Étude — l'exécuteur médié : un exécuteur ameesh sur un appareil sans accès à la base"
description: "Contrat de l'API d'exécuteur portée par le serveur du mesh (v2) : un backend de stockage distant qui implémente par HTTP les 61 opérations dont l'exécuteur a besoin (38 écritures), fencées côté serveur par (owner, epoch) ; enrôlement d'appareil par un humain habilité, clé P-256, jetons courts, révocation ; inactivité venue de Nexlink Compute par une porte d'hôte (fichier ou socket) ; relais de modèle au serveur pour la clé payée au token ; dépôt de travail par paquet git lié au bail, effacé en fin de bail ; lots L107–L115 parallélisables ; décision proposée."
status: proposed
tags: [v2, executeur, api, compute, appareil, bail, fencing, relais, identite, inactivite]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-10T16:00:00+02:00" }
sources:
  - { resource: "../../../src/ameesh/storage/interface.py", title: "Interface de stockage (221 opérations, 29 domaines)" }
  - { resource: "../../../src/ameesh/runner.py", title: "Exécuteur" }
  - { resource: "decisions/0012-autorite-par-ameesh-approve.md", title: "Décision 0012 (autorité par ameesh-approve)" }
  - { resource: "decisions/0014-orchestrateurs-a-tours-et-placement.md", title: "Décision 0014 (hôtes, placement, secrets)" }
  - { resource: "decisions/0028-ressources-des-hotes-et-repartition.md", title: "Décision 0028 (ressources, contre-pression)" }
  - { resource: "decisions/0029-persona-et-session.md", title: "Décision 0029 (persona, visibilité)" }
  - { resource: "decisions/0033-meshes-etanches-hebergement-et-identite.md", title: "Décision 0033 (branche, PR #15 : meshes étanches, OIDC)" }
  - { resource: "ui-integrable.md", title: "Étude UI intégrable (branche design/claude3-ui-integrable : ameesh serve, /api/v1, SSE, L84–L93)" }
  - { resource: "agent-resident.md", title: "Étude agent résident (branche design/claude3-agent-resident, L75–L83)" }
  - { resource: "nexlink:docs/specs/nexlink-compute.md", title: "Spécification Nexlink Compute, §3.2, §4.4–4.10, §5.4–5.6, lot 1c-iii" }
---

# Besoin

But du propriétaire (2026-10-10) : « une release qui permettra à ameesh
d'utiliser les ressources des appareils de mes enfants lorsqu'ils sont
inutilisés ». Voie B retenue : **l'appareil n'a aucun accès direct à la
base**. Un exécuteur ameesh tourne sur l'appareil, sous Linux, dans la VM de
Nexlink Compute (lot 1c-iii : Lima sous macOS, WSL2 sous Windows), et parle
au serveur du mesh par une API.

La spécification Compute (§3.2, point 3) l'écrit comme une **dépendance
d'ameesh** : « une API d'exécuteur médiée par le serveur du mesh (v2) —
seulement : bail, consigne, courrier de la persona, compte rendu du tour ».
NX-COMPUTE-3 attend cette API.

# L'idée directrice, validée avec trois corrections

**Validée** : la voie la plus rapide est un **backend de stockage distant**.
L'exécuteur passe déjà entièrement par `storage.of(db)` : aucun SQL ne reste
dans `runner.py`, `registry.py`, `mail.py`, `cost.py`, `fil.py` ni
`account_turn.py` (seul `self.db.ping()` et les classes d'erreurs de
`ameesh.db` en sortent). Un `RemoteStorage` qui implémente le sous-ensemble
utile, rendu par `storage.of()` quand la connexion est distante, fait tourner
l'exécuteur **sans changer sa logique**.

**Corrections :**

1. **Liste fermée, pas un tunnel.** Le serveur n'expose pas « appelle
   n'importe quelle méthode » : il tient une **table d'opérations admises**
   (61, section 1), chacune avec sa **règle de portée** (quel agent, quel
   hôte) et son statut d'écriture. Toute autre opération est refusée (le
   client lève `NotSupportedRemotely`, le serveur répond 404).
2. **Le fencing est refait au serveur, pour toutes les écritures.** Une
   douzaine d'écritures de l'interface ne portent pas (owner, epoch)
   (`agents.set_status`, `set_session`, `set_pending_prompt`,
   `mark_event_wake`, `pending_spend.*`, `turn_resources.*`, `threads.index`…).
   En v1 l'exécuteur est de confiance ; un appareil tiers ne l'est pas. Chaque
   écriture porte donc une **enveloppe de bail**, recontrôlée par le serveur
   dans la même transaction que l'opération (section 2.3).
3. **Quatre fonctions de l'exécuteur passent au serveur** : la
   synchronisation du canon (et avec elle la visibilité 0029 et le
   placement), le relevé des soldes des fournisseurs, l'échéance des
   délégations et le déplacement entre hôtes (L31). L'exécuteur médié ne les
   lance pas (`mode = "mediated"`) ; les limites de l'hôte, lues aujourd'hui
   dans le canon local (`resources.host_limits`), lui sont rendues par le
   serveur. C'est le seul changement de l'exécuteur, une dizaine de gardes.

# 1. Le sous-ensemble exact

## Méthode

Graphe d'appels au niveau des fonctions (script `ast` sur `src/ameesh`), en
partant de toutes les fonctions de `runner.py`, puis des commandes que la
session du harnais lance **dans la VM** (`agent-mail hook <harnais>`,
`ameesh mail send|inbox|whoami`, `ameesh work move|note`,
`ameesh action propose`). Sont exclus les chemins qui passent au serveur
(correction 3 : `canon_sync`, `relocation`, `balance`, `work.expire_delegations`,
`migrations`) et ceux qu'un exécuteur d'appareil n'emprunte pas
(`run_attach`, `main` qui déclare des agents, `reprise`, `exploitation`,
`stagnation`, `receipts`). Résultat recoupé à la main dans `runner.py`.

Sur **221 opérations** de l'interface (29 domaines), l'exécuteur médié en
utilise **61** : **23 lectures** et **38 écritures**. 52 viennent de
l'exécuteur, 9 de la session.

## Table (contrat figé par L107)

Colonne « portée » : règle appliquée par le serveur. **A** = l'agent nommé
est admis sur l'hôte de l'appareil ; **B** = A, et l'appareil détient le bail
vivant de cet agent, avec l'(owner, epoch) de l'enveloppe ; **H** = l'hôte
forcé à celui de l'appareil ; **S** = jeton de session (agent et epoch tirés
du jeton, pas du corps) ; **∅** = donnée agrégée, sans nom d'autrui.

### Exécuteur (52)

| Opération | É | Portée | Remarque |
|---|---|---|---|
| `agents.claimable(host, names, *, require_responsible)` | | H | `host` ignoré, remplacé ; `names` ∩ agents admis |
| `agents.get(name)` | | A | |
| `agents.cwd_used(cwd, exclude)` | | A | réponse limitée aux agents de l'hôte |
| `agents.harnesses()` | | H | rendu filtré : agents de l'hôte seulement |
| `agents.set_status(name, status, status_text, error, stop_reason)` | ✓ | B | |
| `agents.set_session(name, session_id, account)` | ✓ | B | |
| `agents.set_session_account(name, account)` | ✓ | B | |
| `agents.set_pending_prompt(name, prompt)` | ✓ | B | |
| `agents.mark_event_wake(name)` | ✓ | B | |
| `agents.upsert(name, …)` | ✓ | B | **seul champ admis : `cwd`** (déplacement du dossier, runner l. 1128) ; création refusée |
| `leases.claim(name, owner, ttl_seconds, *, require_responsible)` | ✓ | A, H | `owner` doit commencer par `exec:<id>:` ; `ttl` borné par l'hôte |
| `leases.renew(name, owner, epoch, ttl_seconds)` | ✓ | B | |
| `leases.release(name, owner, epoch)` | ✓ | B | déclenche l'effacement du dossier (section 6) |
| `leases.reap(host)` | ✓ | H | sans effet sur un autre hôte ; le serveur fait aussi sa passe |
| `leases.begin_turn(name, owner, epoch, status_text)` | ✓ | B | |
| `leases.end_turn(name, owner, epoch, *, status, status_text, error, cost_usd)` | ✓ | B | `cost_usd` de l'appareil : indicatif (section 5) |
| `leases.take_pending_prompt(name, owner, epoch)` | ✓ | B | |
| `leases.restore_prompt(name, owner, epoch)` | ✓ | B | |
| `leases.pause(name, owner, epoch, status_text)` | ✓ | B | |
| `leases.clear_session(name, owner, epoch)` | ✓ | B | |
| `leases.set_marked_block(name, owner, epoch, …)` | ✓ | B | |
| `leases.clear_marked_block(name, owner, epoch, …)` | ✓ | B | |
| `mailbox.unread(recipient, limit)` | | A | |
| `mailbox.unread_urgent(recipient, limit)` | | A | |
| `mailbox.get(message_id)` | | A | le destinataire du message doit être un agent de l'hôte |
| `mailbox.reserve(recipient, owner, epoch, token, *, ids, porteur, ttl_seconds, limit)` | ✓ | B | |
| `mailbox.deliver(recipient, owner, epoch, token, ids)` | ✓ | B | |
| `mailbox.release(recipient, token, ids)` | ✓ | B | |
| `pending_spend.put / set_model / clear (name, …)` | ✓ ×3 | B | marqueur fail-closed (0019) |
| `pending_spend.get(name)` | | A | |
| `turn_costs.insert(*, agent, …)` | ✓ | B | **rangé `source=device`, hors plafond** ; le relais fait foi (section 5) |
| `turn_costs.last_reading(agent, harness, session)` | | A | |
| `turn_costs.spent(seconds, *, agent, harnesses, account)` | | A, ∅ | un total, rien d'autre |
| `budgets.limits()` | | ∅ | plafonds du mesh |
| `accounts.active(host, harness)` | | H | |
| `operations.balances(*, provider, since_s, account)` | | ∅ | soldes, sans clé |
| `operations.apply_restart(name, owner, epoch)` | ✓ | B | |
| `operations.message_lots(ids)` | | A | messages de l'agent seulement |
| `operations.set_session_work_item(name, owner, epoch, work_item)` | ✓ | B | |
| `operations.record_gauges(readings)` | ✓ | H | jauges des comptes de l'hôte |
| `turn_resources.open_turn(turn_id, agent, host, *, …)` | ✓ | B, H | |
| `turn_resources.close_turn(turn_id, *, orphan, containers)` | ✓ | B | le tour doit appartenir à un agent de l'hôte |
| `hosts.record(reading)` | ✓ | H | auto-déclaré : sert l'ordonnancement, jamais une preuve |
| `hosts.latest(host)` | | H | |
| `hosts.current(host)` | | H | |
| `hosts.turns_in_progress(host)` | | H | |
| `keys.info(agent)` | | ∅ | clé publique d'un expéditeur signé (vérifier un urgent) |
| `threads.index(*, project, lot, …)` | ✓ | B | entrée de fil (`fil.record`) ; l'auteur doit être l'agent du bail |
| `work.mark_delegate_turn(agent, item_ids, note)` | ✓ | B | lots assignés ou délégués à l'agent |
| `wakeups.subscribe(channels)` | | H | devient le flux d'événements (section 2.5) |

### Session du harnais dans la VM (9 de plus)

| Opération | É | Portée | Remarque |
|---|---|---|---|
| `leases.state(name)` | | S | identité liée au bail (`identity.py`) |
| `agents.overview()` | | S, ∅ | annuaire réduit : nom, rôle, statut ; pas de consigne ni de dossier |
| `mailbox.send(sender, recipient, body, *, …)` | ✓ | S | `sender` = agent du jeton ; **le destinataire doit exister** (pas de création implicite, `backend.py` l. 256 neutralisé) |
| `mailbox.mark_delivered(ids)` | ✓ | S | messages de l'agent du jeton |
| `work.get(item_id)` | | S | lot du projet de l'agent |
| `work.move(item_id, state, *, current, loops, note, actor)` | ✓ | S | lot assigné à l'agent ; `actor` forcé |
| `work.note(item_id, state, text, actor)` | ✓ | S | idem |
| `actions.get(action_id, *, with_receipts)` | | S | `with_receipts` forcé à faux |
| `actions.propose(*, …)` | ✓ | S | **proposer seulement** ; `execute` reste au serveur (Compute §3.2.6) |

### Refusées, à dessein

Tout le reste, et notamment : `approvals.*`, `nonces.*`, `grants.*`,
`authenticators.*`, `keys.register/revoke`, `actions.bind/launch/settle`
(jamais d'approbation ni d'exécution d'action depuis l'appareil, 0012) ;
`canon.*`, `packages.*`, `placements.*`, `visibility.*` (le canon n'est jamais
lu ni écrit par l'appareil) ; `agents.upsert_unleased`, `ephemerals.create`
(pas de création d'agent) ; `session_bindings.*` (une session de la VM est
toujours lancée par l'exécuteur, donc liée au bail : la recherche de liaison
rend vide) ; `progress.*`, `projects.*`, `operations.listing` (vues des
humains, par `/api/v1`).

# 2. L'API

## 2.1 Où elle vit

Dans **`ameesh serve`** (étude UI, L84), sur le serveur du mesh, sous un
**préfixe distinct** : `/api/exec/v1`. Même pile (serveur stdlib
d'ameesh-approve et ses défenses factorisées), **autre authentification** :
jamais de jeton OIDC humain sur `/api/exec`, jamais de jeton d'exécuteur sur
`/api/v1`. Rôle Postgres propre (`ameesh_exec`, données oui, schéma non), qui
n'a pas `SELECT` sur les tables d'autorité. Tant que L84 n'est pas fusionné,
L108 démarre le même module en `ameesh serve --exec-only`.

TLS obligatoire (terminé par le serveur ou un mandataire inverse), aucun port
entrant sur l'appareil : tout part de la VM.

## 2.2 Routes

| Route | Authentification | Rôle |
|---|---|---|
| `POST /api/exec/v1/enroll` | code d'enrôlement + preuve de clé | enrôler l'exécuteur (section 3) |
| `POST /api/exec/v1/token` | assertion ES256 de l'exécuteur | jeton d'accès (10 min) |
| `GET  /api/exec/v1/host` | jeton d'exécuteur | fiche de l'hôte : nom, limites (`max_agents`, seuils 0028), `lease_ttl` imposé, harnais et modèles admis, agents admis |
| `PUT  /api/exec/v1/host/availability` | jeton d'exécuteur | état d'inactivité (section 4) |
| `POST /api/exec/v1/op` | jeton d'exécuteur | une opération de la table, section 1 |
| `POST /api/exec/v1/session-token` | jeton d'exécuteur + enveloppe de bail | jeton de session pour un tour (section 3.4) |
| `POST /api/exec/v1/session/op` | jeton de session | une opération « session » de la table |
| `GET  /api/exec/v1/events` | jeton d'exécuteur | flux de réveil, SSE ou attente longue |
| `GET  /api/exec/v1/work/{agent}/bundle` | jeton d'exécuteur + bail | paquet git d'entrée (section 6) |
| `POST /api/exec/v1/work/{agent}/bundle` | jeton d'exécuteur + bail | paquet git de sortie |
| `POST /api/exec/v1/llm/{provider}/…` | jeton de session | relais de modèle (section 5) |
| `GET  /api/exec/v1/health` | aucune | version du contrat, heure du serveur |

Une seule route `op` plutôt qu'une route par opération : le contrat **est**
la table de la section 1, versée en JSON (`docs/api/executor-v1.json`, L107) ;
un seul répartiteur et une fonction de portée par opération. Les routes
humaines (`/api/v1/executors…`, section 3) appartiennent à `/api/v1`.

## 2.3 Format d'une opération

Requête :

```json
POST /api/exec/v1/op
Authorization: Bearer <jeton d'exécuteur>
Idempotency-Key: 4c1d…            (obligatoire pour une écriture)
{
  "schema": "ameesh-exec-op/1",
  "op": "leases.begin_turn",
  "args": ["inge-front", "exec:7f3a:host-anna:4121", 42, "tour en cours"],
  "kwargs": {},
  "fence": {"agent": "inge-front", "owner": "exec:7f3a:host-anna:4121", "epoch": 42}
}
```

Réponse :

```json
200 {"schema": "ameesh-exec-result/1", "ok": true, "value": true, "server_ts": 1791640000.12}
```

* **Valeurs** : celles de l'interface, telles quelles (instants `*_ts` en
  secondes epoch flottantes, JSON en objets, `Sequence` en listes, `None` en
  `null`). Un **refus métier reste une valeur** (`None`, `false`, liste
  vide) : l'exécuteur le traite déjà ainsi.
* **`fence`** : obligatoire pour toute opération de portée B. Le serveur
  ouvre une transaction, prend `SELECT … FROM agent_registry WHERE name = %s
  FOR UPDATE`, recontrôle `lease_owner = owner`, `lease_epoch = epoch`,
  `lease_expires_at > clock_timestamp()`, `host = hôte de l'exécuteur` et
  `owner LIKE 'exec:<id de l'exécuteur>:%'`, puis appelle l'opération du
  pilote Postgres **dans la même transaction**. Un bail perdu rend la
  **valeur de refus** de l'opération (comme aujourd'hui) avec
  `"fenced": true`, pas une erreur : la logique de bail perdu de l'exécuteur
  reste la seule.
* **Owner** : `exec:<executor_id>:<hôte>:<pid>`. Le serveur rejette un
  `claim` dont l'owner n'est pas préfixé par l'identifiant authentifié : un
  appareil ne peut pas se faire passer pour un autre exécuteur.

## 2.4 Idempotence

Toute écriture porte `Idempotency-Key` (UUID tiré par le client **une fois
par appel logique**, réutilisé à chaque nouvel essai). Table
`exec_idempotency (executor_id, key, op, request_sha256, response, at)`,
écrite dans la transaction de l'opération ; même clé et même requête →
réponse rejouée ; même clé, autre requête → `409 idempotency_mismatch`.
Conservation 24 h. Cela couvre la réponse perdue après un `claim`, un
`mailbox.send`, un `work.note` ou un `turn_costs.insert`.

## 2.5 Erreurs

| HTTP | `error` | Côté client (`ameesh.db`) |
|---|---|---|
| 400 | `bad_request`, `bad_args` | `DbError` |
| 401 | `token_expired`, `token_invalid` | nouveau jeton, puis un essai ; sinon `DbError` |
| 403 | `forbidden_scope` (agent ou hôte hors portée), `executor_revoked`, `host_unavailable` | `Forbidden(DbError)` ; `executor_revoked` arrête l'exécuteur |
| 404 | `op_not_allowed` | `NotSupportedRemotely(DbError)` |
| 409 | `idempotency_mismatch` | `DbError` |
| 413 | `too_large` | `DbError` |
| 429 | `rate_limited` (avec `Retry-After`) | `Unavailable` |
| 5xx, coupure, délai | — | `Unavailable` : les reprises L72 de l'exécuteur s'appliquent telles quelles |

Corps d'erreur : `{"schema": "ameesh-exec-error/1", "error": "…", "message": "…"}`, sans donnée d'autrui.

## 2.6 Réveil : le flux d'événements

`wakeups.subscribe([agent_mail, agent_lease, ameesh_budget])` devient
`GET /api/exec/v1/events`.

* **SSE** (lu par `http.client`, en-tête `Authorization`) ; repli **attente
  longue** : `GET /api/exec/v1/events?wait=25&after=<id>` rend la liste des
  événements ou `[]` après 25 s.
* Le serveur tient **une** connexion `LISTEN` sur les trois canaux. Il ne
  transmet un `agent_mail` ou un `agent_lease` que si `to` (ou `name`) est un
  agent admis sur l'hôte de l'appareil ; `ameesh_budget` passe à tous.
  `data` = le payload NOTIFY d'origine (`{"to", "id", "restart"}`), donc le
  `dispatch` de l'exécuteur ne change pas.
* `id:` croissant (tampon circulaire de 1 000 événements par processus),
  reprise par `Last-Event-ID` ; battement `: ping` toutes les 20 s.
* **Le réveil ne porte jamais de donnée** et peut se perdre : l'exécuteur
  relit toujours en base, et son sondage (`poll`) reste le filet. Aucune
  dépendance au journal `ameesh_events` de L85 ; quand il existera, la même
  route pourra s'y adosser sans changer le contrat.
* Côté client, `RemoteSubscription.wait(timeout)` lit une file alimentée par
  un fil de lecture SSE ; `close()` coupe la connexion.

# 3. Identité et autorisations de l'appareil

## 3.1 La clé

L'exécuteur a **sa** clé **P-256** (ES256), générée dans la VM à
l'enrôlement, privée en `0600` dans le volume persistant de la VM
(`/var/lib/ameesh-exec/key.pem`). Raisons : la clé d'appareil Nexlink est
scellée **sur l'hôte**, hors de la VM, et le hub ne la détient pas ; ameesh ne
dépend pas de Nexlink (0003, 0015). On admet ce que dit Compute §3.2 :
**le propriétaire de l'appareil peut lire cette clé**. Elle prouve **quel
appareil** parle, pas son honnêteté ; la garde réelle est la portée
serveur, le fencing et la relecture humaine.

**Liaison facultative à la clé d'appareil Nexlink** : si l'application de
bureau sait signer un défi avec la clé P-256 de l'appareil, l'enrôlement
joint `device_attestation` (la clé publique de l'exécuteur et le code,
signés par la clé d'appareil). Le serveur l'enregistre (même appareil que
l'offre Compute) ; ce n'est pas une condition.

Signature côté appareil : extra `cryptography` dans l'image de la VM.
Vérification côté serveur : `ameesh.p256.verify`, déjà présent.

## 3.2 Enrôlement par un humain habilité

**Habilité** : le responsable de la fiche `Host` visée, ou un humain de rôle
coordinateur du mesh (0033 §8), ou un suppléant (0033 §7). Jamais un agent.

1. L'humain crée une invitation :
   `ameesh executor invite --host anna-portable [--agents a,b] [--ttl 15m]`
   sur le serveur (mode local), ou `POST /api/v1/executors/invitations`
   (OIDC, L86). La fiche `Host` doit exister au canon fusionné. Réponse : un
   **code** à usage unique (128 bits, affiché en 6 groupes), valable 15 min,
   lié au mesh, à l'hôte et à la liste facultative d'agents.
2. Dans la VM : `ameesh executor enroll --server https://mesh.exemple --code …`
   génère la clé et envoie :

   ```json
   POST /api/exec/v1/enroll
   {"schema": "ameesh-exec-enroll/1", "code": "…",
    "public_key": {"kty": "EC", "crv": "P-256", "x": "…", "y": "…"},
    "proof": "<ES256 sur JCS({code, public_key, server_url})>",
    "device_attestation": null,
    "label": "portable d'Anna (VM Compute)"}
   ```
3. Le serveur vérifie le code (non consommé, non échu), la preuve de
   possession, consomme le code, crée la ligne `executors (id, host,
   public_key, thumbprint, agents_allowlist, enrolled_by, enrolled_at,
   revoked_at, revoked_by, last_seen_at)` et rend
   `{"executor_id", "mesh", "host", "server_time"}`. Journal :
   « exécuteur 7f3a enrôlé pour anna-portable par human:… ».

## 3.3 Mesh, hôte, agents admis

* **Mesh** : un exécuteur appartient à **un** mesh (0033 §1). Deux
  organisations sur un appareil = deux enrôlements, deux configurations, deux
  clés, deux exécuteurs (Compute §3.2.5).
* **Hôte** : fixé à l'enrôlement ; tout paramètre `host` est remplacé par
  celui-ci.
* **Agents admis** : ceux que le **canon** place sur cet hôte (admission,
  politique d'hôte, visibilité 0029, déjà calculées par la synchronisation
  du canon au serveur : le prédicat de `claimable` les applique), **∩**
  `agents_allowlist` si l'invitation l'a fixée, **∩** agents dont le mode
  d'identifiants est compatible avec le relais (section 5). Pour un appareil
  tiers, la fiche `Host` porte `credential_modes: [relay]`.
* **Jetons** : l'exécuteur obtient un jeton d'accès par
  `POST /api/exec/v1/token` avec une assertion ES256 (`iss` = id,
  `aud` = URL du serveur, `iat`, `exp` ≤ 60 s, `jti` à usage unique) ; jeton
  opaque de 10 min, haché en base. Chaque requête relit l'état de
  l'exécuteur (cache ≤ 5 s).

## 3.4 Jeton de session

Le harnais lancé dans la VM (et ses hooks `agent-mail`) a besoin de parler au
serveur **en tant que l'agent**. Après `begin_turn`, l'exécuteur demande
`POST /api/exec/v1/session-token` avec l'enveloppe de bail ; le jeton rendu
porte (exécuteur, agent, epoch), expire avec le bail et meurt dès que
l'epoch change. Il est passé au harnais par `AMEESH_EXEC_TOKEN` (avec
`AMEESH_EXEC_URL`). Il ouvre `session/op` et le relais, rien d'autre.

## 3.5 Révocation

* `ameesh executor revoke <id> --why …` ou
  `POST /api/v1/executors/{id}/revoke` (humain habilité, ou le propriétaire
  de l'appareil lui-même s'il est membre). Effets dans la transaction :
  `revoked_at`, jetons d'accès et de session invalidés, **baux de
  l'exécuteur relâchés** (le courrier réservé est re-livré, signalé comme
  doublon, comme à l'échéance), relais refusé.
* Côté appareil, la révocation Nexlink (offre retirée) arrête la VM ; ameesh
  voit le bail échoir. **Double révocation** (Compute §3.2.1) : retirer aussi
  la fiche `Host` ou l'admission au canon, sinon l'hôte reste admis pour
  un prochain enrôlement.
* `ameesh executor list` : id, hôte, enrôlé par, dernier contact, état.

## 3.6 Jamais d'approbation

* Aucune route de `/api/exec` n'atteint `approvals`, `nonces`, `grants`,
  `authenticators`, `actions.bind|launch|settle` (section 1, « refusées »).
* Le rôle Postgres `ameesh_exec` n'a aucun droit sur ces tables : défense en
  profondeur si un répartiteur se trompait.
* Les personas placées sur un appareil tiers n'ont ni `approve` (0012) ni
  identifiants métier ; `actions.propose` est la seule voie vers un effet
  extérieur, exécuté au serveur après reçu humain.
* Test de L108 : pour chaque opération de la table, un jeton d'exécuteur ne
  peut ni créer ni consommer un reçu.

# 4. État d'inactivité venu de Compute

## 4.1 La porte d'hôte (`HostGate`), interface figée par L107

```python
class HostGate(Protocol):
    def state(self) -> GateState: ...                   # état courant, sans attente
    def wait_change(self, timeout: float) -> GateState: ...
    def acknowledge(self, ack: GateAck) -> None: ...    # ce que l'exécuteur a fait

@dataclass(frozen=True)
class GateState:
    state: Literal["available", "draining", "stopped"]
    seq: int                       # croissant, écrit par le runner Compute
    until_ts: float | None         # fin de fenêtre connue
    drain_deadline_ts: float | None
    caps: dict                     # max_concurrent, cpu_share, memory_mb (profil §4.10)
    reason: str                    # "idle", "user_active", "battery", "window_end", "revoked"
```

Implémentations : `AlwaysAvailable` (défaut, comportement actuel),
`FileGate` et `SocketGate`.

## 4.2 Le canal avec le runner Compute

* **Fichier** : le runner Compute écrit `/run/ameesh-gate/state.json`
  (schéma `ameesh-host-state/1`, champs de `GateState`) par écriture
  atomique (fichier temporaire puis `rename`) dans un dossier partagé de la
  VM (montage Lima, `/mnt/wsl` ou `wsl.exe -e` côté Windows). L'exécuteur le
  relit sur `inotify`, sinon toutes les 2 s. L'exécuteur répond dans
  `/run/ameesh-gate/ack.json` (`ameesh-host-ack/1` : `seq` traité, agents
  encore en tour, `drained: bool`).
* **Socket** : `/run/ameesh-gate/gate.sock`, JSON Lines, mêmes messages,
  le runner Compute pousse, l'exécuteur acquitte.
* Les deux portent les mêmes schémas ; L112 livre les deux, Compute choisit.
  Ce canal est le même que l'état local `ameesh-device/1` de L93 (« mon
  poste ») : l'acquittement y est inclus.

## 4.3 Comportement

| Transition | Exécuteur | Serveur |
|---|---|---|
| → `available` | réclame à nouveau, dans la limite `min(caps.max_concurrent, max_agents)` | `PUT /host/availability {available: true, until_ts, caps}` : l'hôte devient **admissible** ; `ameesh hosts` le montre |
| → `draining` (l'humain reprend la machine, batterie, fin de fenêtre) | **aucune nouvelle réclamation** ; chaque worker termine son tour au **point sûr** (fin du tour en cours, 0028 : jamais au milieu) ; au-delà de `drain_deadline_ts` (défaut : 90 s), interruption du tour comme une préemption (`request_preempt` : consigne remise en attente, comptabilité close) ; puis `release` de chaque bail, effacement des dossiers, `ack drained=true` | `available: false` : `claimable` et `claim` refusent cet hôte (`host_unavailable`), même si l'appareil insistait |
| → `stopped` | arrêt immédiat, sans écriture | le bail échoit ; l'agent passe mort, courrier re-livré et signalé |

Bail : pour un hôte volatil, la fiche `Host` impose un `lease_ttl` plus
court (proposé : **90 s**, renouvelé toutes les 30 s), rendu par
`GET /host` ; le bail Compute qui héberge la VM doit rester **au moins aussi
long** (Compute §4.4).

Réglage côté exécuteur : un `max_concurrent` de 1 par défaut sur un appareil
tiers ; les seuils 0028 continuent de s'appliquer dans la VM.

# 5. Secrets : relais de modèle au serveur

**Choix : le relais de modèle sur le serveur du mesh.** L'appareil appelle
`/api/exec/v1/llm/deepseek/…` avec son **jeton de session** ; le serveur
ajoute la clé et appelle DeepSeek.

| Option | Pour | Contre |
|---|---|---|
| **Relais au serveur** (retenue) | la clé ne quitte jamais le serveur ; le serveur **voit les jetons consommés** : le coût et le plafond deviennent une mesure, pas une déclaration ; refus fail-closed au-delà du plafond ; liste des modèles admis appliquée ; l'egress de la VM se réduit à **un seul nom** (le serveur du mesh) | une charge et une latence au serveur (flux relayé, faible : quelques flux simultanés) ; un point de panne de plus (déjà vrai : sans serveur, pas de bail) |
| Clé à durée limitée | aucun relais | DeepSeek n'émet pas de clé à portée ni à durée limitées : une « clé temporaire » serait une vraie clé, révocable à la main seulement, lisible par le propriétaire de l'appareil pendant sa vie ; coût toujours auto-déclaré |
| Le tiers apporte son compte (Compute option a) | rien à relayer | hors sujet ici : l'appareil d'un enfant n'a pas de compte de fournisseur ; reste ouvert pour d'autres tiers |

Contrat du relais (L111) :

* **Passage OpenAI-compatible** : `POST /api/exec/v1/llm/deepseek/v1/chat/completions`
  (et `/v1/models`), corps transmis tel quel, à trois exceptions : `model`
  doit être admis (politique de l'hôte ∩ persona), `max_tokens` plafonné,
  `stream_options.include_usage` forcé pour lire l'usage en flux.
* **Coût** : à la fin de chaque réponse, le relais écrit `turn_costs`
  (`source = relay`, agent et tour tirés du jeton de session, plus un
  en-tête `X-Ameesh-Turn` facultatif) : c'est la ligne qui compte au plafond
  et au CostBook. La ligne `turn_costs.insert` de l'appareil est rangée
  `source = device` et ne compte pas (migration : colonne `source`).
* **Plafond** : avant chaque requête, le relais recontrôle
  `budget_usd_per_hour` du mesh et `budget_usd_per_day` de la persona ; au-delà,
  `402 budget_exceeded` (le harnais échoue, l'exécuteur range le tour en
  « plafond atteint » comme aujourd'hui, L49).
* **Harnais** : `dsh` doit accepter une URL de base et une clé fournies par
  l'environnement (`DEEPSEEK_API_KEY` = jeton de session,
  `DEEPSEEK_BASE_URL` = relais). **À vérifier en premier par L111** ; à
  défaut, un fournisseur « openai-compatible » dans le patch de profil `dsh`.
* La clé du serveur reste où elle est (trousseau du serveur, 0014) ; elle
  n'apparaît dans aucune réponse ni aucun journal.

Les harnais au forfait (Claude, Codex) restent **hors** des appareils tiers :
leurs abonnements restent sur les appareils de leurs titulaires (0033 §3).

# 6. Données et visibilité (0029)

* **Visibilité** : un humain qui a l'accès physique à la machine peut tout
  lire dans la VM. La règle de 0029 s'applique donc aux enfants **comme
  responsables de fait** : la fiche `Host` de l'appareil déclare
  `responsible` (le parent, qui consent et administre) **et** les humains qui
  ont l'accès physique (`admins`, ou un champ `occupants` à ajouter, question
  1). La synchronisation du canon au serveur n'admet une persona sur cet
  hôte que si **tous** ont accès à son dépôt de mémoire **et** au dépôt de
  travail du lot. Ne partent sur ces appareils que des lots **montrables**,
  sans secret.
* **Mémoire de persona** : non clonée par défaut (Compute §3.2.7).
* **Dépôt de travail, entrée** : à la prise du bail, l'exécuteur demande
  `GET /work/{agent}/bundle` ; le serveur du mesh prépare un **paquet git**
  (`git bundle`, historique court, commit épinglé de la branche du lot), selon
  le patron du hub Compute 1d-i : URL signée **liée au bail**, taille et
  `sha256` annoncés et vérifiés, **aucun identifiant de forge sur
  l'appareil**. Le paquet est déplié dans
  `/var/lib/ameesh-exec/work/<agent>/<epoch>/`, qui devient le `cwd` de la
  session. Si le hub Compute héberge les octets, le serveur y dépose le
  paquet : le contrat ne change pas.
* **Sortie** : en fin de tour, s'il y a de nouveaux commits, l'exécuteur
  envoie un paquet git des commits ajoutés (`POST /work/{agent}/bundle`,
  bail). Le serveur vérifie (descend du commit d'entrée, taille bornée,
  aucune réécriture) et pousse **avec ses propres identifiants** sur une
  branche de l'agent (`ameesh/<persona>/<lot>`), jamais sur une branche
  protégée ; la revue habituelle s'applique avant toute fusion.
* **Effacement** : à `leases.release` (ou à la révocation, ou au drainage),
  l'exécuteur supprime le dossier de l'epoch **et** la session du harnais
  (`DSH_HOME/sessions`), puis le journalise. La session n'est pas portable :
  l'agent reprendra ailleurs sur un résumé (0018). Un appareil coupé
  brutalement garde ses fichiers jusqu'au prochain démarrage de
  l'exécuteur, qui efface tout dossier dont il ne détient plus le bail.
  Le runner Compute peut en plus détruire la VM (patron 1c-iii).

# 7. Lots L107–L115

Tailles : **S** ≤ 1 jour, **M** 2–3 jours, **L** 4–5 jours (agent seul).

| Lot | Contenu | Dépend de | Taille | Côté Nexlink |
|---|---|---|---|---|
| **L107 — contrat figé** | `docs/api/executor-v1.json` : table des 61 opérations (nom, arguments, retour, écriture, portée) ; `src/ameesh/executor_api/contract.py` : enveloppes, codes d'erreur, `GateState`/`HostGate`, schémas `ameesh-exec-*/1` ; jeux d'essai JSON dorés (requête et réponse par opération), partagés par serveur et client ; fixtures de jetons | cette étude | S | — |
| **L108 — serveur d'exécuteur** | `/api/exec/v1` dans `ameesh serve` (ou `--exec-only`) : répartiteur `op` et `session/op`, une fonction de portée par opération, fencing transactionnel, `exec_idempotency`, rôle `ameesh_exec`, flux `events` (SSE et attente longue) filtré par hôte, `GET /host`, `PUT /host/availability` et garde de `claim` ; vérification de jeton derrière `ExecutorAuth.verify(token) -> Principal` (bouchon jusqu'à L110) ; tests : chaque opération hors portée refusée, bail perdu → valeur de refus | L107 | L | — |
| **L109 — client de stockage distant** | `storage/remote/` : `RemoteDb` (`name`, `ping`), `RemoteStorage` (61 opérations, le reste lève `NotSupportedRemotely`), `storage.of()` qui choisit selon la connexion ; `RemoteSubscription` (SSE, repli attente longue) ; clés d'idempotence, reprises, correspondance des erreurs ; mode `mediated` de l'exécuteur (pas de sync du canon, de solde, de déplacement, d'échéance de délégation ; limites lues par `GET /host`) ; `agent-mail` et `ameesh mail/work/action` par `AMEESH_EXEC_TOKEN` ; essais sur un faux serveur nourri des jeux dorés | L107 | L | — |
| **L110 — enrôlement et identité** | migrations `executors`, `executor_invitations`, `executor_tokens` ; `ameesh executor invite|enroll|list|revoke` ; `POST /enroll`, `/token`, `/session-token` ; assertion ES256 (`cryptography` côté appareil, `p256.verify` côté serveur) ; révocation qui relâche les baux ; routes humaines `/api/v1/executors…` derrière L86 (CLI locale du serveur en attendant) ; liaison facultative `device_attestation` | L107 | M | signature d'un défi par la clé d'appareil (facultatif) |
| **L111 — relais de modèle** | `/api/exec/v1/llm/deepseek/…` en flux, usage relevé, `turn_costs.source`, plafonds fail-closed, modèles admis ; vérification de `dsh` avec URL de base et clé d'environnement ; profil `dsh` « relais » | L107 ; monté dans L108 (module séparé, aucun fichier commun) | M | — |
| **L112 — inactivité et hôte Compute** | `FileGate`, `SocketGate`, `AlwaysAvailable` ; garde de réclamation, drainage au point sûr, préemption à l'échéance, acquittement ; rapport de disponibilité ; `lease_ttl` d'hôte volatil ; champ `occupants` (si retenu) et `credential_modes: [relay]` au profil du canon ; état `ameesh-device/1` partagé avec L93 | L107 | M | écrire l'état (fichier ou socket), attendre l'acquittement avant d'arrêter la VM |
| **L113 — dépôt de travail** | paquet git d'entrée et de sortie (serveur et exécuteur), URL liée au bail, vérifications, poussée sur la branche de l'agent, effacement à la fin du bail et au démarrage | L108, L109 | M | hébergement des octets par le hub (facultatif) |
| **L114 — empaquetage dans la VM Compute** | image Lima et distribution WSL2 : Python, roue ameesh, `dsh`, `git`, `cryptography` ; unité systemd `ameesh-executor` ; configuration (`AMEESH_EXEC_URL`, mesh, porte) ; volume persistant pour la clé ; egress = le seul serveur du mesh ; guide d'enrôlement | L109, L110, L112 | M | PROV 1c-iii : modèle de VM, volume persistant, entrée d'allow-list, lancement du service |
| **L115 — essai de bout en bout** | un mesh d'essai (base jetable) et une VM Lima ou qemu : enrôlement, réclamation, tour par le relais, courrier, fil, proposition d'action, paquet de sortie ; reprise de la machine → drainage ; VM tuée → bail échu, courrier re-livré ; révocation ; tentatives interdites (autre agent, autre hôte, approbation, canon, création d'agent) ; mesure de latence par tour | L108–L114 | M | une machine d'essai Compute (Mac ou Windows) |

**Parallélisme.** Après L107 (une demi-journée, à faire d'abord), quatre
agents en même temps, sans fichier commun :

* agent 1 : **L108**, puis L113 (côté serveur) ;
* agent 2 : **L109**, puis L113 (côté exécuteur) ;
* agent 3 : **L110**, puis L114 ;
* agent 4 : **L111** puis **L112**.

Interfaces figées entre eux : la table et les jeux dorés (L107) entre L108 et
L109 ; `ExecutorAuth.verify` et le format des jetons entre L110 et
L108/L111 ; `HostGate` et `ameesh-host-state/1` entre L112, L109 et
Compute. L115 ferme la release. Chemin critique : L107 → L108 → L113 → L115.

**Ce qui dépend de Nexlink** : l'écriture de l'état d'inactivité (L112), la
PROV de la VM (L114), une machine d'essai (L115), la liaison de clé
(facultative, L110). Rien de cela ne bloque L107–L111 ni la démonstration sur
une VM Linux simple, sans Compute.

# 8. Questions ouvertes

1. **Accès physique et visibilité** : nouveau champ `occupants` dans la fiche
   `Host`, ou réutiliser `admins` ? Recommandation : `occupants`, car un
   enfant n'administre pas l'hôte.
2. **Consentement d'un mineur** : le consentement Compute est donné par le
   titulaire de l'appareil ; pour un enfant, le parent consent et l'enfant
   voit l'écran « mon poste ». À confirmer par le propriétaire.
3. **`lease_ttl` d'un hôte volatil** : 90 s proposés ; à mesurer en L115.
4. **Paquet de sortie** : branche par lot poussée par le serveur, ou PR
   ouverte par le serveur ? Recommandation : branche seulement, la PR suit le
   flux habituel du lot.
5. **Latence** : environ 30 à 60 appels HTTP par tour (bail, courrier,
   statut, coût). À mesurer ; si besoin, une opération composée
   (`turn.begin` = `begin_turn` + `take_pending_prompt` + `reserve`) en
   version 1.1, sans casser la table.
