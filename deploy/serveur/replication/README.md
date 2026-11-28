# Réplication en flux PostgreSQL 17 du mesh (lots #6868 EP8.3a et #8027)

Scripts de **préparation** de la réplication en flux à trois nœuds décrite par
l'étude [replique-base-mesh.md](../../../docs/design/etudes/replique-base-mesh.md)
(§2 « trois nœuds » et §3 « réplication »). Ce lot ne joue **aucun geste sur un
hôte réel** : il livre les scripts idempotents, leurs témoins sur des faux et
cette notice. Le geste réel, avec ses fenêtres et son retour arrière, est le lot
EP8.3 (#6860). Le réseau privé est #6859, l'archivage #6858 : ni l'un ni l'autre
n'est traité ici, et `installer-serveur.sh`, `terraform` et
`sauvegarder-base.sh` ne sont pas modifiés.

Le lot **#8027** (décision owner de veille du mesh) fixe le primaire sur le
**hub** : au repos, seul `manaty-mesh-1` reste allumé et doit servir la base.
La bascule d'EP8.3 vers `ameesh-agents-1` n'est donc plus la cible ; les
agents sont les standbys synchrones du hub, et une promotion d'agent n'est
qu'une **bascule de secours**. Le mode veille (`mode-veille.sh`) vide la liste
synchrone avant l'arrêt des agents et la rétablit au réveil.

## Cible

Trois nœuds PostgreSQL 17, sans VM nouvelle (étude §2) :

| Nœud | Inventaire | Rôle effectif | Flux |
|---|---|---|---|
| `manaty-mesh-1` | `primaire`, **nofailover**, priorité 0 | primaire permanent, hub WireGuard | sert les clients ; seul allumé en veille |
| `ameesh-agents-1` | `standby`, synchrone, priorité 100 | standby synchrone | `quorum` (l'un des deux) |
| `ameesh-agents-2` | `standby`, synchrone, priorité 90 | standby synchrone | `quorum` (l'un des deux) |

L'inventaire [`noeuds.conf`](noeuds.conf) porte ces valeurs ; les scripts n'ont
aucun nom de nœud en dur. Le hub est le primaire permanent ; un agent ne
devient primaire que par une bascule de secours (le hub ne revient qu'au
retour planifié, `--retour-nofailover`). Les scripts lisent aussi le **rôle
effectif** du serveur (`pg_is_in_recovery()`), pas seulement l'inventaire,
pour poser les rôles et les slots sur celui qui sert.

Règles encodées :

- `synchronous_commit = on` et, **seulement quand un candidat est déjà en
  flux**, `synchronous_standby_names = 'ANY 1 (...)'` avec les deux VM
  d'agents : un seul synchrone, `ANY 1` choisit celui des deux qui est
  connecté ; le primaire (le hub) n'est jamais candidat — il n'est pas son
  propre standby. À l'amorçage (aucun standby), la liste reste **vide** : sans
  Patroni, une liste non vide sans standby connecté ferait attendre chaque
  `COMMIT` indéfiniment (B1 de revue #6868) ;
- en **mode veille** (#8027), la liste reste vide même si un candidat est
  encore en flux : c'est la condition pour arrêter les agents sans bloquer un
  `COMMIT` ; `mode-veille.sh sortir` la rétablit après le rattrapage ;
- `wal_log_hints = on` (pg_rewind) et `full_page_writes = on` ;
- `wal_level = replica`, `max_wal_senders = 10`, `max_replication_slots = 10`,
  `hot_standby = on` ;
- `wal_keep_size` et `max_slot_wal_keep_size` bornent le WAL retenu : un slot
  inactif au-delà de la borne est invalidé (`wal_status = lost`) et le standby
  concerné se re-seed ; l'alerte correspondante est du lot EP8.4 ;
- `password_encryption = 'scram-sha-256'`, `pg_hba` en `hostssl` seulement.

## Prérequis

1. **PostgreSQL 17 et ses outils clients** (`pg_basebackup`, `pg_rewind`,
   `pg_ctlcluster`) sur les trois nœuds. `psql`/`pg_basebackup` 16 ne pilotent
   pas un serveur 17 (étude, « What already works in the client »).
2. **Réseau privé (#6859)** : les nœuds doivent se joindre sur le port 5432
   par l'adresse privée de l'inventaire (`10.78.0.0/24`, RESEAU-PRIVE.md),
   sans passer par le hub, et `listen_addresses` doit contenir cette adresse
   (`reseau-prive.sh postgres` la pose). Le script le signale s'il ne le voit pas ; il ne
   touche pas au réseau.
3. **TLS** (`ssl = on`, posé par `installer-serveur.sh`) : les lignes
   `hostssl` de `pg_hba` ne servent à rien sans lui. `sslmode=require` chiffre
   sans vérifier le certificat ; une AC du mesh est un suivi, pas un prérequis
   (étude §8).
4. **Espace disque** : une base backup (≈ 1,5 Go aujourd'hui) plus le WAL
   retenu par les slots. `ameesh-agents-2` avait 13 Go libres, à nettoyer
   avant EP8.3 (étude §2).
5. **Secrets** dans `/etc/ameesh/replication.env` (root:root, **0600**), sur
   chaque nœud ; le fichier est refusé s'il est lisible par un autre compte
   **ou s'il n'appartient pas à l'utilisateur effectif** (root en production) :
   un 0600 d'un autre compte serait modifiable par lui, puis sourcé en root.

   ```bash
   install -d -m 0750 /etc/ameesh
   umask 077
   {
     printf 'REPLICATION_SUBNET=%s\n' '10.78.0.0/24'   # réseau privé #6859
     printf 'REPLICATOR_PASSWORD=%s\n' "$(head -c 48 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
     printf 'REWIND_PASSWORD=%s\n' "$(head -c 48 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
   } > /etc/ameesh/replication.env
   chmod 0600 /etc/ameesh/replication.env
   ```

   Les deux mots de passe sont les mêmes sur les trois nœuds (les rôles et
   leurs empreintes se répliquent). Facultatif, pour ajuster les bornes de WAL :
   `WAL_KEEP_SIZE` (défaut `1GB`) et `MAX_SLOT_WAL_KEEP_SIZE` (défaut `4GB`).
   Les scripts n'acceptent que `A-Z a-z 0-9 + / = . _ ~ ! @ % -` (ni espace,
   ni `:`, ni guillemet) : le pgpass n'a rien à échapper et aucune ligne de
   commande n'est coupée.

   Aucun secret n'entre dans le dépôt, ni en argument d'un processus : les
   `ALTER ROLE … PASSWORD` passent par l'entrée standard de `psql`, et le
   `primary_conninfo` ne porte pas de mot de passe.

6. **pgpass du compte postgres** : `initialiser-standby.sh`, `suivre-primaire.sh`
   et `reintegrer.sh` écrivent les entrées `*:5432:replication:replicator:…`
   et `*:5432:postgres:rewind:…` dans `/var/lib/postgresql/.pgpass` (0600,
   postgres:postgres). C'est le seul porteur du secret au repos sur un nœud,
   avec `replication.env` (root). Rien à faire à la main.

## Fichiers

| Fichier | Rôle |
|---|---|
| `noeuds.conf` | inventaire : nom, hôte, rôle cible, synchrone, nofailover, priorité |
| `lib.sh` | fonctions communes : inventaire, secrets 0600, fragments de configuration, pgpass, attentes bornées, SQL |
| `sql/roles.sql` | rôles `replicator` (LOGIN REPLICATION) et `rewind` (LOGIN + quatre fonctions pg_rewind), sans superuser |
| `configurer-noeud.sh` | idempotent : fragment `conf.d`, bloc `pg_hba`, rechargement, redémarrage optionnel, rôles et slots sur le primaire effectif |
| `initialiser-standby.sh` | `pg_basebackup -X stream -S <slot> -R -c fast`, reprise et `standby.signal`, contrôle du flux |
| `promouvoir.sh` | promotion manuelle avec gardes anti-split-brain et borne WAL rejouée ; crée les slots des autres nœuds |
| `reintegrer.sh` | `pg_rewind` de l'ancien primaire, re-pose de la configuration copiée par le rewind, démarrage en standby, suppression des slots physiques inactifs de l'ancien rôle primaire |
| `suivre-primaire.sh` | fait suivre un autre standby au nouveau primaire (`primary_conninfo`/slot), recharge et contrôle |
| `retour-arriere.sh` | retire la configuration du lot (dont le marqueur de veille), et sur demande les slots et les rôles ; aucune donnée supprimée |
| `etat.sh` | lecture seule : rôle, mode veille, synchrones, slots, flux, retards, avertissements |
| `mode-veille.sh` | #8027 : `entrer` (liste synchrone vidée, fin des tours, sauvegarde, consignation), `sortir` (rattrapage puis liste rétablie), `etat` |

Tous les scripts tournent en **root** (comme `installer-serveur.sh`) et
passent les commandes PostgreSQL par `runuser -u postgres`. Les chemins sont
surchargeables par l'environnement pour les témoins (`AMEESH_REPLICATION_*`,
voir `lib.sh`) ; ce n'est pas une interface d'exploitation.

## Mise en service (geste réel EP8.3, pas ce lot)

L'ordre ci-dessous est la cible ; il suppose le réseau #6859 posé et
`replication.env` présent sur les trois nœuds.

1. **Sur le hub (`manaty-mesh-1`), primaire permanent** : rôles, slots des
   deux agents, configuration et `pg_hba` ; le premier passage exige un
   redémarrage court (`wal_log_hints`), fenêtre d'EP8.1/EP8.3. Aucun standby
   n'existe encore : `synchronous_standby_names` reste **vide**, donc les
   `CREATE ROLE`, `ALTER ROLE` et la création des slots ne s'attendent pas
   (B1 de revue #6868) :

   ```bash
   deploy/serveur/replication/configurer-noeud.sh --redemarrer manaty-mesh-1
   ```

2. **Sur chaque agent neuf** (`ameesh-agents-2`, puis `ameesh-agents-1`) :
   configuration puis base backup physique depuis le hub. Le répertoire de
   données Debian doit être arrêté ; le script ne l'arrête pas.

   ```bash
   deploy/serveur/replication/configurer-noeud.sh ameesh-agents-2
   pg_ctlcluster 17 main stop            # si un cluster existe déjà
   deploy/serveur/replication/initialiser-standby.sh \
       --source manaty-mesh-1 ameesh-agents-2
   ```

3. **Activer le synchrone sur le hub**, une fois `ameesh-agents-2` en flux :
   rejouer la configuration (sans redémarrage). Le script ne pose `ANY 1` que
   s'il voit un candidat en `streaming` ; `etat.sh` doit alors afficher
   `sync_state = quorum` (valeur de `ANY 1` en PG17) sur le ou les agents en
   flux ; le hub, primaire, ne figure jamais dans la liste :

   ```bash
   deploy/serveur/replication/configurer-noeud.sh manaty-mesh-1
   deploy/serveur/replication/etat.sh manaty-mesh-1
   ```

   **Avant EP8.4 (Patroni)**, si le standby synchrone est perdu alors que la
   liste est active, les `COMMIT` attendent indéfiniment (`synchronous_mode_strict`
   est une propriété de Patroni, pas de PostgreSQL). Geste d'urgence, sans
   redémarrage : `configurer-noeud.sh --sans-synchrone manaty-mesh-1`
   (liste vide + rechargement) ; rétablir par un passage normal dès le flux
   revenu. `etat.sh` signale les deux cas.

4. **Bascule de secours, puis retour planifié sur le hub** (le hub est le
   primaire permanent : on ne planifie plus une bascule *vers* un agent).
   Si le hub est perdu, promouvoir un agent avec la borne WAL relevée sur le
   hub avant sa perte, ou `--force` en désastre assumé :

   ```bash
   # sur le hub, écritures client arrêtées si possible : relever la borne
   psql -Atc 'SELECT pg_current_wal_flush_lsn()'   # ex. 0/1A2B3C4, à noter
   # puis arrêt propre de l'ancien primaire (si possible)
   pg_ctlcluster 17 main stop
   # sur ameesh-agents-1 : promotion avec la borne relevée
   deploy/serveur/replication/promouvoir.sh --borne 0/1A2B3C4 ameesh-agents-1
   ```

   Quand le hub revient, il est réintégré en standby de l'agent primaire, puis
   le retour planifié le ramène primaire ; `--retour-nofailover` lève le seul
   `nofailover` et exige la borne :

   ```bash
   # sur manaty-mesh-1 : réintégration par pg_rewind derrière l'agent primaire
   deploy/serveur/replication/reintegrer.sh --source ameesh-agents-1 manaty-mesh-1
   # retour planifié : sur ameesh-agents-1, écritures arrêtées, relever la borne et arrêter
   psql -Atc 'SELECT pg_current_wal_flush_lsn()'
   pg_ctlcluster 17 main stop
   # sur manaty-mesh-1 : promotion de retour
   deploy/serveur/replication/promouvoir.sh --retour-nofailover --borne <LSN> manaty-mesh-1
   # sur ameesh-agents-1, puis ameesh-agents-2
   deploy/serveur/replication/reintegrer.sh --source manaty-mesh-1 ameesh-agents-1
   deploy/serveur/replication/suivre-primaire.sh --source manaty-mesh-1 ameesh-agents-2
   # sur manaty-mesh-1, les deux agents en flux : ANY 1
   deploy/serveur/replication/configurer-noeud.sh manaty-mesh-1
   ```

   La borne est **obligatoire** pour une bascule planifiée : `ANY 1` ne
   garantit pas que tous les candidats ont reçu tous les `COMMIT`, et un
   candidat en retard perdrait des transactions confirmées que le `pg_rewind`
   de l'ancien primaire effacerait ensuite. `promouvoir.sh` attend le rejeu de
   la borne (borné) et refuse si elle n'est pas atteinte. Sans borne, il
   refuse aussi ; `--force` seul autorise une promotion de désastre, en
   annonçant la perte possible — jamais pour une bascule planifiée. Si la
   borne n'a pas été relevée avant l'arrêt, redémarrer l'ancien primaire,
   arrêter les écritures et recommencer.

   Après la promotion, le nouveau primaire n'a encore aucun standby en flux :
   sa liste synchrone est vide. La rejouer (`configurer-noeud.sh
   ameesh-agents-1`) une fois `ameesh-agents-2` en flux.

5. **Contrôle final** sur le primaire : `etat.sh` doit montrer le hub primaire,
   deux standbys en flux, deux slots actifs, `wal_log_hints = on`, et le hub
   jamais dans la liste synchrone. En régime nominal, les deux agents sont en
   `quorum` ; si `etat.sh` signale un primaire effectif sur un agent, c'est
   une bascule de secours : le mode veille refuse tant que le hub n'est pas
   revenu primaire. Un seul primaire répond ; les clients se reconnectent par
   le DSN multi-hôte (EP8.4/EP8.5).

## Mode veille (#8027)

Au repos, le mesh n'a qu'un hôte allumé : le hub. La base doit donc rester
servie par le hub, et l'arrêt d'`ameesh-agents-1`/`-2` ne doit bloquer aucun
`COMMIT`. Le déclencheur de veille (#8029, `lifecycle_hook` du `vm_adapter`)
appelle ce script **une fois** avant d'arrêter le premier hôte qui porte une
réplique, puis une fois au réveil, après le retour des deux agents :

```bash
deploy/serveur/replication/mode-veille.sh entrer
# … arrêt des VM agents (déclencheur) …
deploy/serveur/replication/mode-veille.sh sortir
# … réveil des VM agents, puis sortir quand elles sont de retour …
```

**Contrat** : sortie 0 = le déclencheur peut arrêter (ou rendre) les agents ;
sortie non nulle = ne pas arrêter les agents, l'état est consigné. Le script
est idempotent : le rejouer ne change rien d'utile.

`entrer` :

1. refuse si le primaire effectif n'est pas le hub (**jamais** arrêter les
   agents dans ce cas : une bascule de secours doit d'abord ramener le
   primaire sur le hub) ;
2. exige une sauvegarde logique de moins de 26 h dans
   `/home/ameesh/sauvegardes` (`ameesh-sauvegarde.timer`) ; la dérogation
   `--sans-sauvegarde "<raison>"` est consignée ;
3. écrit le marqueur `/etc/ameesh/mode-veille` (0644, clé=valeur) **avant** la
   configuration, puis réécrit le fragment avec
   `synchronous_standby_names = ''` et recharge. Le marqueur est relu par
   `configurer-noeud.sh` : un passage pendant la veille ne repose pas la liste ;
4. vérifie que `SHOW synchronous_standby_names` est bien vide après le
   rechargement ; si un autre gestionnaire (Patroni) tient la valeur, le script
   refuse et les agents ne doivent pas être arrêtés ;
5. attend qu'aucun **tour** ne soit réellement en cours (statut `running` et
   bail vivant dans `agent_registry`, base `ameesh`) ; les runners *idle*
   gardent leur bail et ne comptent pas, sinon la veille ne pourrait jamais
   être prise avant l'arrêt des agents. L'attente est bornée par
   `AMEESH_REPLICATION_ATTENTE_TOURS` (600 s par défaut) ; un tour restant est
   un refus, pas un arrêt forcé ;
6. consigne `mode=veille`, l'heure, le nœud, la liste avant, la sauvegarde, le
   compte de tours et le résultat dans `/etc/ameesh/mode-veille`.

Si l'entrée échoue après la pose du marqueur (tours restants, autre
gestionnaire, rechargement), elle est **annulée** : le marqueur est retiré, la
liste synchrone rétablie si un candidat est en flux, et l'échec consigné en
`mode=actif` — les agents ne sont pas arrêtés et la durabilité n'est pas
laissée dégradée sans échéance. Un rejeu sur une veille **déjà active** ne
défait jamais l'état existant : en cas d'échec, le marqueur et la liste vide
restent en place.

`sortir` :

1. refuse si le primaire n'est pas le hub ;
2. attend qu'au moins un candidat synchrone soit **en flux et rattrapé**
   (`replay_lsn >= pg_current_wal_flush_lsn()`), borné par
   `AMEESH_REPLICATION_ATTENTE_REVEIL` (900 s : démarrage des VM et
   rattrapage) ; sans réplique rattrapée, la liste reste vide et le script
   échoue (les agents ne sont pas « rendus ») ;
3. retire le marqueur, réécrit le fragment (`ANY 1` avec les agents en flux),
   recharge, vérifie que la liste n'est plus vide ; en cas d'échec, le
   marqueur de veille est reposé (état sûr : liste vide) ;
4. consigne `mode=actif`, la liste rétablie, les répliques rattrapées.

Branché sur le `vm_adapter` (#8420), `sortir` est rejoué : une entrée réussie
laisse une dette durable sur la ligne de contrôle de la réplique
(`standby_entered_at`), et un échec de `sortir` y ajoute
`standby_exit_failed_at` (0615). Le worker rejoue `sortir` à chaque tick tant
que la dette existe, et `host_release.alerts` lève
`host_release_standby_exit_failed` jusqu'au succès (#8459) : une liste
synchrone laissée vide n'est plus silencieuse. Le script reste idempotent,
donc ce rejeu ne change rien hors veille.

Le mode veille ne touche **aucune donnée** : il ne fait que lire (baux,
`pg_stat_replication`) et réécrire le fragment de configuration. La sauvegarde
logique est lue, pas produite. `etat.sh` affiche le mode et ne signale pas la
liste vide comme une dégradation quand la veille est active ; il signale en
revanche un primaire effectif hors du hub.

En cas d'urgence hors veille (standby perdu, `COMMIT` qui attendent), le geste
immédiat reste `configurer-noeud.sh --sans-synchrone` : il vide la liste pour
le passage courant, sans marqueur persistant ; `mode-veille.sh entrer` est le
geste durable qui survit à un `configurer-noeud.sh` pendant l'arrêt.

Le plan Patroni (EP8.4, #6959) n'est **pas** applicable en l'état : il prévoit
`ameesh-agents-1` comme primaire préféré et une promotion préalable du hub
vers AG1, ce que #8027 interdit. `adoption.py topologie` compare
`topologie.json` à `noeuds.conf` et refuse désormais l'adoption (écart
« primaire d'EP8.3 manaty-mesh-1, primaire préféré de Patroni
ameesh-agents-1 ») ; #6959 reste en attente d'une révision du plan Patroni et
d'une décision owner. Ne pas promouvoir un agent pour rendre l'adoption
possible.

## Promotion manuelle

`promouvoir.sh` est un geste **d'exploitant**, pas une décision d'agent
(étude §4 : Patroni fera le code en EP8.4). Depuis #8027, promouvoir un agent
est une **bascule de secours** (le hub primaire est perdu) ; l'opération
planifiée est le **retour** du primaire sur le hub (`--retour-nofailover`).
Gardes :

- un nœud `nofailover` (le hub) est refusé sans `--force`, sauf le retour
  planifié sur le hub (`--retour-nofailover`, borne exigée, autres gardes
  intactes) ;
- le serveur local doit tourner et être en récupération ;
- **aucun autre nœud joignable ne doit répondre comme primaire** ; un nœud
  joignable dont le rôle est illisible arrête aussi (on ne parie pas) ;
- `--borne <LSN>` est **obligatoire** pour une bascule planifiée : la borne
  WAL relevée sur l'ancien primaire avant son arrêt doit être rejouée par le
  candidat, sinon `promouvoir.sh` refuse (attente bornée puis erreur
  explicite) ;
- `--force` ne sert qu'à une décision **humaine** de désastre (double panne) :
  il autorise l'absence de borne ou un candidat en retard, en annonçant la
  perte possible ; il est journalisé par le message, jamais par une écriture
  silencieuse.

Une bascule planifiée relève la borne
(`SELECT pg_current_wal_flush_lsn()`) sur l'ancien primaire **avant** de
l'arrêter (`pg_ctlcluster 17 main stop`), puis promeut avec `--borne`. Le
script ne fait pas le premier geste : un primaire qui tourne encore est un
split brain en puissance.

Après promotion, `promouvoir.sh` réécrit la configuration : à cet instant aucun
standby n'est encore en flux, donc `synchronous_standby_names` est vide (les
premiers `COMMIT` du nouveau primaire ne s'attendent pas) ; un passage de
`configurer-noeud.sh` après le flux rétablit `ANY 1`. Le script recharge et crée
un slot physique pour chaque autre nœud (les slots ne se répliquent pas).
L'ancien primaire revient par `reintegrer.sh`, les autres standbys par
`suivre-primaire.sh`.

## pg_rewind et re-seed

`reintegrer.sh` exige :

- le serveur local **arrêté proprement** (le script ne l'arrête pas) ;
- le rôle `rewind` (quatre fonctions, pas superuser) et le pgpass du compte
  postgres ;
- `wal_log_hints = on` sur l'ancien primaire **avant** qu'il ne serve : le
  paramètre est posé par `configurer-noeud.sh` mais ne s'applique qu'au
  redémarrage. `etat.sh` le signale ; sans lui, `pg_rewind` échoue.

pg_rewind recopie la configuration présente dans le répertoire de données,
dont `postgresql.auto.conf` (sur Debian, `postgresql.conf` et `pg_hba.conf`
sont hors du répertoire de données, mais le script ne s'appuie pas sur cet
emplacement). Le script **re-pose ensuite** le fragment du nœud, son bloc
`pg_hba`, ses `primary_conninfo`/`primary_slot_name` et `standby.signal` :
sans cela, le standby pourrait suivre l'ancienne source ou démarrer avec la
configuration d'un primaire.

Avec l'archivage WAL (#6858), une reprise peut aussi chercher les segments
manquants avec `pg_rewind -c` (`--restore-target-wal`) ; ce lot ne le pose pas,
l'archive étant d'un autre lot.

Un échec de `pg_rewind` n'est pas masqué : le script sort en erreur, ne
redémarre pas le répertoire en primaire (il peut être inutilisable) et imprime
le re-seed explicite :

```bash
deploy/serveur/replication/initialiser-standby.sh --reinitialiser \
    --source ameesh-agents-1 manaty-mesh-1
```

`--reinitialiser` **déplace** l'ancien répertoire en
`<données>.remplace-<horodatage>-<pid>` ; il ne le supprime jamais. Si la base
backup échoue, l'ancien répertoire est toujours là.

## Retour arrière

Deux niveaux, à ne pas confondre :

1. **La préparation de ce lot** (configuration seule) :
   `retour-arriere.sh [--tout] [nœud]` retire le fragment `conf.d` et le bloc
   marqué de `pg_hba`, puis, sur demande, les slots (`--slots`) et les rôles
   (`--roles`, après les slots). Avant `DROP ROLE rewind`, les quatre
   `GRANT EXECUTE` de `pg_rewind` sont révoqués nommément (B3 de revue #6868 :
   PostgreSQL refuse sinon le `DROP ROLE`, « objects depend on it ») ; aucun
   `DROP OWNED` général n'est utilisé. Aucune donnée n'est touchée, le serveur
   n'est pas arrêté, `standby.signal` reste en place. Un slot actif arrête sans
   `--force`. Les paramètres de redémarrage (`wal_log_hints`, `wal_level`)
   reprennent leur défaut au redémarrage suivant, que le script n'impose pas.
2. **La bascule d'EP8.3** (données) : elle se décide, elle ne se scripte pas
   ici.
   - **Avant toute écriture sur le nouveau primaire** : arrêter les clients,
     arrêter le nouveau primaire, redémarrer l'ancien en primaire, puis
     `suivre-primaire.sh` sur les standbys pour les repointer vers lui. Le
     nouveau primaire promu a divergé par sa promotion (nouvelle timeline) :
     il ne peut pas redevenir standby sans `pg_rewind` ou re-seed.
   - **Après des écritures** : revenir en arrière perd les transactions
     postérieures à la bascule. La priorité est de réparer en avant ; une
     perte de données est une décision humaine (owner/orchestrateur), pas un
     geste de script. La copie PITR (#6858) est le recours pour une erreur
     logique, pas pour annuler une bascule.

## Surveillance

`etat.sh` lit le nœud local, sans connexion distante ni secret (il ne lit pas
`replication.env`). Sur le primaire il affiche les standbys (`state`,
`sync_state`, LSN), les slots (`active`, `wal_status`) ; sur un standby, le
flux (`pg_stat_wal_receiver`, source, slot, `written_lsn`/`flushed_lsn`/
`latest_end_lsn`, date du dernier message — les colonnes `*_lag` n'existent
que dans `pg_stat_replication`, côté primaire). Il affiche le mode veille
(#8027) et, sur un primaire, si le primaire effectif est bien le hub de
l'inventaire. Il avertit si `wal_log_hints` est off, si un nœud `nofailover`
apparaît dans `synchronous_standby_names`, si la liste synchrone est active
sans candidat en flux (risque de `COMMIT` bloqués, B1) ou vide hors veille
(RPO dégradé), si aucun standby n'est `sync`/`quorum`, si un slot est inactif
ou perdu. En veille, la liste vide n'est pas une dégradation : les slots
inactifs restent signalés (ils retiennent du WAL). Il sort 0 si le serveur
répond, 1 sinon : il n'est pas une sonde de supervision.

Les alertes `db_replication_lag`, `db_slot_lost`, `db_no_primary` et
`db_archive_stale` sont du lot EP8.4/EP8.5 (étude §7) : ce lot ne fixe pas de
seuil. Les slots sont bornés par `max_slot_wal_keep_size` ; un `wal_status`
perdu impose un re-seed, pas une augmentation silencieuse de la borne.

## Sécurité et moindre privilège

- `replicator` : LOGIN + REPLICATION, ni superuser, ni CREATEDB/CREATEROLE,
  NOINHERIT.
- `rewind` : LOGIN sans superuser, avec exactement les quatre `EXECUTE` que
  la documentation PostgreSQL 17 exige pour `pg_rewind` en ligne.
- `pg_hba` : `hostssl` seulement, `scram-sha-256`, restreint au CIDR
  `REPLICATION_SUBNET` ; l'accès client `ameesh` reste le bloc `ameesh-mesh`
  d'`installer-serveur.sh`.
- `primary_conninfo`, `pg_basebackup` et `pg_rewind` sont en
  `sslmode=require` : TLS obligatoire, aucun repli en clair. La vérification
  du certificat (`verify-full`) attend l'AC du mesh (#6859, étude §8) ; c'est
  une limite connue, non bloquante pour ce lot.
- Secrets : `replication.env` 0600 root ; pgpass 0600 postgres ; aucun mot de
  passe dans le dépôt, en argument, dans `primary_conninfo` ni dans un journal
  de script. Une rotation se fait en modifiant `replication.env` puis en
  rejouant `configurer-noeud.sh` (rôles) et `installer_pgpass` via
  `initialiser-standby.sh`/`suivre-primaire.sh` (pgpass) sur chaque nœud.
- Ce lot ne crée aucun superuser joignable, aucun port public, aucun secret
  partagé hors des nœuds.

## Témoins

Aucun test ne touche un hôte réel ni le réseau : `tests/test_6868_replication.py`
remplace `psql`, `pg_ctlcluster`, `pg_isready`, `pg_basebackup`, `pg_rewind`,
`runuser` et `hostname` par des doublures sous un répertoire jetable, et
vérifie aussi que les mots de passe ne passent jamais en argument. Les témoins
du mode veille (#8027) couvrent le refus hors du hub, la sauvegarde logique,
la liste vidée et rechargée, l'attente des tours réellement en cours (les
baux des runners idle ne comptent pas), l'annulation d'une entrée échouée
avec liste rétablie, le rejeu sur une veille active, le rattrapage au réveil
et la consignation dans le marqueur.

```bash
PYTHONPATH=src python -m unittest tests.test_6868_replication -v
```

## Limites connues

- Pas de Patroni/etcd : la promotion est manuelle et le split brain est gardé
  par un contrôle de rôle, pas par un quorum (EP8.4). Le plan Patroni actuel
  (#6959) suppose le primaire sur AG1 et est **suspendu** par #8027 tant
  qu'il n'est pas révisé (voir « Mode veille »).
- Le réseau privé (#6859) et l'archivage/PITR (#6858) sont des prérequis
  d'autres lots ; les hôtes de `noeuds.conf` sont les adresses privées de
  #6859 (EP8.3, #6860).
- Le mode veille (#8027) est branché sur le déclencheur d'arrêt/réveil des
  VM par #8420 (cibles `replica` du `vm_adapter`, voir
  deploy/vm-adapter/README.md, « Mode veille de la base ») ; aucun hôte réel
  n'a encore été arrêté avec la veille.
- Aucune alerte ni sonde de supervision : `etat.sh` est un outil
  d'exploitation, pas un service (EP8.4/EP8.5).
- La bascule réelle, la fenêtre et la recette sur les trois nœuds restent le
  lot EP8.3 (#6860) ; ce lot ne les a pas jouées.
