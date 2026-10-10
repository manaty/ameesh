# Exécuteur médié : le contrat de `/api/exec/v1`

Contrat figé par le lot L107 (version `1.0.0`, schéma
`ameesh-exec-contract/1`). Étude et décision :
`docs/design/etudes/executeur-mediee.md`,
`docs/design/decisions/00xx-executeur-mediee.md`.

Un exécuteur ameesh tourne sur un appareil prêté (VM Compute) **sans accès à
la base**. Il parle au serveur du mesh par `/api/exec/v1`. Une modification
incompatible ouvre une version 2 du contrat ; elle ne retouche jamais la v1
en silence.

## Fichiers

| Fichier | Rôle |
|---|---|
| `src/ameesh/executeur_mediee/contrat.json` | la table fermée des 61 opérations et la liste des 160 refusées |
| `src/ameesh/executeur_mediee/contrat.py` | chargeur (`load()`), enveloppes, idempotence, erreurs, vérificateur de schémas |
| `src/ameesh/executeur_mediee/evenements.py` | flux SSE, attente longue, curseurs |
| `src/ameesh/executeur_mediee/porte.py` | `HostGate`, `ameesh-host-state/1`, `ameesh-host-ack/1` |
| `src/ameesh/executeur_mediee/interfaces.py` | interfaces entre L108, L109 et L110 |
| `tests/dore/executeur_mediee/*.json` | jeux d'essai dorés, partagés par le serveur et le client |
| `tests/test_l107_contrat_executeur.py` | cohérence de la table, des jeux dorés et des interfaces |

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

Comptes : 61 opérations, dont 23 lectures et 38 écritures. 51 passent par
`op`, 9 par `session/op`, et `wakeups.subscribe` passe par `events`. Le test
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

## Porte d'hôte

Le runner Compute écrit `ameesh-host-state/1`. Ce message porte `state`
(`available`, `draining` ou `stopped`), `seq`, `until_ts`,
`drain_deadline_ts`, `caps` et `reason`. Il passe par
`/run/ameesh-gate/state.json` (écrit puis renommé) ou par la socket
`gate.sock`, en JSON Lines.

L'exécuteur répond par `ameesh-host-ack/1`, qui porte `seq`, `state`,
`in_turn`, `held` et `drained`. Il relaie aussi l'état au serveur par
`PUT /host/availability` (`porte.availability_body`). Un état illisible
vaut `draining`.

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
  les schémas de `porte`.

## Client et mode médié (L109)

Le client vit dans `src/ameesh/storage/remote/` :

| Fichier | Rôle |
|---|---|
| `client.py` | `RemoteDb` (la « connexion », sans SQL), `RemoteStorage` (les 61 opérations), `LeaseBook` (carnet des baux) |
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
