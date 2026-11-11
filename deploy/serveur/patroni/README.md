# etcd + Patroni — préparation de la grappe PostgreSQL du mesh (EP8.4b)

Étude : [`docs/design/etudes/replique-base-mesh.md`](../../../docs/design/etudes/replique-base-mesh.md)
§4 « Failover: etcd + Patroni », §5, §7 et §8. Décision owner #207987 option a
(pas de quatrième votant). Lots : #6870 (cette préparation), #6861 (EP8.4,
geste réel), #6869 (EP8.4a, clients), #6862 (EP8.5, `db status`/alertes),
#6863 (EP8.6, bascule réelle). Issue : manaty/ameesh#5226.

Ce répertoire est une **préparation** : il produit les configurations, le
matériel TLS, les unités systemd, l'enveloppe `patronictl`, les alertes de
santé et ce runbook. **Aucun geste n'est fait sur un hôte réel par ce lot** ;
l'application est EP8.4. Il ne touche ni `installer-serveur.sh`, ni
`deploy/serveur/terraform/`, ni `sauvegarder-base.sh`.

## Topologie

Trois votants, un par VM ; deux voix suffisent pour le quorum etcd. Les
adresses privées sont celles d'EP8.2 (lot #6859,
`deploy/serveur/RESEAU-PRIVE.md`, livré avec ce lot) :

| Nœud | Adresse privée | Rôle | `failover_priority` | Tags Patroni |
|---|---|---|---|---|
| `ameesh-agents-1` | `10.78.0.11` | primaire préféré | 100 | `failover_priority: 100` |
| `ameesh-agents-2` | `10.78.0.12` | candidat primaire | 90 | `failover_priority: 90` |
| `manaty-mesh-1` | `10.78.0.10` | votant etcd, standby asynchrone | 0 (effectif) | `nofailover: true`, `nosync: true` |

- **Primaire préféré** : `ameesh-agents-1`, par la priorité Patroni la plus
  haute ; Patroni élit seul, aucun agent ne décide (canon §7).
- **Un seul synchrone** : `synchronous_mode: true`,
  `synchronous_node_count: 1`, `synchronous_mode_strict: false`. Le hub porte
  `nosync: true` : il n'entre jamais dans `synchronous_standby_names`, même
  quand un agent est arrêté. Le pair synchrone est l'autre VM d'agent.
- **Pas de bascule vers le hub** : `nofailover: true` et priorité effective 0.
- **`failsafe_mode: false`** : une perte du DCS met les écritures en pause au
  lieu de risquer un fork.
- **`use_pg_rewind: true`** et `use_slots: true` : l'ancien primaire revient
  par `pg_rewind`, sinon Patroni le réinitialise (`reinit`).
- **Chien de garde** : `watchdog.mode: 'off'`. Il n'est pas activé tant qu'il
  n'a pas été testé sur les VM (étude §4).

| Port | Écoute | Usage |
|---|---|---|
| 2379 | adresse privée seule | clients etcd (Patroni, `etcdctl`) |
| 2380 | adresse privée seule | pairs etcd |
| 8008 | adresse privée seule | API REST Patroni (TLS + authentification) |
| 5432 | `localhost` + adresse privée | PostgreSQL (TLS, `pg_hba` `hostssl`) |

Aucun port public nouveau. Les certificats du matériel TLS ne sont pas des
certificats PostgreSQL : Postgres reste en `sslmode=require` avec le
certificat snakeoil de Debian, comme l'étude §8 le prévoit ; une CA du mesh
pour `verify-full` est une suite.

## Fichiers

| Fichier | Rôle |
|---|---|
| `topologie.json` | source unique : noms, adresses, priorités, tags, ports, chemins, seuils |
| `modeles/etcd.yml.in` | modèle etcd 3.5 (trois membres, TLS pair et client) |
| `modeles/patroni.yml.in` | modèle Patroni 4.x (DCS, synchrone, `pg_rewind`, slots, priorités) |
| `generer-configuration.py` | rend les modèles par nœud ; `--liste-noeuds`, `--verifier` |
| `valider-modeles.py` | validation hors ligne avec le schéma de Patroni (sondes neutralisées) |
| `generer-tls.sh` | CA, secrets, certificats etcd/REST par nœud, empreintes ; hors dépôt |
| `etcd.service` | unité etcd 3.5 (`/usr/local/bin/etcd`, TLS) |
| `patroni.service` | unité Patroni (`/opt/patroni/venv`, environnement des secrets) |
| `patronictl` | enveloppe `patronictl` (configuration + secrets) |
| `sante-base.py` | collecteur de santé et alertes `db_*` |
| `ameesh-db-sante.service` / `.timer` | contrôle toutes les minutes, journal et `/run/ameesh-db/alertes.json` |
| `requirements.txt` | dépendances du venv Patroni (4.x + psycopg) |

