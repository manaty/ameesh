# ameesh-pg — le banc Postgres local et son journal (#7962)

`ameesh-pg` est le banc Postgres **local** d'un hôte d'agents : la base des
tests, pas la base du mesh. Il est créé par `scripts/pg-up.sh`, visé par
`scripts/test.sh` et le `Makefile` sur `127.0.0.1:55432`, et son journal
`json-file` n'avait aucun plafond (1,24 Go en 7 jours sur AG2). Ce document
décrit l'objet, son journal, le plafond posé sans redémarrage, et les
`log-opts` de la prochaine recréation.

## Ce que c'est

Relevé sur AG2 le 20/11/2026, avant tout geste :

| | |
|---|---|
| Conteneur | `ameesh-pg`, id `a4b96f4333581860b39710a99d3b3d0fd901db3ecf0c34ac845be4db15887cc6` |
| Image | `postgres:17` (`sha256:2d2b8998d310…`) |
| Création | 2026-11-04T07:09:33Z ; démarré 2026-11-13T16:31:58Z (`Up 7 days`, healthy) |
| Volume | `ameesh-pg-data` → `/var/lib/postgresql/data` (3,485 Go le 20/11) |
| Port | `127.0.0.1:55432` → `5432/tcp` (boucle locale seulement) |
| Redémarrage | `unless-stopped` |
| Étiquette | `ameesh.operator=5495` |
| Journal | `LogConfig` = `json-file`, `Config` = `{}` : **aucun `max-size`** |

L'instance d'AG2 n'a pas `--init` (le script courant le pose depuis #3833) et
son étiquette `ameesh.operator` n'est posée par aucun fichier du dépôt : elle
a été créée à la main, avec les réglages d'autovacuum et le healthcheck du
script. C'est cette pose manuelle qui a laissé le journal sans plafond.

Ce conteneur **n'est pas la base du mesh** : `AMEESH_DSN` et
`AGENT_MESH_DSN` des exécuteurs visent le socket PgBouncer local (port 6432),
qui relaie vers la base centrale. `ameesh-pg` ne sert qu'au banc de tests.
`scripts/test-parallele.sh` crée ses propres conteneurs `ameesh-pg-part-*` :
ils ne dépendent pas de `ameesh-pg` et ne sont pas visés par ce plafond.

## Qui s'en sert

- `scripts/pg-up.sh` le crée (ou le démarre) ;
- `scripts/test.sh`, le `Makefile` et `scripts/demo-v1.sh` le visent par
  défaut (`postgresql://agent_mesh@127.0.0.1:55432/agent_mesh`), avec
  `AMEESH_TEST_DSN` / `AMEESH_DSN` pour le remplacer ;
