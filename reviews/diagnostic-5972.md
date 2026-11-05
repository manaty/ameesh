# Diagnostic #5972 — sessions courtes vers la base centrale

Lot #5972 (dépend de #5764), mty-amh-impl-13, 2026-11-05. Diagnostic
seulement : aucun réglage PG, aucun redémarrage, aucun correctif ici.
Base : `origin/main` af552e6be.

## Constat du bureau (#194428, 19:01Z)

~92 nouvelles sessions/s, ~540 commits/s, 125-126 backends, zéro rollback ;
clients 10.77.0.4 (agents-2, 52 au repos), 10.77.0.3 (agents-1, 33),
10.77.0.5 (agents-3, 17).

## Mesures locales (agents-3, 19:25-19:40Z)

- Sessions nouvelles vers 10.77.0.1:5432, échantillonnage `ss` à 5 ms sur
  20-30 s : 4,2 à 5,2/s, durée médiane ~2 s ; TCP sortant total de l'hôte
  (`/proc/net/snmp` ActiveOpens) : 5/s. Borne haute de agents-3 : ~5/s.
- 9 processus `ameesh run` sur l'hôte : ~0,55 session/s par runner.
- Aucun processus CLI `ameesh`/`agent-mail` réel lancé en boucle (hors
  bouchons de tests) sur 90 s ; aucune minuterie de rôle sur cet hôte.
- `AMEESH_DB_TRACE` : `agent-mail inbox`, `ameesh work show`, `ameesh list`
  = 1 connexion chacune ; le wrapper `gh` n'en ouvre aucune.
- Attribution par PID impossible pour un agent (`/proc/<pid>/fd` refusé sous
  NoNewPrivs) : l'attribution fine passe par `application_name` (§ 4).

## pgbouncer (#653) : jamais posé sur les VM

agents-3 : ni binaire `~/.local/bin/pgbouncer`, ni `~/.config/pgbouncer`,
ni `~/.local/state/pgbouncer` ; le DSN de `config.json` et de ses quatre
sauvegardes (30/10 → 03/11) vise 10.77.0.1:5432 en direct. Le relevé du
bureau du 27/10 (#150697, consigné dans `docs/DEPLOIEMENT-HOTE-AGENTS.md`
§ pool) donne déjà : seul pc-smichea a PgBouncer, les trois VM se connectent
directement. Il n'a donc pas été posé puis retiré : il n'a jamais été posé
sur agents-1/2/3. agents-2 constaté identique par mesh-design ; agents-1 à
confirmer par le bureau.

Or le code de #653 suppose ce pool : `ScopedDb` (ci-dessous) a été écrit pour
rendre les sessions entre deux instructions, ce qui ne coûte rien derrière un
pool en mode session (la session serveur est réutilisée, `server_idle_timeout`
30 s) mais coûte une session PostgreSQL complète (fork du backend, TLS,
authentification) sans lui.

## Sources, par part estimée

Les parts sont des estimations : constantes du code, mesure d'agents-3
extrapolée aux ~46 runners actifs (15 sur agents-1, 20 sur agents-2, 8 sur
agents-3, 3 sur pc-smichea). La répartition par IP du serveur (demandée au
bureau) les confirmera ou les corrigera.

1. **`ScopedDb` : une session par instruction SQL** —
   `src/ameesh/scoped_db.py:21-43`. Chaque `query`/`execute` hors
   `transaction()` fait `db.connect()` puis `close()`. Treize passes
   périodiques du runner l'utilisent : `branch_sweep_once`
   (`runner.py:13566`, 300 s, dizaines à centaines d'instructions),
   `resource_once` + `host_lifecycle_once` (`runner.py:13090`, 60 s),
   `host_release_once` (`runner.py:13169`, 60 s), `canon_sync_once`
   (`runner.py:12750`, 300 s, 30 s avant que la porte se pose),
   `_housekeeping_pass` (600 s), `notify_health_once` (300 s), passes du
   train (`train_runner.py:514-713`, 60 s), `balance_once`, l'adaptateur du
   cycle de vie d'hôte (`runner.py:10960`). Mesuré : ~0,5/s par runner,
   soit **~20-25/s pour la flotte de runners** (≈ 25 % du total).
2. **Coût doublé de chaque connexion** — `db.connect()` passe par
   `_writer_connection` (`db.py:1597`) : `require_compatible_writer`
   (`migrations.py:776-808`) lit `schema_non_additive_migrations` à chaque
   session, sans cache. Avec `autocommit=True` (`db.py:1138`), chaque
   instruction est un commit : chaque session `ScopedDb` = au moins deux
   commits. C'est la principale explication du rapport ~540 commits/s pour
   ~92 sessions/s (≈ 6 instructions par session en moyenne, chacune commise
   à part ; les lectures sont comptées dans `xact_commit`).
3. **`ameesh serve`** — `serve.py:1077-1101` : `db_reader`/`db_writer`
   ouvrent une session par lecture ou écriture, donc par requête HTTP ; les
   flux SSE en ouvrent une par lot drainé (`serve.py:1545`), à chaque NOTIFY
   `ameesh_events` et toutes les 1 s (`WAITING_POLL_S`, `serve.py:123`) tant
   qu'un événement attend ; repli hub toutes les 5 s (`POLL_S`) ; l'UI
   relance une requête au plus tard 1 s après chaque événement
   (`ui/static/ameesh-ui.js:115`). Avec un fort débit d'événements, quelques
   onglets ouverts suffisent à des dizaines de sessions/s. Part inconnue
   tant que le bureau n'a pas dit où tourne `serve` (sur le serveur même :
   connexions locales, hors des trois IP).
