# Exécuteur médié : le contrat de `/api/exec/v1`

Contrat figé par le lot L107 (version `1.0.0`, schéma
`ameesh-exec-contract/1`), complété à l'assemblage de la voie B (version
`1.1.0`, compatible : voir « Contrat 1.1 »). Étude et décision :
`docs/design/etudes/executeur-mediee.md`,
`docs/design/decisions/00xx-executeur-mediee.md`.

Un exécuteur ameesh tourne sur un appareil prêté (VM Compute) **sans accès à
la base**. Il parle au serveur du mesh par `/api/exec/v1`. Une modification
incompatible ouvre une version 2 du contrat ; elle ne retouche jamais la v1
en silence.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/ameesh/executeur_mediee/contrat.json` | la table fermée des 62 opérations et la liste des 159 refusées |
| `src/ameesh/executeur_mediee/contrat.py` | chargeur (`load()`), enveloppes, idempotence, erreurs, vérificateur de schémas |
| `src/ameesh/executeur_mediee/evenements.py` | flux SSE, attente longue, curseurs |
| `src/ameesh/executeur_mediee/porte.py` | `HostGate`, `ameesh-host-state/1`, `ameesh-host-ack/1` |
| `src/ameesh/executeur_mediee/interfaces.py` | interfaces entre L108, L109 et L110 |
| `src/ameesh/executeur_mediee/porte_hote.py` | L112 : `FileGate`, `SocketGate`, contrôleur de l'exécuteur, bail d'hôte volatil |
| `src/ameesh/executeur_mediee/disponibilite.py` | L112 : disponibilité des hôtes côté serveur, alertes |
| `tests/dore/executeur_mediee/*.json` | jeux d'essai dorés, partagés par le serveur et le client |
| `tests/test_l107_contrat_executeur.py` | cohérence de la table, des jeux dorés et des interfaces |
| `src/ameesh/executeur_mediee/identite.py` | serveur : codes d'enrôlement, `DbIdentityProvider`, `verify_token`, révocation (L110) |
| `src/ameesh/executeur_mediee/appareil.py` | appareil : clé P-256, enrôlement, `HttpTokenSource` (L110) |
| `src/ameesh/executeur_mediee/jose.py` | formats ES256 partagés : JWK, empreinte, JWS, défi Nexlink (L110) |
| `src/ameesh/host_cli.py` | `ameesh host enroll\|revoke\|list\|show`, `ameesh device enroll\|challenge\|show` (L110) |

La table est dans le paquet, et non sous `docs/`, parce que l'exécuteur de
la VM et le serveur la lisent à l'exécution. Elle est livrée dans la roue
(`package-data`).

## La table

Chaque ligne de la table donne :

- `nom` : l'opération de `storage.interface` ;
- `ecriture` ;
- `transport` : `op`, `session/op` ou `events` ;
- `portee` ;
- `fence` ;
- `param_agent` ;
- `forces` : les paramètres que le serveur remplace ;
- `parametres` : le nom, la sorte (`positionnel` ou `nomme`), le schéma, `requis` et le défaut ;
- `resultat` : son schéma ;
- `refus` : la valeur rendue quand le bail est perdu ;
- `idempotence` ;
- `regles`.

Les portées :

- **A** : l'agent nommé est admis sur l'hôte de l'exécuteur.
- **B** : la portée A, plus une enveloppe de bail vivante.
- **H** : l'hôte est forcé à celui de l'exécuteur.
- **S** : l'agent et l'epoch sont tirés du jeton de session.
- **agregat** : une donnée agrégée.

Comptes : 62 opérations, dont 24 lectures et 38 écritures. 52 passent par
`op`, 9 par `session/op`, et `wakeups.subscribe` passe par `events`. Cinq
lignes de `op` sont marquées `session: true` (contrat 1.1) : elles sont
servies aussi par `session/op`. Le test
vérifie que chaque ligne existe dans `storage.interface` avec la même
signature et les mêmes types. Il vérifie aussi que l'interface entière (221
opérations) est classée, admise ou refusée. Toute opération ajoutée à
l'interface fait donc échouer le test tant qu'elle n'est pas classée.

## Requête et réponse

```
POST /api/exec/v1/op            Authorization: Bearer amx1.…
POST /api/exec/v1/session/op    Authorization: Bearer ams1.…
Idempotency-Key: <uuid>         (obligatoire pour toute écriture)

{"schema": "ameesh-exec-op/1", "op": "leases.begin_turn",
 "args": ["inge-front", "exec:7f3a:anna-portable:4121", 42, "tour en cours"],
 "kwargs": {},
 "fence": {"agent": "inge-front", "owner": "exec:7f3a:anna-portable:4121", "epoch": 42}}

200 {"schema": "ameesh-exec-result/1", "ok": true, "value": true, "server_ts": 1791640000.12}
```

- **Liaison.** Le serveur lie `args` et `kwargs` comme Python
  (`Contract.bind`), puis contrôle les schémas.
- **Enveloppe de bail (`fence`).** Elle est obligatoire en portée B. Ses
  valeurs `owner` et `epoch` doivent égaler celles des arguments. Le serveur
  la recontrôle sous `FOR UPDATE`, dans la transaction de l'opération.
- **Bail perdu.** Ce n'est pas une erreur. Le serveur répond `200` avec
  `"fenced": true`, et `value` vaut la valeur `refus` de la ligne.
- **Owner.** Sa forme est `exec:<executor_id>:<hôte>:<pid>` (`owner_for`).
- **Idempotence.** Le client tire la clé une seule fois par appel logique,
  puis la réutilise à chaque nouvel essai. L'empreinte de la requête est le
  SHA-256 du JSON canonique (RFC 8785) du corps, `fence` compris
  (`request_sha256`). Les clés sont gardées 24 h.
  - Avec la même clé et la même empreinte, le serveur rejoue la réponse et
    ajoute `Idempotency-Replayed: true`.
  - Avec la même clé et une autre empreinte, il répond `409`.

## Erreurs

Le corps d'une erreur a la forme
`{"schema": "ameesh-exec-error/1", "error", "message", "retry_after"?}`.
La table complète est `contrat.ERRORS`. Côté client, `client_exception`
fait la correspondance.

| HTTP | Codes | Exception côté client |
|---|---|---|
| 400 | `bad_request`, `bad_args`, `fence_required`, `idempotency_key_required` | `DbError` |
| 401 | `token_expired`, `token_invalid` | le client renouvelle le jeton, essaie une fois, puis lève `DbError` |
| 402 | `budget_exceeded` (relais) | `DbError` |
| 403 | `forbidden_scope`, `host_unavailable` | `Forbidden` |
| 403 | `executor_revoked` | `ExecutorRevoked` : l'exécuteur s'arrête |
| 404 | `op_not_allowed` | `NotSupportedRemotely` (aussi levée localement, avant tout appel) |
| 409 | `idempotency_mismatch` | `DbError` |
| 413 | `too_large` | `DbError` |
| 429, 5xx, coupure | `rate_limited`, `internal`, `unavailable` | `Unavailable` : les reprises de L72 s'appliquent |

## Événements

`GET /api/exec/v1/events` sert le flux SSE. Le repli en attente longue est
`?wait=25&after=<curseur>`, qui rend `ameesh-exec-events/1` :
`{events: [{id, channel, data}], last_id}`.

- **Curseur.** Sa forme est `<flux>:<n>`. Le flux identifie le processus
  serveur. La reprise passe par `Last-Event-ID` ou par `after`.
- **Canaux.** Ce sont `agent_mail`, `agent_lease` et `ameesh_budget`.
  `data` contient le payload NOTIFY d'origine. `Event.as_signal()` rend la
  forme de `Subscription.wait`.
- **Battement.** Le serveur envoie `: ping` toutes les 20 s.
- **Trou de reprise.** Un curseur sorti du tampon de 1 000 événements, ou
  venu d'un autre flux, donne un événement `reset`. Le client relit alors
  tout.

Un réveil ne fait jamais foi : l'exécuteur relit toujours la base.

## Contrat 1.1

L'assemblage de la voie B (L108 à L114) a relevé des écarts entre le
client, le serveur et les jeux dorés. Ils sont tranchés ici, dans
`contrat.json` (version `1.1.0`) et dans les jeux dorés, régénérés par
`tests/dore/generer_executeur.py`. Le client et le serveur lisent la même
table : aucun écart n'est toléré d'un côté seulement.

- **Jeux dorés conformes au schéma.** États de lot de `work.STATES`,
  `kind` de 0012, identifiants et empreintes d'action de 0010, clés du
  pilote pour `hosts.record` (`mem_available_bytes`, `swap_used_bytes`,
  `load1`, `cpu_count`, `disk_free_bytes`, `disk_path`,
  `turns_in_progress`), identifiant d'exécuteur sur 16 caractères
  hexadécimaux, code d'enrôlement de 24 caractères Crockford.
- **`leases.state`** rend `{lease_owner, lease_epoch, status, live}`, les
  clés que lit `registry.lease_matches`.
- **NOTIFY `agent_lease`.** Sur le fil, le payload est `{name, owner,
  epoch, status}`. Le déclencheur de 0001 écrit `agent` ; le serveur le
  renomme `name` à la réception (`flux.wire_payload`) et filtre sur `name`.
- **Idempotence.** `request_sha256` canonise aussi les décimaux (un TTL de
  90.5 s, un coût de 0.031 $), à la manière d'ECMAScript (RFC 8785).
  `90.0` et `90` ont la même empreinte. NaN, les infinis et les entiers
  hors de ±(2^53 − 1) donnent 400 `bad_args`.
- **`agents.overview`** (annuaire réduit de la session) : `name`, `role`,
  `team`, `chantier`, `canon_governed`, `status`. `role` vaut `team` : le
  registre n'a pas de colonne « rôle ».
- **Lignes ouvertes au jeton de session** (`session: true`) :
  `mailbox.reserve`, `mailbox.deliver`, `mailbox.release` (le hook
  `agent-mail` dans la VM), `mailbox.unread` (`mail inbox`) et
  `threads.index`. L'agent contrôlé doit être celui du jeton, et
  l'enveloppe de bail doit porter son agent et son epoch. Le bail du tour
  (`AMEESH_RUNNER_ID`, `AMEESH_LEASE_EPOCH`) entre au carnet de la session.
- **`operations.assigned_open_lots`** entre dans la table (lecture, portée
  A) : l'exécuteur en a besoin pour le lot du tour (rotation de session au
  changement de lot, 0025). L'essai L115 l'a trouvé manquant.
- **`threads.index`** : l'auteur est `agent:<nom>` (membre du fil,
  `fil.member`) ; le nom nu reste admis.
- **`HostInfo.limits`** (`GET /host`) : `{"max_agents": int|null,
  "resources": {"min_mem_available", "max_swap_used", "max_load",
  "min_disk_free"}}`, tiré du canon (`resources.host_limits`). Un seuil
  absent prend son défaut prudent côté exécuteur.
- **Porte.** `GateAck.from_json` lève `ValueError` pour tout corps faux.
  Un état illisible vaut `stopped` pour un hôte médié. `max_concurrent`
  vaut 1 par défaut.
- **Coûts.** `turn_costs.insert` venu d'un appareil est rangé
  `source=device`, avec l'exécuteur et le bail de l'enveloppe, quelles que
  soient les valeurs reçues (forçages `executeur`, `owner_enveloppe`,
  `epoch_enveloppe`). Pour la dépense d'un appareil, seuls les relevés du
  relais (`source=relay`) comptent dans les plafonds : `turn_costs.spent`
  ignore `source=device`.
- **Interfaces.** `IdentityProvider.issue_session_token` lève
  `ScopeError("forbidden_scope")` quand le bail n'appartient pas à
  l'exécuteur ou que l'agent est hors de son invitation. Les commandes
  sont `ameesh host` (serveur) et `ameesh device` (appareil).

## Porte d'hôte

Le runner Compute écrit `ameesh-host-state/1`. Ce message porte `state`
(`available`, `draining` ou `stopped`), `seq`, `until_ts`,
`drain_deadline_ts`, `caps` et `reason`. Il passe par
`/run/ameesh-gate/state.json` (écrit puis renommé) ou par la socket
`gate.sock`, en JSON Lines.

L'exécuteur répond par `ameesh-host-ack/1`, qui porte `seq`, `state`,
`in_turn`, `held` et `drained`. Il relaie aussi l'état au serveur par
`PUT /host/availability` (`porte.availability_body`). Un état illisible
vaut `stopped` pour un hôte médié (`GateState.unreadable()`, contrat 1.1) ;
un hôte classique doté d'une porte garde un repli réglable
(`host_gate_fallback`, L112, voir `porte_hote`). `caps.max_concurrent`
absent vaut 1 (`porte.DEFAULT_MAX_CONCURRENT`).

La classe abstraite est `porte.HostGate` :

```python
state() -> GateState
wait_change(timeout) -> GateState
acknowledge(ack: GateAck) -> None
close() -> None
```

`AlwaysAvailable` en est l'implémentation par défaut.

## Ce que chaque lot implémente

- **L108, serveur.**
  - `interfaces.ExecDispatcher.dispatch(principal, request, *, route, idempotency_key) -> (statut, corps, en-têtes)`.
  - `interfaces.ScopeRules` : `admitted_agents`, `apply` et `filter_result`.
  - Le serveur valide chaque requête par `Contract.validate_request`. Il
    authentifie par `ExecutorAuth.verify(token) -> Principal`, avec un
    bouchon jusqu'à L110.
  - Le flux d'événements suit `evenements`. La route `GET /host` rend
    `HostInfo`.
- **L109, client.**
  - `interfaces.ExecTransport` : `call`, `session_call`, `poll_events`,
    `stream_events`, `host_info`, `put_availability` et `session_token`.
  - `RemoteStorage` lève `NotSupportedRemotely` hors de la table. Les
    erreurs passent par `client_exception`.
  - Les essais tournent contre un faux serveur nourri de
    `tests/dore/executeur_mediee`.
- **L110, identité.**
  - `interfaces.IdentityProvider` : `verify`, `enroll`,
    `issue_access_token`, `issue_session_token` et `revoke`.
  - `interfaces.TokenSource.access_token(*, refresh)` côté client.
  - Les jetons sont opaques : `amx1.…` pour un jeton d'accès de 10 min,
    `ams1.…` pour un jeton de session lié au bail. L'assertion est un JWS
    ES256 de 60 s au plus.
- **L112, inactivité.** `FileGate(HostGate)` et `SocketGate(HostGate)`, sur
  les schémas de `porte` (livrés dans `porte_hote`, avec le contrôleur de
  l'exécuteur `GateController`, `lease_ttl_for` et `TransportSink`, que L109
  branche sur `ExecTransport.put_availability`). Côté serveur,
  `disponibilite` : `check_body`, `admissible`, `AvailabilityRegistry`
  (mémoire, fichier ; L108 peut la mettre en base) et les alertes
  `host_unavailable`, `host_drain_overdue`. Exploitation :
  `docs/EXPLOITATION.md`, « Porte d'hôte ».

## Le serveur (L108)

`ameesh serve --exec-only --server-url URL [--listen 127.0.0.1:8471]
[--work-repos FICHIER]` sert `/api/exec/v1` seul, tant que `ameesh serve`
(L84) n'existe pas. Depuis l'assemblage de la voie B, il sert l'identité des
exécuteurs enrôlés (L110, `LockedIdentity` sur `DbIdentityProvider`), le
relais de modèle sous `/api/exec/v1/llm/` (L111, `--no-relay` pour s'en
passer) et, avec `--work-repos`, le dépôt de travail (L113). Il synchronise
aussi le canon de chaque hôte médié enrôlé (`--canon-sync`, 60 s). Les
limites de `GET /host` viennent du canon (`resources.host_limits`).
`--auth-file` (jetons fixes, `bouchon.StaticAuth`) reste pour les bancs
d'essai ; il exclut le relais. Le
serveur utilise la bibliothèque standard (`http.server`) et n'ajoute aucune
dépendance. Une écoute hors de la boucle locale exige TLS (`--tls-cert`,
`--tls-key`) ou `--allow-plain` derrière un mandataire qui termine TLS.

| Fichier | Rôle |
|---|---|
| `executeur_mediee/serveur.py` | `ExecApp` (routes, sans socket), `ExecHTTPServer`, `main` |
| `executeur_mediee/repartiteur.py` | `PgDispatcher` : transaction, idempotence, fencing, audit |
| `executeur_mediee/portee.py` | `HostScopeRules` : une règle par ligne de la table |
| `executeur_mediee/flux.py` | `EventHub` : une écoute `LISTEN`, tampon, filtre par hôte |
| `executeur_mediee/bouchon.py` | `StaticAuth`, jetons fixes, en attendant L110 |
| `migrations/0048_executeur_mediee.sql` | `exec_idempotency`, `exec_host_availability`, `exec_audit` |

**Agents admis.** Un agent est admis s'il remplit toutes ces conditions :

- il est inscrit sur l'hôte de l'exécuteur ;
- il est gouverné par le canon ;
- son placement est admis pour son profil actuel ;
- il figure dans la liste blanche de l'invitation, si elle existe ;
- son mode d'identifiants fait partie de `--credential-modes`, si l'option
  est donnée.

Un agent inscrit à la main n'est jamais admis sur un hôte médié.

**Une transaction par opération.** Le serveur y enchaîne, dans l'ordre :

1. le verrou d'idempotence et la relecture de la clé ;
2. la portée ;
3. le fencing sous `FOR UPDATE` ;
4. l'appel du pilote ;
5. l'écriture de la réponse d'idempotence ;
6. l'audit.

Toutes les écritures de portée B sont fencées, y compris celles que
l'interface ne fence pas (`agents.set_status`, `pending_spend.*`,
`turn_resources.*`, `threads.index`…). Une écriture de portée S exige que le
bail du jeton de session tienne encore (même epoch, même exécuteur, bail
vivant). Sinon le serveur répond `401 token_expired`.

**Porte d'hôte.** `PUT /host/availability` garde le dernier état rapporté
(`seq` ne recule pas). L'hôte est indisponible dans trois cas : aucun état
rapporté, un état autre que `available`, ou une fenêtre `until_ts` échue.
Dans ces cas, `leases.claim` répond `403 host_unavailable` et
`agents.claimable` rend une liste vide.

**Bornes.**

- Corps limité à 1 Mio (`413`).
- `limit` limité à 200, listes d'identifiants à 1 000, relevés de jauge à 100.
- 600 requêtes par minute et par exécuteur, 1 200 par adresse (`429`,
  `Retry-After`).
- 256 connexions simultanées.
- Une connexion SSE dure au plus 10 minutes, la vie d'un jeton d'accès.

**Audit.** `exec_audit` reçoit chaque écriture, chaque rejeu et chaque
refus. Il ne garde jamais ni argument ni corps. Les clés d'idempotence de
plus de 24 h sont élaguées toutes les 10 minutes, l'audit après 90 jours.

**Points d'intégration.**

- **L110.** `LockedIdentity` (une connexion dédiée, un appel à la fois,
  rouverte après une panne) ouvre `/enroll`, `/token` et `/session-token`
  et vérifie chaque jeton par `verify_token(db, jeton, kind=…)`. Le serveur
  recontrôle le bail de `/session-token` dans sa transaction avant
  d'appeler `issue_session_token`.
- **L111.** Le relais est monté au niveau HTTP (`extra_routes["llm"]`,
  objet doté de `handle`) : `relay.ExecutorTokens` vérifie le jeton de
  session par L110 (`kind="session"`), puis relit le bail en base ; c'est
  cet owner qui est inscrit au grand livre (`source=relay`). Une ligne
  `turn_costs` déclarée par l'appareil est rangée `source=device`, hors
  plafond.
- **L113.** `extra_routes["bundle"]` : `depot.WorkDepot` (voir « Dépôt de
  travail »).
- **L112.** L'exécuteur relaie sa porte par `PUT /host/availability` au
  démarrage, à chaque changement et jusqu'à ce que le serveur l'ait reçue
  (nouvel essai toutes les 5 s) : sans ce rapport, l'hôte reste
  indisponible. Sans porte configurée, l'exécuteur médié rapporte
  `AlwaysAvailable`.

## Client et mode médié (L109)

Le client vit dans `src/ameesh/storage/remote/` :

| Fichier | Rôle |
|---|---|
| `client.py` | `RemoteDb` (la « connexion », sans SQL), `RemoteStorage` (les 62 opérations), `LeaseBook` (carnet des baux) |
| `http.py` | `HttpTransport(ExecTransport)`, `StaticTokenSource`, `FileTokenSource` |
| `events.py` | `RemoteSubscription` : SSE, repli en attente longue, curseur gardé |
| `__init__.py` | `connect(cfg)`, `set_token_source_factory` (point d'attache de L110) |

- **Choix du stockage.** `storage.of(db)` rend le stockage distant pour une
  `RemoteDb`. `db.connect(cfg)` rend une `RemoteDb` quand
  `backend: mediated` ; `exec_url` (`AMEESH_EXEC_URL`) donne le serveur.
- **Hors table.** Toute opération refusée lève `NotSupportedRemotely` avant
  tout appel. Une connexion de session n'appelle que `session/op`, une
  connexion d'exécuteur que `op`.
- **Enveloppe de bail.** Elle vient des arguments `owner`/`epoch`, sinon du
  carnet des baux, tenu par `claim`, `renew`, `release` et chaque réponse
  `fenced`. Sans bail détenu, l'opération rend sa valeur de refus sans
  appel.
- **Transport.** Un 401 renouvelle le jeton une fois. Une coupure, un 429
  ou un 5xx donnent deux nouveaux essais (même `Idempotency-Key`,
  `Retry-After` borné à 5 s), puis `Unavailable` : les reprises de L72
  prennent le relais. TLS obligatoire hors de la boucle locale.
- **Jeton d'exécuteur.** Par défaut, il est lu dans un fichier 0600
  (`exec_token_file`, `AMEESH_EXEC_TOKEN_FILE`). L110 fournit la vraie
  source par `set_token_source_factory(factory)`.

Le mode médié de l'exécuteur (`runner.py`, `Runner.mediated`) :

- l'hôte, l'owner (`exec:<id>:<hôte>:<pid>`), le bail et les limites
  viennent de `GET /host` ;
- ni synchronisation du canon, ni relevé des soldes, ni échéance des
  délégations, ni déplacement entre hôtes, ni connexion à la base ; le
  relevé des ressources passe par `hosts.record` ;
- aucune réclamation quand la porte d'hôte n'est pas `available`
  (`Runner.gate`, `AlwaysAvailable` par défaut ; L112 la remplace) ;
- un événement `reset` du flux réveille tous les workers ;
- `executor_revoked` arrête l'exécuteur, sans nouvel essai ;
- le harnais reçoit `AMEESH_BACKEND=mediated`, `AMEESH_EXEC_URL` et
  `AMEESH_EXEC_TOKEN` (jeton de session demandé après `begin_turn`), jamais
  de DSN ;
- `agent-runner register|stop` et `--migrate` sont refusés.

La session du harnais (`backend.MediatedBackend`) passe par le jeton de
session : `mail whoami`, `mail send`, `work move|note`, `action propose`.

Écarts relevés avec le contrat 1.0.0 (à trancher par une version 1.1) :

1. Le hook `agent-mail` réserve, livre et relâche le courrier, et
   `mail inbox` lit `mailbox.unread`. Ces opérations sont de la route `op`
   (jeton d'exécuteur) : une session ne peut pas les appeler. En mode
   médié, le hook ne remet rien (le courrier attend le tour suivant) et
   `mail inbox` échoue.
2. `threads.index` est de la route `op` : `mail send` et `action propose`
   en session n'écrivent pas le fil (avertissement de `fil.record`).
3. `threads.index` contrôle `author` comme nom d'agent, mais `fil.record`
   envoie `agent:<nom>` : `validate_request` répond `bad_args`.
4. Jeux dorés : `leases.state` rend `owner`/`epoch` au lieu de
   `lease_owner`/`lease_epoch` (clés du pilote, lues par
   `registry.lease_matches`) ; `work.*` emploie les états `doing`/`review`,
   absents de `work.STATES`.
5. `HostInfo.limits` : « format de `resources.host_limits` » est ambigu. Le
   client lit `max_agents` et les seuils (à plat ou sous `resources`).

  les schémas de `porte`.

## Identité des appareils (L110)

### Enrôlement

1. Sur le serveur, un humain habilité émet un code :
   `ameesh host enroll <hôte> --by human:ID [--mesh M] [--agents a,b] [--ttl 15m]`.
   - **Habilité** : le responsable de la fiche `Host`, ou un membre de rôle
     `coordinator`, `coordinateur` ou `coordinatrice` du canon qui déclare
     l'hôte. La fiche `Host` doit exister.
   - **Jamais une session d'agent** : la commande refuse de tourner si
     `AGENT_MAIL_NAME`, `AMEESH_RUNNER_ID`, `AMEESH_LEASE_EPOCH` ou
     `AMEESH_EXEC_TOKEN` est posée, ou si la session est liée à un agent
     (`ameesh mail bind`).
   - **Le code** : 24 caractères de Crockford (120 bits), en 6 groupes de 4,
     à usage unique, 15 min par défaut et 1 h au plus. Il est lié au mesh, à
     l'hôte et à la liste d'agents. Chaque agent de la liste doit être admis
     sur l'hôte par le canon. Seul le SHA-256 du code est gardé.
2. Dans la VM : `ameesh device enroll --server https://mesh.exemple --code-file -`
   (code lu sur l'entrée standard, ou `--code-file FICHIER`). `--code CODE`
   reste accepté mais laisse le code dans la ligne de commande
   (`/proc/<pid>/cmdline`) : à éviter. `--code` et `--code-file` sont
   exclusifs, l'un des deux est obligatoire.
   - La clé P-256 est générée au premier appel, en PKCS#8 PEM, dans
     `AMEESH_EXEC_HOME` (`/var/lib/ameesh-exec` par défaut) : fichier `0600`
     dans un dossier `0700`. Une clé trop ouverte est refusée.
   - `proof` est la signature ES256 de JCS({code, public_key, server_url}).
   - L'état (`executor.json`, `0600`) garde l'identifiant de l'exécuteur,
     l'hôte, le mesh et l'URL du serveur, sans secret.
3. Liaison facultative à la clé d'appareil Nexlink :
   `ameesh device challenge --server … --code-file -` écrit le défi (ASCII, une
   ligne par champ). L'application de bureau le signe avec la clé d'appareil.
   `--attestation FICHIER` le joint à l'enrôlement :
   `{"schema": "ameesh-device-attestation/1", "device_public_key": <SPKI
   base64url>, "signature": <r‖s base64url>}`. Une attestation fausse fait
   refuser l'enrôlement ; une attestation absente n'empêche rien.

### Formats

- Signatures : ES256 au sens de JOSE, `r‖s` brut de 64 octets en
  base64url. Jamais de DER.
- Clé publique : JWK EC P-256 ; son empreinte RFC 7638 est le `kid` des
  assertions.
- Identifiant d'exécuteur : 16 chiffres hexadécimaux.
- Jeton d'accès `amx1.…` : 10 min, obtenu par une assertion de 60 s au plus,
  `jti` à usage unique, `aud` = l'URL du serveur sans `/` final.
- Jeton de session `ams1.…` : il vit tant que le bail (agent, epoch) de
  l'exécuteur vit, 12 h au plus. Son `expires_ts` est l'échéance du bail au
  moment de l'émission ; les renouvellements du bail le prolongent.

### `verify_token`, pour L108 et L111

```python
from ameesh.executeur_mediee.identite import verify_token
principal = verify_token(db, token, kind="executor" | "session" | None)
```

La fonction rend un `interfaces.Principal`. Elle lève `interfaces.AuthError`
avec l'un de ces codes :

- `token_invalid` : jeton mal formé, inconnu ou de la mauvaise sorte ;
- `token_expired` : jeton échu ; pour un jeton de session, aussi un bail
  perdu ou un epoch changé ;
- `executor_revoked`.

L'état est relu au plus toutes les 5 s. `DbIdentityProvider(db, mesh=…)`
porte le reste de `IdentityProvider` : `enroll`, `issue_access_token`,
`issue_session_token` et `revoke`. `issue_session_token` lève `ScopeError`
(`forbidden_scope`) si le bail n'est pas vivant ou s'il est hors de la liste
de l'invitation.

### Révocation et état

- `ameesh host revoke <hôte> --by human:ID --why … [--executor ID]` :
  - les exécuteurs sont marqués révoqués ;
  - leurs jetons sont effacés ;
  - leurs baux sont relâchés (epoch + 1, le courrier réservé est re-livré) ;
  - les codes en attente sont annulés.

  Tout se fait dans une transaction. Pensez aussi à retirer la fiche ou
  l'admission au canon.
- `ameesh host list` et `ameesh host show <hôte>` donnent :
  - la fiche, ses occupants et les agents admis ;
  - les exécuteurs et leurs baux ;
  - les codes en attente ;
  - le journal (`executor_events`).

### Occupants (0029)

La fiche `Host` accepte `occupants: [human:…]` : les humains qui ont l'accès
physique à la machine sans l'administrer. Ils entrent dans `H(hôte)` avec le
responsable et les `admins`. La règle de visibilité les vérifie donc au
placement (`canon sync`). Sans `occupants` déclarés, `H(hôte)` reste le
responsable et les admins : seules les personas lisibles par eux partent sur
la machine. Un occupant qui ne résout pas vers un `Member` rend la règle
illisible pour l'hôte, donc refusée (`host-occupant-unresolved`).

### Jamais d'approbation (0012)

Les tables de L110 (migration 0049) ne référencent aucune table d'autorité.
`Principal` ne porte aucun droit d'approbation. Le contrat refuse toutes les
opérations `approvals`, `nonces`, `grants` et `authenticators`.
`tests/test_l110_enrolement.py` le vérifie.

## Dépôt de travail (L113)

L'appareil n'a ni identifiant de forge, ni clone du dépôt. Le travail
passe par le serveur (`executeur_mediee/depot.py`, monté par
`ameesh serve --work-repos FICHIER [--work-cache DOSSIER]`).

- **Prise du bail.** `GET /work/{agent}/bundle`, avec le jeton d'accès et
  l'enveloppe de bail en en-têtes (`X-Ameesh-Lease-Owner`,
  `X-Ameesh-Lease-Epoch`). Le serveur rend l'archive tar EXACTE du commit
  de départ : la branche `agent/<nom>` si elle existe sur la forge, sinon
  la branche de base du dépôt. L'archive est construite depuis l'arbre
  (modes et liens compris), sans les attributs `export-ignore`. L'appareil
  en fait un dépôt git sous `<AMEESH_EXEC_HOME>/work/<agent>/<epoch>`. Son
  premier commit est recréé à l'identique des deux côtés (même arbre,
  auteur, date et message fixes) ; le serveur l'annonce
  (`X-Ameesh-Device-Base`) et l'appareil le vérifie.
- **Après chaque tour, et à la fin du bail.** L'appareil envoie le paquet
  git de ses commits depuis le dernier envoi (`POST`, `X-Ameesh-Base`,
  64 Mio au plus). Le serveur rejoue chaque commit sur son historique :
  même arbre, même auteur, même message, le committer est l'exécuteur.
  Il pousse ensuite `agent/<nom>` avec ses propres identifiants, en avance
  rapide seulement (`--force-with-lease`). Il rend
  `ameesh-exec-bundle/1` : `{branch, commit, device_head, commits}`.
- **Fin du bail.** Le travail non commité est commité, envoyé, puis le
  dossier est effacé. Un bail perdu efface aussi le dossier, sans envoi.

Refus : bail mort ou d'un autre exécuteur (403 `forbidden_scope`), base
inconnue (409 `idempotency_mismatch`), commit de fusion ou sous-module
(400 `bad_args`). Sans dépôt monté, la route rend 404 : l'exécuteur
travaille alors dans un dossier vide.

`--work-repos` :

```json
{"agents": {"inge-front": {"repo": "/srv/git/site.git", "base": "main"}},
 "default": {"repo": "git@forge.example:org/site.git", "base": "main"}}
```
