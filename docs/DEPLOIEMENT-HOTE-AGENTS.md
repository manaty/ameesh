# Installer, redémarrer et contrôler une VM d’agents

Un hôte d’agents utilise les unités **utilisateur**
`ameesh-runner-agent@<agent>.service`, sous le compte `ameesh`.
Depuis le poste, `deploy/mise-a-jour/vm.sh agents redemarrer` les redémarre
une par une, hors tour. `agents controler` (ou `agents verifier`) contrôle
les unités et les baux, sans redémarrer personne. `agents installer [REF]`
installe une révision gelée dans le layout du compte et `agents retour`
réinstalle la précédente (#1252).

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

## Installer une révision gelée, revenir en arrière

Le mode agents travaille dans le layout du compte `ameesh`
(`~/.local/share/ameesh` : clone `src`, instantanés `src-<sha7>`, venv,
`previous-sha`, journal `installation-en-cours`). Un `BASCULE_ENV` par VM porte
`AGENTS_SSH` et `HOTE_AGENTS`.

```sh
deploy/mise-a-jour/vm.sh agents verifier
deploy/mise-a-jour/vm.sh agents installer <sha40>
deploy/mise-a-jour/vm.sh agents redemarrer
deploy/mise-a-jour/vm.sh agents controler
deploy/mise-a-jour/vm.sh agents retour
```

`installer` et `retour` refusent d’abord si l’identité effective du compte
runner — `AMEESH_HOST` de son `EnvironmentFile`, lue dans l’environnement de
l’unité — n’est pas `HOTE_AGENTS` : un `BASCULE_ENV` qui vise une autre VM
n’écrit rien. La lecture passe par le paquet installé, avec un repli qui lit le
même fichier sans lui (une installation inachevée peut l’avoir cassé) ; toute
lecture impossible ferme la commande.

`installer` refuse ensuite tant que les migrations du commit visé manquent de
la base commune du mesh (`poste.sh migrer` d’abord), et refuse un code d’avant
non traçable (`direct_url.json` du venv ou HEAD du clone). Il ne redémarre
personne. Il sauvegarde l’état (env et unités, 0600, sous
`~/.local/state/ameesh/mise-a-jour`), vérifie ou crée les instantanés exacts
`src-<sha7>` du code quitté et du commit visé, puis écrit un **journal de
récupération** (`installation-en-cours`) avant de mettre le clone en détaché
sur le SHA visé et de réinstaller depuis l’instantané
(`pip --force-reinstall --no-deps` puis les dépendances). Un instantané est un
worktree détaché du commit : son arbre est celui du commit, jamais une copie
d’un clone sale. Avant réutilisation, les artefacts de construction ignorés
(`build/`, `*.egg-info`, `__pycache__`) en sont retirés, puis l’instantané doit
correspondre exactement au commit, fichiers suivis comme ignorés ; sinon
l’opération refuse de l’utiliser et ne l’écrase jamais — le retirer à la main
pour le reconstruire.

`previous-sha` n’est publié qu’après une installation réussie, et le journal
est alors effacé. Si l’installation échoue (même après un premier `pip` qui a
déjà remplacé le paquet), la commande ferme en conservant le journal :
`retour` ramène au code quitté qu’il désigne, et non à un `previous-sha` plus
ancien — y compris à la première utilisation, sans `previous-sha`. `retour`
réinstalle ce commit depuis son instantané puis enchaîne `redemarrer` et
`controler` ; après réussite il laisse `previous-sha` sur le code installé,
donc un second `retour` est idempotent. Il refuse si la cible (journal ou
`previous-sha`) est absente, invalide ou introuvable dans le clone de la VM
comme dans le dépôt du poste.

Si la VM n’a pas le commit visé, il est envoyé en paquet git par SSH depuis le
poste. Le paquet exclut le HEAD du clone de la VM, dont la présence est prouvée
avant l’envoi ; sans base lisible, ou si l’exclusion vide le paquet, il est
autonome — une cible déjà fusionnée dans `origin/main` passe donc, comme le
retour vers un ancêtre absent de la VM.

Les migrations de la base commune doivent être appliquées avant l’installation
du code, selon la procédure de [BASCULE.md](BASCULE.md). Les commandes de cette
page sont à exécuter par le responsable du déploiement après revue et
validation CI, une VM à la fois ; `controler` affiche le code installé, le
clone et `previous-sha` avant les unités.