- les suites de tests y créent des bases jetables `agent_mesh_test_*` ;
- `~/.pgpass` porte une entrée `127.0.0.1:55432` (le mot de passe n'est
  jamais versionné) ; sans elle, les tests Pg échouent en `fe_sendauth`
  (#4065), voir `docs/DEPLOIEMENT-HOTE-AGENTS.md`.

## Le journal json-file

Emplacement, donné par `docker inspect` :

```text
~/.local/share/docker/containers/<id>/<id>-json.log
```

Relevé du 20/11/2026 :

- **1 243 739 484 octets** (1,24 Go) avant la troncature manuelle du bureau à
  17:50Z ; le conteneur n'a été ni arrêté ni recréé ;
- la fin a été gardée par le bureau dans
  `~ameesh/ameesh-pg-json-tail-20261120.log.gz` (0600, 4,1 Mo compressés) ;
- cette preuve couvre 07:09:55 → 17:40:09Z, soit **328 467 enregistrements**
  en 10 h 30 (~60 Mo, ~137 Mo/jour), dont **2 869 `ERROR` + `STATEMENT`** ;
- l'essentiel du volume n'est pas la charge normale de Postgres : ce sont les
  erreurs **volontaires** des tests négatifs, chacune suivie du contexte SQL
  multiligne de la fonction testée. Chaque ligne de ce contexte devient un
  enregistrement json distinct.

Baisser le niveau de log masquerait ces erreurs de test ; le plafond porte
donc sur le **fichier**, pas sur ce que Postgres journalise.

## Plafond sans redémarrage

Le pilote `json-file` écrit en `O_APPEND`. La troncature externe est donc
sûre : la prochaine écriture repart à l'offset 0 du **même inode**, sans trou
ni erreur. Preuve faite sur AG2 le 20/11/2026, pas seulement déduite :

- dockerd rootless (pid 3015992) tient le journal **fd 22** ;
- `/proc/3015992/fdinfo/22` donne `flags: 02102001`, soit
  `O_WRONLY|O_LARGEFILE|O_APPEND|O_CLOEXEC` ;
- après le `truncate` de 17:50Z, la première écriture suivante (une `FATAL`
  de base absente, 19:09Z) est allée à l'**offset 162**, inode 2420525 ;
- `docker logs ameesh-pg` (sans `--tail`) relit la nouvelle ligne normalement.

### Ce que la troncature casse, et pourquoi on l'accepte

Docker garde en mémoire la taille du journal au moment de la création du
lecteur. Après une troncature externe, `docker logs --tail N` et
`docker logs -f` peuvent donc attendre indéfiniment à l'ancien offset (moby
[#45728](https://github.com/moby/moby/issues/45728), fermé comme connu et
attendu) ; c'est vrai depuis la troncature du bureau à 17:50Z, pas seulement
depuis cette minuterie. En revanche :

- `docker logs ameesh-pg` (sans `--tail`) et `docker logs --since <date>`
  lisent le fichier courant et fonctionnent ;
- l'archive gzip de la passe contient la fin complète, lisible par `zcat` ;
- le blocage disparaît à la prochaine recréation du conteneur, qui naîtra avec
  les `log-opts` natifs (rotation Docker, sans troncature externe).

C'est le prix explicite du « sans redémarrage » de #7962 : Postgres n'est
jamais arrêté pour réparer un confort de lecture. Pour lire le journal d'un
banc qui vient d'être tronqué, utiliser `--since` ou l'archive.

### La minuterie

`scripts/plafond-journal-docker.py` et les unités utilisateur
`deploy/systemd/ameesh-pg-journal.{service,timer}` :

- seuil **128 Mio** ; au-delà, la fin (**8 Mio** par défaut) est copiée,
  compressée en `0600` dans `~/.local/state/ameesh/journal-conteneur/`, puis
  le journal est tronqué à zéro ;
- **8 archives** gardées (rotation par nombre, noms horodatés) ;
- passe **toutes les 10 minutes** après la précédente, et 5 minutes après le
  démarrage ; une passe ordinaire ne fait qu'un `stat` ;
- plafond effectif ≈ 128 Mio + la croissance de 10 minutes (~1 Mo au débit
  mesuré), archives ≈ 10–20 Mo ;
- verrou `flock` : une seule passe tronque à la fois ; aucun arrêt, aucun
  redémarrage, aucune recréation, aucun accès au volume ni à la configuration
  de Postgres.

Installation sur un hôte d'agents, sous le compte `ameesh`, sans root :

```sh
install -D -m 0755 scripts/plafond-journal-docker.py \
    ~/.local/bin/plafond-journal-docker.py
install -D -m 0644 deploy/systemd/ameesh-pg-journal.service \
    deploy/systemd/ameesh-pg-journal.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ameesh-pg-journal.timer
```

Contrôle :

```sh
systemctl --user list-timers ameesh-pg-journal.timer
systemctl --user start ameesh-pg-journal.service   # une passe immédiate
journalctl --user -u ameesh-pg-journal.service -n 20 --no-pager
~/.local/bin/plafond-journal-docker.py --etat
~/.local/bin/plafond-journal-docker.py --etat --json
~/.local/bin/plafond-journal-docker.py --dry-run --verbose
```

`--etat` est en lecture seule : il affiche le type de pilote, le chemin, la
taille, le seuil, le nombre et le poids des archives. `--dry-run` dit ce qui
serait fait sans rien copier ni tronquer. Le script refuse un journal hors de
`<DockerRootDir>/containers/<id>/`, un lien symbolique, un fichier remplacé
entre-temps, ou un pilote autre que `json-file` (il l'ignore alors, sans
erreur).

## Prochaine recréation : les log-opts

Le plafond de la minuterie borne le conteneur **existant**. Pour les
suivants, deux endroits sont posés :

1. `scripts/pg-up.sh` passe désormais à la création :

   ```text
   --log-driver json-file --log-opt max-size=64m --log-opt max-file=3
   ```

   Tout banc neuf est donc borné à 3 × 64 Mio, avec rotation native du pilote.

2. Sur AG2, `~/.config/docker/daemon.json` (rootless, `0600`) a reçu le
   défaut d'hôte :

   ```json
   "log-opts": {"max-size": "64m", "max-file": "3"}
   ```

   Le fichier a été validé par
   `dockerd --validate --config-file <candidat>` ; **le démon n'a pas été
   relancé** le 20/11. Ces valeurs s'appliqueront aux conteneurs créés après
   le prochain démarrage de Docker, pas au conteneur existant (Docker fige
   `LogConfig` à la création). La minuterie couvre l'existant d'ici là.

Après une recréation, vérifier la configuration effective :

```sh
docker inspect ameesh-pg --format '{{json .HostConfig.LogConfig}}'
# {"Type":"json-file","Config":{"max-file":"3","max-size":"64m"}}
```

et que le journal est bien sous `~/.local/share/docker/containers/<id>/`.
La minuterie reste utile : elle couvre un conteneur recréé sans les opts (par
exemple à la main), et son `--etat` sert de diagnostic.

## Limites

- Le plafond est propre au pilote `json-file` ; un autre pilote est ignoré.
- La copie de la fin est une photo : les quelques écritures arrivées entre la
  lecture et la troncature ne sont pas dans l'archive. Sur ce banc, quelques
  lignes au plus, sans effet sur Postgres.
- Le plafond ne réduit pas le volume produit par les tests négatifs ; il
  l'empêche seulement de remplir le disque. Réduire ce volume demanderait de
  revoir les tests ou leur niveau de log, pas cette minuterie.
- `docker logs --tail`/`-f` peut se bloquer après une troncature externe
  ([moby #45728](https://github.com/moby/moby/issues/45728)) ; utiliser
  `docker logs --since` ou l'archive gzip. Le prochain conteneur recréé avec
  les `log-opts` n'a plus ce défaut.
- `daemon.json` n'agit qu'au prochain démarrage du démon ; ne pas le
  redémarrer pour « appliquer » le plafond, la minuterie suffit.

## Références

- lot #7962 (ce plafond), suite de #7948 (disque d'AG2) ; #3833 (`--init` du
  banc), #5495 (étiquette de l'instance manuelle) ;
- `scripts/pg-up.sh`, `scripts/plafond-journal-docker.py`,
  `deploy/systemd/ameesh-pg-journal.{service,timer}`,
  `tests/test_7962_plafond_journal.py` ;
- `docs/DOCKER-STORAGE.md` (volumes et ménage Docker, pas les journaux),
  `docs/DEPLOIEMENT-HOTE-AGENTS.md` (banc et `~/.pgpass`).