4. **Publieur `agent_peek`** — `agent_peek.py:172-199` : nouvelle connexion
   toutes les 15 s (`INTERVAL`) par worker, au repos compris : ~46/15 ≈
   **3/s** fixes (+ la lecture de compatibilité).
5. **Crochets des harnais et minuteries de rôle** — `agent-mail hook`
   (SessionStart, UserPromptSubmit, PostToolUse si le marqueur a plus de
   15 s, Stop : `fastmail.py:41,265-312`) ; `review_provenance record` à
   chaque commit git (`review_provenance.py:1352`) ; minuteries
   `role_timers.py:111-146` (alertes 30 s, release 60 s, observation 60-90 s,
   calendrier `*:0/10`), un processus et une ou plusieurs sessions chacune.
   Ordre de grandeur : quelques sessions/s pour la flotte.

Le bail (`runner.py:1195`, 100 s) et la réclamation de canon (100 s) ouvrent
aussi leur propre connexion, mais leur part est négligeable. La boucle
principale, le listener et les sondes d'inactivité réutilisent `self.db` :
ils chargent la base en instructions, pas en sessions.

Total des parts 1+4+5 : ~25-35/s. Les ~55-65/s restants viennent soit de
`serve`, soit d'agents-1/agents-2, qui feraient plus qu'agents-3 par runner
(plus d'hôtes ou de rôles élus, train). La répartition par IP du serveur
tranche.

## Attribution : `application_name`

Ni `application_name` ni `fallback_application_name` ni `PGAPPNAME` ne sont
posés (`db.py:1134-1155`, `startup_options` `db.py:261-289`, environnement
de `PsqlDriver`). Toutes les sessions se ressemblent dans `pg_stat_activity`.
Proposition : `fallback_application_name` (pour qu'un DSN explicite reste
maître) de la forme `ameesh/<famille>/<agent ou rôle>@<hôte>`, tronqué à 63
octets, la famille étant posée par l'appelant (`runner.main`,
`runner.scoped.<passe>`, `peek`, `lease`, `serve`, `cli.<commande>`,
`hook`, `timer.<rôle>`). PgBouncer suit déjà `application_name`
(`pgbouncer_local.py:67`), il n'y a donc rien à ajouter à
`track_extra_parameters`.

## Lots correctifs proposés

| # | Lot | Gain estimé | Classe |
|---|---|---|---|
| A | `application_name` posé sur chaque session (famille, agent ou rôle, hôte) | attribution sans heuristique, préalable aux mesures | sensible (db) |
| B | `ScopedDb` : une session par passe (ouverte à la première instruction, fermée en fin de passe et avant tout travail externe long), au lieu d'une par instruction | ~20/s et ~2 commits par instruction | sensible (concurrence) |
| C | `require_compatible_writer` est volontairement sans cache (`migrations.py:777` : les minuteries longues doivent voir une incompatibilité nouvelle) ; le vérifier une fois par passe ou par processus court plutôt qu'à chaque session, en gardant cette garantie (vérification au bord de tick, déjà exigée par `_writer_connection`) | ~moitié des commits des sessions courtes | sensible (porte de migration) |
| D | `serve` : connexions de lecture et d'écriture réutilisées (pool interne, ou pool `psycopg_pool` comme `mediated_executor/server.py:706`), SSE drainés sur une connexion partagée | dépend de la mesure serveur | sensible |
| E | `agent_peek` : publier sur la connexion du runner (ou une connexion gardée par le publieur) au lieu d'une nouvelle toutes les 15 s | ~3/s | léger |
| F | Pose de PgBouncer sur agents-1/2/3 par le bureau, recette `deploy/pgbouncer-host.md`, après relecture du dimensionnement (budget 3 × 80 + pc 80 > 300 à revoir : agents-3 est à 40) | absorbe les sessions courtes restantes côté serveur | geste du bureau, pas un lot de code |

Ordre recommandé : A d'abord (il rend mesurables tous les autres), puis B et
C, puis D selon la part que le serveur attribue à `serve`. F est indépendant
du code et peut précéder B : il retire immédiatement le coût des sessions
courtes au serveur sans toucher au code, B et C réduisent ensuite le nombre
d'allers-retours.

## En attente

Lectures serveur demandées au bureau (courrier du 2026-11-05) : TIME_WAIT
par IP cliente sur 5432 (taux exact par hôte, connexions locales comprises),
photo `pg_stat_activity` groupée, processus ameesh sur manaty-mesh-1.
