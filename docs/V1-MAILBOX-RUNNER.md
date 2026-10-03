# agent-mesh v1 — boîte aux lettres Postgres et exécuteur par machine

Ce document couvre les points 1 et 2 de la spec (§5) : ce qui a été construit,
les décisions prises, et ce qui reste volontairement dehors. Il est écrit pour
être relu par un autre agent (relecture croisée) et pour servir de base au
portage IMA (M1 : paquet autonome `agent-mesh`).

---

## 1. Langage : Python 3 stdlib + `psycopg` si présent, sinon `psql`

**Décision : Python 3**, avec une couche `db.py` qui parle à Postgres par
`psycopg` (v3) quand il est importable, et par le binaire `psql` sinon.

Pourquoi Python plutôt que Node :

1. **La v0 est en Python.** `agent-mail.v0.py` définit l'identité (alias,
   `AGENT_MAIL_NAME`, assainissement du dossier), le rendu des hooks et le
   format de la boîte fichier. Réimplémenter ces règles dans un second langage
   aurait créé deux sources de vérité pour la compatibilité ; en Python, la
   logique est portée à l'identique et testée contre le script v0 lui-même
   (même JSON, à l'octet près).
2. **Zéro dépendance possible.** La stdlib couvre JSON, sous-processus,
   sockets, threads, `pty`, `argparse`, `unittest`. Avec le pilote `psql`, le
   paquet tourne sans **aucune** dépendance Python — ce qui compte sur un PC
   d'agent où l'on n'installe pas de venv à la légère. Node n'a pas de client
   Postgres dans sa stdlib : il aurait fallu `pg`, donc un `node_modules` à
   maintenir.
3. **Un seul runtime pour les trois harnais.** Le runner est un service local :
   Python est déjà présent partout où `agent-mail` v0 tourne (c'est le même
   interpréteur), et `psql` est présent partout où Postgres est utilisé.
4. **`psycopg` reste le chemin nominal** quand il est là (installé par IMA ou
   par un venv) : connexions directes, LISTEN/NOTIFY natif, pas de processus
   par requête. C'est un accélérateur, pas un prérequis.

L'alternative Node avait un avantage réel (le harnais Codex et `jq` sont déjà
dans l'écosystème npm), mais elle perd sur la compatibilité v0 et sur
l'absence de client Postgres stdlib.

---

## 2. Modèle de données (`0001_init.sql`, `0002_overview.sql`)

Migrations versionnées `NNNN_nom.sql`, appliquées une fois, empreinte SHA-256
enregistrée dans `schema_migrations`, verrou consultatif
(`pg_advisory_xact_lock`) pour que deux exécuteurs qui démarrent ensemble ne se
marchent pas dessus, et **immuabilité** : modifier une migration déjà appliquée
est une erreur, pas une mise à jour silencieuse (il faut en ajouter une).

### `agent_registry` — présence et bail

`name` (clé, même grammaire qu'`agent-mail` v0 : `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`),
`chantier`, `harness`, `host`, `cwd`, `session_id`, `status`
(`idle|queued|running|blocked|stopped|dead`), `status_text`, `model`,
`budget_usd`, `spent_usd`, `turns`, `pending_prompt`, `last_error`,
`lease_owner`, `lease_expires_at`, `lease_epoch`, `last_turn_at`, `last_seen`.

Deux points de conception :

* **`pending_prompt`** — l'équivalent v1 de `nexlink-agent start <nom> … "consigne"` :
  la consigne attend dans le registre, l'exécuteur la consomme atomiquement.
  Le `RETURNING` d'un `UPDATE` renvoie la *nouvelle* valeur (donc `NULL`) : la
  consommation passe par un CTE `taken … FOR UPDATE` qui capture l'ancienne
  valeur avant de l'effacer. (Bug attrapé par les tests.)
* **`upsert` partiel** — un `send`, un hook ou un `status` ne doivent pas
  écraser ce qu'ils ne connaissent pas. Les colonnes `chantier`, `harness`,
  `host` sont donc mises à jour par `coalesce(nullif(%s,''), colonne)`, et non
  par la valeur par défaut de la ligne proposée. (Deuxième bug attrapé par les
  tests : un `send` remettait `harness` à `'other'`.)

### `agent_mailbox` — messages durables

`sender`, `recipient`, `body`, `kind` (`request|reply|notify`), `payload jsonb`,
`work_item_id`, `status` (`pending|delivered|answered`), `created_at`,
`delivered_at`, `signature`, `signature_key`, `slack_ts`, `host`, `meta`.

C'est la liste de la spec §5.1 (`from, to, body, created, delivered,
signature`) enrichie des colonnes du design IMA (`kind`, `payload`,
`work_item_id`, `slack_ts`) pour que la même table serve aux transports
suivants (R6 : Nexlink, Slack). `sender`/`recipient` portent `from`/`to`, mots
réservés SQL.

Un message **non remis** est `delivered_at IS NULL` ; l'index partiel
`agent_mailbox_unread_idx (recipient, id) WHERE delivered_at IS NULL` sert la
requête chaude. La remise est faite par le hook du harnais (comme en v0) ou par
l'exécuteur à la fin d'un tour de messages réussi : un message n'est jamais
perdu si le tour échoue.

### Vue `agent_mesh_overview`

Une ligne par agent avec bail, non-lus et budget : c'est la base de
`agent-mail list` aujourd'hui et de `mesh list` (point 5) demain.

---

## 3. Baux d'agents (R3, R5)

Un agent est **épinglé à un hôte** (v1 : les sessions des harnais sont des
fichiers locaux). Le bail est la seule autorisation de tourner.

* **Prise** — `UPDATE agent_registry SET lease_owner=%s, lease_expires_at=now()+ttl,
  lease_epoch=lease_epoch+1 WHERE name=%s AND (lease_owner IS NULL OR
  lease_owner=%s OR lease_expires_at < now()) RETURNING …`. Sous READ COMMITTED,
  un `UPDATE … WHERE` réévalue la condition après le verrou de ligne : deux
  exécuteurs concurrents ne peuvent pas gagner le même agent. Testé avec 2 et 4
  connexions simultanées, puis avec **deux processus `agent-runner`**.
* **Fence temporel** — `renew`, `begin_turn` et `take_pending_prompt` exigent
  tous `lease_expires_at > now()` : un bail expiré ne se prolonge pas, ne
  démarre pas de tour et ne consomme pas de consigne. L'exécuteur doit
  re-`claim` (nouvel epoch) pour continuer ; `end_turn` reste fencé par
  `lease_owner` + `lease_epoch`, pour qu'un tour commencé sous bail valide
  puisse être enregistré.
* **Battement robuste** — une panne de base pendant un tour ne laisse pas le
  harnais tourner sans bail : le battement compte les échecs
  (`max(3, ttl/3)` secondes d'intervalle), et arrête le processus dès que la
  base reste injoignable ou que l'échéance connue du bail est atteinte. Il ne
  dort **jamais au-delà de l'échéance** (le réveil est borné par
  `lease_deadline`), même si l'intervalle nominal est plus long.
* **Arrêt du harnais** — `terminate` envoie SIGTERM, laisse 3 secondes, puis
  SIGKILL : un processus qui ignore SIGTERM ne survit pas à la perte du bail.
* **Consigne durable (R5)** — `pick` ne vide plus `pending_prompt` : la
  consigne est *déplacée* dans `current_prompt` quand le tour démarre, effacée
  par `end_turn` **uniquement si le tour a abouti**, et restaurée en attente
  dans tous les autres cas : tour en échec (y compris harnais tué), harnais
  absent, dossier disparu, changement de main — y compris quand le même
  `--runner-id` reprend un bail expiré. Si une consigne était déjà en attente
  pendant un tour interrompu, **les deux sont conservées** (l'ancienne puis la
  nouvelle, séparées par une ligne vide). Au pire un tour est rejoué, jamais
  une consigne perdue.
* **Expiration** — si l'exécuteur meurt (SIGKILL, coupure), le bail expire ;
  n'importe quel exécuteur du même hôte le reprend (`lease_expires_at < now()`).
  `reap()` marque `dead` les agents dont le bail a expiré en plein tour.
* **Perte en cours de tour** — le battement détecte la perte, termine le
  processus du harnais, et le tour n'est pas compté. Testé : vol de bail
  pendant un faux tour de 12 s, interruption en moins de 7 s.

---

## 4. LISTEN/NOTIFY : ce que `psql` impose

Le déclencheur `agent_mailbox_notify` émet
`pg_notify('agent_mail', {"to": …, "id": …})` à chaque insertion ;
`agent_registry_notify` fait de même sur les changements de bail/statut
(`agent_lease`). L'exécuteur écoute les deux canaux et réveille le worker
concerné ; le sondage (`--poll`, défaut 5 s) reste un filet de sécurité.

Avec `psycopg`, tout est direct. Avec `psql`, trois comportements de psql ont
été découverts **en test** et dictent l'implémentation :

1. `psql -c …` **ne lit jamais stdin** — un écouteur lancé avec `-c "LISTEN …"`
   puis `-c "SELECT pg_sleep(…)` ne peut pas rester interactif ; il faut une
   invocation sans `-c`, avec le `SET search_path` et les `LISTEN` envoyés sur
   stdin.
2. bloqué dans une requête (`pg_sleep`), psql **ne vide pas** les notifications :
   elles ne sortent qu'à la fin de la requête — un `pg_sleep` d'une heure ne
   réveille donc rien.
3. sur un pipe, psql ne traite pas les notifications du tout ; il faut un
   **pty**, et il n'imprime les notifications qu'au tour de boucle suivant.

L'écouteur `psql` est donc : un `psql` interactif sur un pty, un battement
`SELECT 1` deux fois par seconde, et un filtre sur la ligne
`Asynchronous notification "canal" with payload "…"`. Latence de réveil
≈ 0,5 s. Le détail est commenté dans `db._PsqlListener`, et un test
`doctor --notify-test` fait le tour complet.

Avec `psycopg`, `Connection.notifies(stop_after=1)` **jette** les notifications
non consommées à la fermeture du générateur : l'écouteur draine la connexion
dans un tampon (`notifies(timeout=0)`) et les rend une à une. Là encore, un
test (trois messages → trois notifications) a attrapé le bug.

---

## 5. Exécuteur (`agent-runner`)

* **Un par machine**, il réclame les agents `host = <hôte>`, renouvelle leurs
  baux et lance les tours. Mode service, ou `--once` pour le banc.
* **Adaptateurs** : lignes de commande reprises de `nexlink-agent.v0.sh`
  (`claude -p --output-format stream-json … --resume`, `codex exec --json
  resume`, `dsh --profile agent --json --session-id`), lecture JSONL,
  capture de la session et du coût quand le harnais le donne
  (`total_cost_usd` pour Claude, `cost_usd` pour DeepSeek).
* **Tours** : consigne en attente → messages non lus → relance d'inactivité
  (mêmes textes que la v0). Le flux brut est journalisé dans
  `~/.local/state/agent-mesh/<nom>/events.jsonl`, la session en base **et** sur
  disque (reprise après crash), stderr dans `stderr.log`.
* **Chemin de reprise** : la session est relue depuis le registre, sinon depuis
  le fichier `session`.
* **Binaire des harnais** : `AGENT_MESH_<HARNAIS>_BIN`, puis
  `AGENT_MESH_BIN_DIR/<nom>`, puis le PATH. C'est ce qui permet au banc
  d'utiliser de faux harnais sans jamais appeler les vrais.
* **Arrêt** : SIGTERM/SIGINT → les baux sont rendus (l'agent repart aussitôt
  ailleurs) ; SIGKILL → expiration naturelle.

---

## 6. CLI `agent-mail` compatible v0 et repli

Mêmes commandes, mêmes textes : `send`, `inbox`, `list`, `status`, `alias`,
`whoami`, `statusline`, `hook`, plus `migrate` et `doctor`. Les alias et
l'identité sont **partagés** avec la v0 (`~/.config/agent-mail/aliases.tsv`,
écriture atomique par `os.replace`).

Les hooks produisent le **même JSON** que la v0 (y compris la décision `Stop`
et le plafond de 3 relances, compté localement dans
`~/.local/state/agent-mesh/hooks/<nom>.stops`) : un test compare la sortie du
script v0 et celle de la v1 pour les quatre événements.

**Hooks : écrire d'abord, marquer ensuite.** Le JSON est écrit sur stdout et
vidé (`flush`) *avant* de marquer les messages remis ; si le harnais a fermé son
entrée (BrokenPipe), la remise n'a pas lieu et le courrier reste pour le tour
suivant.

**Repli** : si Postgres est injoignable et que `AGENT_MESH_BACKEND=auto`
(défaut), la CLI écrit dans la boîte fichier v0
(`~/.local/state/agent-mail`, mêmes noms de fichiers, même JSON) et affiche un
avertissement lisible sur stderr. En mode `pg`, elle échoue franchement. Un
schéma présent mais non migré n'est **pas** un repli silencieux : le message
dit de lancer `agent-mesh migrate`. Les hooks, eux, ne font jamais échouer
l'agent : toute erreur → sortie 0 sans rien.

---

## 7. Banc de test

`scripts/pg-up.sh` démarre `agent-mesh-pg` (`postgres:17`, port publié
uniquement sur `127.0.0.1:55432`, volume nommé). `scripts/test.sh` lance la
suite deux fois : pilote `psql`, puis pilote `psycopg` dans un venv local si
l'installation est possible.

63 tests `unittest` (stdlib), un schéma Postgres jetable et un état disque
jetable par classe : migrations, mailbox, notifications, baux (courses à 2 et
4 connexions, expiration, fencing), **deux exécuteurs concurrents qui se
disputent un bail** (processus réels, un seul tour, codes 0 et 3), réveil sur
NOTIFY en mode service avec sondage à 60 s (donc NOTIFY obligatoire), reprise
de session des trois harnais, égalité du JSON des hooks avec la v0, et repli
fichier.

Aucun test ne lance un vrai harnais : les binaires de `tests/fakebin/`
émettent les mêmes flux JSONL et journalisent leur argv. Aucun test ne touche
`~/.local/bin`, `~/.local/state/agent-mail` (hors répertoires temporaires) ni
les sessions réelles.

---

## 8. Limites assumées et suites

* **Aucun branchement sur les agents réels** cette nuit : c'est un banc. Le
  passage en production sur ce PC se fera en installant le paquet dans un venv
  (ou en lançant des wrappers `bin/`), en migrant le schéma `public`, puis en
  remplaçant `nexlink-agent` par `agent-runner` — outil par outil.
* **Un agent reste épinglé à son hôte** (v1) : déplacer une session est un
  chantier séparé (R1/R5).
* **L'autorité du propriétaire (R4)** et **`mesh list`** sont traités dans
  [`V1-AUTORITE-MESH.md`](V1-AUTORITE-MESH.md) (points 3 à 5), avec
  `signing.py`, `authority.py` et `mesh_cli.py`.
* **Budget** : `budget_usd` / `spent_usd` sont stockés et alimentés quand le
  harnais annonce un coût ; le contrôle (refuser un tour au-delà du budget)
  n'est pas implémenté.
* **IMA (M1)** : le paquet est autonome (`pyproject.toml`, aucun import d'IMA).
  Les tables et les colonnes `kind`/`payload`/`work_item_id` sont prêtes pour
  la reprise côté IMA ; il restera à décider qui possède `work_items`.