`generer-tls.sh` n'écrit jamais dans le dépôt : il exige `--sortie`. Un
`.gitignore` local refuse les clés, certificats, `secrets.env` et `patroni.env`
créés par erreur dans le répertoire.

## Prérequis (état EP8.1–EP8.3)

1. **Réseau privé** EP8.2 : chaque nœud a son adresse `10.78.0.x`, les trois
   se joignent, aucun port base public.
2. **PostgreSQL 17** EP8.3 : rôles `ameesh`, `replicator` et `rewind` ou
   `postgres` local, `pg_hba` `hostssl`, `wal_log_hints=on`, `archive_mode=on`
   et l'archivage EP8.1 (pgBackRest). Le présent lot ne crée ni rôle ni
   migration.
3. **etcd 3.5.x** : binaire officiel `/usr/local/bin/etcd` et `etcdctl`.
   Version validée pour ce lot : **3.5.17**, empreinte du tar.gz
   `eff6ac621d41711085d0f38fab17d8fa3705f6326c3ff11301a1f5a71fc94edd`.
   Le paquet de la distribution est en 3.4.x : ne pas l'utiliser.
4. **Patroni 4.x** dans `/opt/patroni/venv` :
   `python3 -m venv /opt/patroni/venv && /opt/patroni/venv/bin/pip install -r requirements.txt`.
   Version validée pour ce lot : **4.1.5**.
5. **PostgreSQL 17 arrêté par Patroni** : l'unité Debian
   `postgresql@17-main` ne doit pas rester active en même temps que Patroni
   (deux gestionnaires du même `data_dir`). Le basculement est un geste
   EP8.4, avec retour arrière.

## Générer et vérifier hors ligne

Tout se fait sur un poste d'administration, sans root, et ne touche aucun
hôte :

```bash
cd deploy/serveur/patroni

# 1. Matériel TLS + secrets de la grappe (clés 0600), hors du dépôt.
./generer-tls.sh --sortie "$HOME/grappe-ameesh"

# 2. Configurations des trois nœuds, mots de passe injectés dans patroni.env.
./generer-configuration.py --secrets "$HOME/grappe-ameesh/secrets.env" \
    --sortie "$HOME/grappe-ameesh/config" --tous --verifier

# 3. Validation Patroni seule, si le venv est sur ce poste.
PATRONI_PYTHON=/opt/patroni/venv/bin/python \
  ./generer-configuration.py --secrets "$HOME/grappe-ameesh/secrets.env" \
    --sortie "$HOME/grappe-ameesh/config" --tous --verifier

# 4. Contrôle d'intégrité du matériel TLS.
( cd "$HOME/grappe-ameesh" && sha256sum -c empreintes.sha256 )

# 5. Syntaxe des unités, hors nœud : les ExecStart sont remplacés par true.
D=$(mktemp -d)
for u in etcd.service patroni.service ameesh-db-sante.service ameesh-db-sante.timer; do
  sed -e 's|^ExecStart=.*|ExecStart=/bin/true|' \
      -e 's|^EnvironmentFile=.*|EnvironmentFile=-/dev/null|' "$u" > "$D/$u"
done
systemd-analyze verify "$D"/*.service "$D"/*.timer
```

`generer-configuration.py --verifier` fait deux choses :

- il vérifie les invariants de la grappe (trois noms uniques, adresses dans le
  réseau privé, exactement un nœud `nosync`, priorité maximale unique et
  primaire préféré, deux candidats primaires, deux candidats synchrones,
  aucun secret dans les YAML, écoutes privées seulement) ;
