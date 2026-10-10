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
| `src/ameesh/executeur_mediee/porte_hote.py` | L112 : `FileGate`, `SocketGate`, contrôleur de l'exécuteur, bail d'hôte volatil |
| `src/ameesh/executeur_mediee/disponibilite.py` | L112 : disponibilité des hôtes côté serveur, alertes |
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
vaut `draining` d'après `GateState.unreadable()` ; L112 le remplace par un
repli réglable (`host_gate_fallback`) : `stopped` pour un hôte médié,
`available` pour un hôte classique (consigne du propriétaire, voir
`porte_hote`).

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
