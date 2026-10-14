# Redémarrer et contrôler une VM d’agents

Un hôte d’agents utilise les unités **utilisateur**
`ameesh-runner-agent@<agent>.service`, sous le compte `ameesh`.
Depuis le poste, `deploy/mise-a-jour/vm.sh agents redemarrer` les redémarre
une par une, hors tour. `agents controler` contrôle les unités et les baux,
sans redémarrer personne ; `agents verifier` lit, sans rien modifier, le code
installé, la version du venv et les migrations manquantes. `agents
installer [REF]` et `agents retour` installent le code, comme sur le serveur
du mesh, dans le layout du compte `ameesh` : `src/` (dépôt détaché),
`venv/`, `src-<court>/` (instantané par commit) et `previous-sha`.

Dans le fichier privé `$BASCULE_ENV` (par défaut
`~/.config/ameesh/bascule.env`), renseigner la cible SSH **root** et le nom
exact de l’hôte dans le registre :

```sh
AGENTS_SSH=root@adresse-de-la-vm
HOTE_AGENTS=nom-hote-du-registre
```

Le mode agents ne demande ni `VM` ni `PERSONAS_VM`. Il lit `ameesh list
--json` **sur la VM**, avec la configuration de l’utilisateur `ameesh` et le même
`~/.config/ameesh/env` que le runner (DSN et schéma compris), et
sélectionne les agents placés sur `HOTE_AGENTS`, en mode `execute`, non
arrêtés. Il contrôle aussi les unités manquantes ou inactives. Un registre
illisible ou une sélection vide fait échouer la commande. Aucun nom d’agent
n’est à maintenir dans le fichier de paramètres.

```sh
deploy/mise-a-jour/vm.sh agents verifier
deploy/mise-a-jour/vm.sh agents redemarrer
deploy/mise-a-jour/vm.sh agents controler
```

Le redémarrage attend la fin des tours, pendant au plus `ATTENTE_MAX`
secondes (7200 par défaut). Il relit le placement avant chaque redémarrage :
un agent déplacé, arrêté ou passé en observation est ignoré. Le compte
`ameesh` doit avoir un gestionnaire systemd utilisateur ; le script pose
`HOME`, `XDG_RUNTIME_DIR` et `DBUS_SESSION_BUS_ADDRESS` pour joindre son bus.
Le CLI distant est celui de l’unité :
`/home/ameesh/.local/share/ameesh/venv/bin/ameesh`.
La CLI et la lecture de l’horloge passent par `systemd-run --user --pipe
--wait --quiet`, avec `EnvironmentFile=%h/.config/ameesh/env` obligatoire.
Systemd lit ce fichier avec le même parseur que le runner : les valeurs
contenant `$USER`, des guillemets ou `#` restent intactes. Un fichier absent
ou illisible produit une erreur avant toute exécution, sans configuration
de repli.

Une unité active ne suffit pas à prouver la reprise d’un bail. Le contrôle
partagé avec le poste et le serveur (`deploy/mise-a-jour/bail.sh`, #502/#503)
lit le propriétaire attendu dans l’ExecStart de l’unité (`--runner-id`,
formes séparée, `=`, abréviation et dernière occurrence comprises). Sans
cet argument, il utilise le propriétaire par défaut `hôte:MainPID`. Un
ExecStart illisible fait échouer le contrôle avant tout redémarrage.

Si le runner tenait son bail avant le redémarrage, il doit le reprendre à
une époque supérieure, avec le propriétaire attendu et une expiration
future : jusqu’à 24 lectures espacées de 5 secondes. Sans bail préalable,
le redémarrage affiche un WARNING par persona et un décompte final « non
vérifié » ; l’absence de preuve de vie ne bloque pas cette opération.
Le contrôle sans redémarrage exige toujours le bail, le propriétaire
attendu et une époque positive. L’horloge est celle de PostgreSQL, lue via
`db.connect(config.load())` dans le venv distant et l’environnement du
runner ; aucun DSN n’est passé en argument de processus. Un changement
d’hôte pendant le contrôle fait échouer la preuve. En cas d’échec, la
commande indique l’unité et la commande `journalctl --user` à lancer sous
`ameesh` sur la VM.

`agents installer REF` refuse tant que la base commune n’a pas toutes les
migrations de REF (lues dans le dépôt du poste, comparées à
`schema_migrations` sur la VM avec l’environnement du runner) : lancer
d’abord `poste.sh migrer REF`, qui n’applique que des migrations additives
avant l’installation. Le commit visé est déposé sur la VM en paquet git par
SSH s’il y manque, puis extrait dans `src-<court>` et installé dans le venv
(`--no-deps` d’abord, dépendances ensuite) ; le commit courant est écrit dans
`previous-sha` avant toute modification. `agents retour` réinstalle le commit
de `previous-sha`, redémarre puis contrôle ; il refuse sans précédent
enregistré. Les migrations passées avant l’installation sont additives,
l’ancien code les ignore. Les commandes de cette page sont à exécuter par le
responsable du déploiement depuis le poste, après revue et validation CI.