- si le module Patroni est joignable (`PATRONI_PYTHON`, `/opt/patroni/venv` ou
  l'interpréteur courant), il appelle `valider-modeles.py`, qui utilise le
  schéma de Patroni — le même code que `patroni --validate-config` — avec les
  sondes réseau neutralisées. Patroni absent n'est pas un échec : la
  validation est alors déclarée structurelle (canon §3).

Tests unitaires (aucun réseau, aucune base, aucun root) :

```bash
PYTHONPATH=src python3 -m unittest tests.test_6870_patroni -v
```

## Installation sur un nœud (geste EP8.4, pas ce lot)

L'ordre est important : TLS, etcd, puis Patroni. Chaque étape est rejouable.
Un exemple pour `ameesh-agents-1` ; remplacer le nom, l'adresse et le groupe
pour les deux autres.

1. **Matériel TLS et secrets** (depuis le poste d'administration). Ne copier
   que `ca.crt`, `secrets.env` et le dossier du nœud ; **jamais `ca.key`**.

   ```bash
   install -d -m 0755 /etc/ameesh/tls
   install -m 0644 "$HOME/grappe-ameesh/ca.crt" /etc/ameesh/tls/ca.crt
   install -d -m 0750 -o root -g postgres /etc/patroni/tls
   install -m 0640 -o root -g postgres \
     "$HOME/grappe-ameesh/ameesh-agents-1/etcd-client.crt" \
     "$HOME/grappe-ameesh/ameesh-agents-1/etcd-client.key" \
     "$HOME/grappe-ameesh/ameesh-agents-1/patroni-rest.crt" \
     "$HOME/grappe-ameesh/ameesh-agents-1/patroni-rest.key" \
     /etc/patroni/tls/
   install -d -m 0750 -o root -g etcd /etc/etcd/tls
   install -m 0640 -o root -g etcd \
     "$HOME/grappe-ameesh/ameesh-agents-1/etcd-server.crt" \
     "$HOME/grappe-ameesh/ameesh-agents-1/etcd-server.key" \
     /etc/etcd/tls/
   install -m 0640 -o root -g postgres \
     "$HOME/grappe-ameesh/config/ameesh-agents-1/patroni.env" \
     /etc/ameesh/patroni.env
   ```

   L'utilisateur `etcd` est système, sans login :
   `useradd --system --home /var/lib/etcd --shell /usr/sbin/nologin etcd`
   (idempotent : `id etcd` d'abord). Le groupe `postgres` existe avec
   PostgreSQL.

2. **Configurations** :

   ```bash
   install -m 0640 -o root -g etcd \
     "$HOME/grappe-ameesh/config/ameesh-agents-1/etcd.yml" /etc/etcd/etcd.conf.yml
   install -d -m 0750 -o root -g postgres /etc/patroni
   install -m 0640 -o root -g postgres \
     "$HOME/grappe-ameesh/config/ameesh-agents-1/patroni.yml" /etc/patroni/patroni.yml
   install -m 0755 ./etcd.service ./patroni.service \
     ./ameesh-db-sante.service ./ameesh-db-sante.timer /etc/systemd/system/
   install -m 0755 ./patronictl /usr/local/bin/patronictl
   install -m 0755 ./sante-base.py /usr/local/sbin/ameesh-db-sante
   systemctl daemon-reload
   ```

3. **etcd** : sur les trois nœuds, dans la même fenêtre (un cluster neuf se
   forme avec `initial-cluster-state: new`) :

   ```bash
   systemctl enable --now etcd.service
   etcdctl --endpoints https://10.78.0.11:2379 \
     --cacert /etc/ameesh/tls/ca.crt \
     --cert /etc/patroni/tls/etcd-client.crt \
     --key /etc/patroni/tls/etcd-client.key endpoint health --cluster
   ```

   Le quorum est atteint quand les trois (ou au moins deux) répondent
   `is healthy`. Un troisième nœud qui arrive après la formation du cluster
   doit avoir `initial-cluster-state: existing` : c'est le chemin de
   remplacement d'un membre, pas le démarrage initial.

4. **Patroni** : arrêter d'abord l'unité Debian sur le nœud qui porte la base
   (`systemctl disable --now postgresql@17-main`), puis démarrer Patroni sur
   **un seul** nœud pour amorcer la grappe :

   ```bash
   systemctl enable --now patroni.service
   journalctl -u patroni.service -n 80 --no-pager
   /usr/local/bin/patronictl list
   ```

   Le premier nœud crée la grappe si `data_dir` est vide (`bootstrap.initdb`) ;
   avec une base existante EP8.3, Patroni reprend le `data_dir` et les autres
   nœuds le rejoignent par `pg_basebackup`. Démarrer ensuite Patroni sur les
   deux autres nœuds, un par un, et vérifier :

   ```bash
   /usr/local/bin/patronictl list
   # ameesh-agents-1  Leader  running  ...  sync_state=sync pour l'autre agent
   # manaty-mesh-1    Replica streaming ... sync_state=async
   /usr/local/bin/patronictl topology
   ```

5. **Santé** :

   ```bash
   systemctl enable --now ameesh-db-sante.timer
   systemctl start ameesh-db-sante.service
   journalctl -u ameesh-db-sante.service -n 20 --no-pager
   cat /run/ameesh-db/alertes.json 2>/dev/null || true
   ```

## Santé et alertes

`sante-base.py` sonde, dans cet ordre : l'API REST Patroni (`/cluster`), les
trois membres etcd (`etcdctl endpoint health --cluster`), le disque du
`data_dir` (`statvfs`) et `pg_stat_archiver` (si `archive_mode=on`). Il écrit
un JSON dans le journal et, s'il y a des alertes, `/run/ameesh-db/alertes.json`
pour le chemin de notification d'EP8.5.

| Alerte | Sévérité | Seuil par défaut | Source |
|---|---|---|---|
| `db_no_primary` | critique | aucun leader | REST Patroni |
| `db_failover` | avertissement | changement de leader (une fois) | état local |
| `db_replication_lag` | avertissement | 16 Mio | `/cluster`, membre par membre |
| `db_etcd_no_quorum` | critique | moins de 2 membres sains | `etcdctl` |
| `db_disk_low` | critique | 5 Gio ou 10 % libres | `statvfs` |
| `db_archive_stale` | avertissement | 900 s sans WAL archivé, ou dernier échec plus récent | `pg_stat_archiver` |

Les seuils sont dans `topologie.json` (`sante`) et recopiés dans chaque
`patroni.env` ; on peut les surcharger en ligne de commande
(`--seuil-retard-octets`, `--seuil-disque-gio`, `--seuil-disque-pct`,
`--seuil-archive-s`). Une observation ratée produit une alerte, pas un échec
silencieux : `db_no_primary` ou `db_etcd_no_quorum` nomment la sonde et
l'erreur. Le service sort 0 même avec des alertes ; `--strict` sort 1 pour un
usage manuel ou un test.

Modèle d'observation hors ligne (`--lecture`) : les clés `patroni`, `etcd`,
`disque`, `archivage` et `erreurs` ; voir les fixtures de
`tests/test_6870_patroni.py`. L'état local ne garde que le dernier leader et
la date du dernier failover (`AMEESH_DB_ETAT`, par défaut
`/var/lib/ameesh-db/sante.json`).

**Branchement à ameesh** : EP8.5 (#6862) ajoute `ameesh db status`, la ligne
`db` de `ameesh doctor` et les alertes `db_*` dans `ameesh alerts`, puis
`ameesh-notify` les pousse à l'owner avec le PC éteint (veille sur une VM).
Le présent lot fournit le collecteur, pas ce branchement.

## Runbook d'exploitation (EP8.4/EP8.6)

Toutes les commandes passent par l'enveloppe, qui lit `/etc/patroni/patroni.yml`
et exporte les secrets de `/etc/ameesh/patroni.env`.

```bash
patronictl list
patronictl topology
patronictl history
patronictl show-config
```

### Bascule planifiée

```bash
# Choisir une cible saine (pas le hub nosync) et vérifier le retard.
patronictl list
patronictl switchover --leader ameesh-agents-1 \
  --candidate ameesh-agents-2 --force
patronictl list
```

Aucun agent ne décide : `patronictl` demande, Patroni exécute. Vérifier
`sync_state` après la bascule ; le hub reste `async`.

### Perte du primaire

Patroni détecte, élit le candidat de plus haute priorité sain et promeut en
quelques dizaines de secondes (cible < 1 min). Ne pas lancer de
`failover` manuel pendant ce temps : vérifier `patronictl list`, l'alerte
`db_failover`, puis le retour de l'ancien primaire. S'il ne revient pas seul :

```bash
patronictl reinit ameesh <ancien-primaire>
```

`reinit` réinitialise le nœud par `pg_basebackup` : c'est le chemin après un
échec de `pg_rewind`, et il est tracé dans le journal Patroni. Ne jamais
recopier à la main un `data_dir` vivant.

### Remplacement d'un membre etcd

Un membre etcd ne se remplace pas à chaud en réutilisant son `data_dir` :

```bash
etcdctl --endpoints https://10.78.0.11:2379 \
  --cacert /etc/ameesh/tls/ca.crt \
  --cert /etc/patroni/tls/etcd-client.crt \
  --key /etc/patroni/tls/etcd-client.key \
  member list
etcdctl ... member remove <ID>
```

Sur le nœud neuf : générer son matériel TLS (même CA), vider son
`/var/lib/etcd/ameesh`, passer temporairement `initial-cluster-state` à
`existing` dans son `etcd.yml` avec `initial-cluster` à la liste des trois
membres (le nouveau nom et la même adresse), démarrer etcd, puis
`member add` si nécessaire. Vérifier `endpoint health --cluster` à chaque
étape : le quorum ne doit jamais descendre sous 2.

### Perte du DCS (etcd)

Avec `failsafe_mode: false`, Patroni met les écritures en pause quand le DCS
n'est plus joignable ; il ne promeut pas. Vérifier le quorum, rétablir deux
membres etcd, puis `patronictl list`. Ne pas couper `failsafe_mode` pour
« débloquer » : c'est le comportement qui évite le fork.

### Disque plein

`db_disk_low` et `host_disk_soon_full` se complètent. Sur le primaire :
purger les WAL archivés (`pg_archivecleanup` seulement après vérification de
l'archive), les sauvegardes locales selon la rétention, puis
`VACUUM (VERBOSE, ANALYZE)` si les tables mortes sont la cause. Un slot de
réplication non consommé peut remplir le disque : `pg_replication_slots`.
Ne jamais supprimer un WAL non archivé.

### Suspicion de split brain

Deux primaires ne peuvent pas être élus tant que le quorum etcd est unique.
En cas de doute : `patronictl list` sur les trois API, puis
`etcdctl endpoint health --cluster`. Si deux nœuds se disent primaires,
arrêter Patroni sur les deux, vérifier les timelines
(`pg_controldata`), garder la timeline la plus récente avec les WAL archivés,
puis `reinit` l'autre. Ne pas restaurer à l'aveugle.

### Retour arrière vers l'unité Debian

1. `systemctl disable --now patroni.service` sur les trois nœuds.
2. Sur le nœud qui doit servir la base : restaurer la configuration Debian
   d'avant Patroni. Patroni a renommé `postgresql.conf` en
   `postgresql.base.conf` et écrit son propre `postgresql.conf` ; remettre le
   fichier d'origine (sauvegarde prise avant l'installation) et le
   `pg_hba.conf` d'origine.
3. `systemctl enable --now postgresql@17-main`, vérifier `pg_isready`.
4. `systemctl disable --now etcd.service` sur les trois nœuds.
5. Les clients repassent sur le DSN mono-hôte ; c'est le retour d'EP8.4, pas
   une opération de ce lot.

## Sécurité

| Chemin | Mode | Propriétaire |
|---|---|---|
| `/etc/ameesh/tls/ca.crt` | 0644 | root:root |
| `ca.key` (hors nœuds) | 0600 | administrateur |
| `/etc/etcd/etcd.conf.yml` | 0640 | root:etcd |
| `/etc/etcd/tls/etcd-server.{crt,key}` | 0640 | root:etcd |
| `/etc/patroni/patroni.yml` | 0640 | root:postgres |
| `/etc/patroni/tls/patroni-rest.{crt,key}` | 0640 | root:postgres |
| `/etc/patroni/tls/etcd-client.{crt,key}` | 0640 | root:postgres |
| `/etc/ameesh/patroni.env` | 0640 | root:postgres |
| `/var/lib/etcd` | 0700 | etcd:etcd |

- etcd : TLS pair et client, authentification par certificat client, écoute
  sur l'adresse privée seule. Aucun port public.
- Patroni REST : TLS signé par la CA de la grappe, authentification
  utilisateur/mot de passe ; `ctl.cacert` vérifie le serveur.
- PostgreSQL : `pg_hba` réécrit par Patroni avec `hostssl` + `scram-sha-256`
  sur le réseau privé et l'adresse du PC, `local … peer` pour Postgres, aucun
  `trust`.
- Aucun secret dans le dépôt : les mots de passe sont générés par
  `generer-tls.sh`, recopiés dans `patroni.env` et lus par Patroni via
  `PATRONI_*`. La clé de la CA ne doit jamais être copiée sur un nœud.
- Le matériel TLS est daté : CA 10 ans, certificats 825 jours par défaut
  (`--jours`, `--jours-ca`). La rotation est un geste à planifier avec EP8.6.
- `journalctl -u patroni` peut contenir des noms d'hôtes, pas de mots de
  passe ; `patroni.yml` n'en contient aucun.

## Limites et suites

- Ce lot **ne crée aucun rôle, aucune migration, aucun cluster** : il prépare
  les fichiers et les tests. EP8.4 (#6861) fait le geste réel, l'intégration
  et la preuve de bascule sur staging (§14).
- Les clients (DSN multi-hôtes, `target_session_attrs=read-write`,
  reconnexion psycopg, écouteur LISTEN/NOTIFY et balayage après bascule) sont
  EP8.4a (#6869), pas ce lot. Le DSN documenté dans l'étude §5 suppose les
  adresses de ce répertoire.
- `ameesh db status`, `ameesh doctor`, les alertes `db_*` dans `ameesh alerts`
  et `sauvegarder-base.sh` sont EP8.5 (#6862). Ici, `sante-base.py` est le
  collecteur local, pas la commande ameesh.
- Le chien de garde matériel, le test de `pg_rewind` réel, la bascule réelle
  et les runbooks en production sont EP8.4/EP8.6, sur staging.
- `installer-serveur.sh`, `deploy/serveur/terraform/` et
  `sauvegarder-base.sh` ne sont pas modifiés : leur intégration appartient à
  EP8.4, avec revue de la classe sensible.

## Dépannage

- **`patronictl` : configuration illisible** : vérifier
  `/etc/patroni/patroni.yml` (0640 root:postgres) et
  `/etc/ameesh/patroni.env` (0640 root:postgres) ; l'enveloppe refuse de
  deviner.
- **`etcd` ne démarre pas** : `journalctl -u etcd` ; les erreurs typiques sont
  un certificat absent (le `--force` de `generer-tls.sh` a changé la CA sans
  redistribuer les certificats), un `data-dir` appartenant au mauvais
  utilisateur, ou un `initial-cluster` qui ne correspond pas aux trois noms.
- **Patroni ne démarre pas** : `journalctl -u patroni` ; vérifier
  `/usr/lib/postgresql/17/bin` (binaires présents), les droits de
  `/etc/postgresql/17/main` pour `postgres`, et que `postgresql@17-main`
  n'est pas déjà actif.
- **Pas de synchrone** : `patronictl list` ; l'autre agent doit être
  `streaming` et non `nosync`. Le hub ne doit jamais apparaître en `sync`.
- **Alerte `db_archive_stale` alors qu'EP8.1 n'est pas actif** : le collecteur
  ne lit `pg_stat_archiver` que si `archive_mode=on` ; vérifier
  `SHOW archive_mode`. Sans archivage, l'alerte est silencieuse.

## Références

- Étude : `docs/design/etudes/replique-base-mesh.md` §4, §5, §7, §8.
- Réseau privé : `deploy/serveur/RESEAU-PRIVE.md` (EP8.2, lot #6859 ; livré
  avec ce lot, absent de `main` tant qu'il n'est pas fusionné).
- Archivage : `deploy/serveur/pgbackrest/README.md` (EP8.1, lot #6858 ; même
  remarque).
- Patroni : <https://patroni.readthedocs.io/>, etcd :
  <https://etcd.io/docs/v3.5/>, PostgreSQL 17 :
  <https://www.postgresql.org/docs/17/warm-standby.html>.
