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

Les tables de L110 (migration 0110) ne référencent aucune table d'autorité.
`Principal` ne porte aucun droit d'approbation. Le contrat refuse toutes les
opérations `approvals`, `nonces`, `grants` et `authenticators`.
`tests/test_l110_enrolement.py` le vérifie.
