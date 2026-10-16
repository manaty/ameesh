# Installer, redémarrer et contrôler une VM d’agents

Un hôte d’agents utilise les unités **utilisateur**
`ameesh-runner-agent@<agent>.service`, sous le compte `ameesh`.
Depuis le poste, `deploy/mise-a-jour/vm.sh agents redemarrer` les redémarre
une par une, arrêt prouvé avant chaque démarrage. `agents controler` (ou
`agents verifier`) contrôle les unités et les baux, sans redémarrer personne.
`agents installer [REF]` installe une révision gelée dans le layout du compte
et `agents retour` réinstalle la précédente (#1252).

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

Le redémarrage ferme l’admission de chaque unité par `stop` (SIGTERM) avant
de la démarrer : le tour en cours draine, borné par `TimeoutStopSec` (35 min),
et le démarrage n’a lieu qu’après preuve du point sûr (`ActiveState=inactive`
et `MainPID=0`). Aucune fenêtre « idle » n’est attendue : une persona en tour
ne bloque plus la mise à jour. Il relit le placement avant chaque redémarrage :
un agent déplacé ou passé en observation est ignoré ; un agent arrêté n’est
pas réactivé (aucun bail, aucun tour — #1131), mais son unité encore active
est rechargée sur le nouveau code (#1633), sans quoi elle servirait la
reprise de l’owner avec la version d’avant le déploiement. Le compte
`ameesh` doit avoir un gestionnaire systemd utilisateur ; le script pose
`HOME`, `XDG_RUNTIME_DIR` et `DBUS_SESSION_BUS_ADDRESS` pour joindre son bus.
Le CLI distant est celui de l’unité :
`/home/ameesh/.local/share/ameesh/venv/bin/ameesh`.
La CLI et la lecture de l’horloge passent par `systemd-run --user --pipe
--wait --quiet`, avec `EnvironmentFile` sur un chemin absolu : `systemd-run
-p` n’étend pas le spécificateur `%h` dans une unité transitoire (#1133), le
shell expanse donc `$HOME` (`/home/ameesh/.config/ameesh/env`). Les fichiers
d’unité `deploy/systemd/*.service` gardent `%h`, résolu dans une unité
installée. Systemd lit ce fichier avec le même parseur que le runner : les
valeurs contenant `$USER`, des guillemets ou `#` restent intactes. Un
fichier absent ou illisible produit une erreur avant toute exécution, sans
configuration de repli.

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
n’écrit rien. La lecture passe par le paquet installé ; si celui-ci ne
s’importe plus (une installation inachevée peut l’avoir cassé), un repli
reproduit `config.load().host` sans l’importer : `AMEESH_HOST` puis
`AGENT_MESH_HOST`, sinon le champ `host` du JSON désigné par `AMEESH_CONFIG`
ou `AGENT_MESH_CONFIG` (repli sur `~/.config/agent-mesh/config.json` comme
`config.load`), sinon le nom court de la machine. Un fichier de configuration
présent mais illisible, ou un host non textuel ou vide, ferme la commande
plutôt que de retomber sur le nom machine.

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
(`build/`, `*.egg-info`, `__pycache__`) en sont retirés, puis chaque fichier du
disque est haché et comparé aux objets du commit, sans passer par l’index :
`assume-unchanged` et `skip-worktree` ne masquent rien, et un fichier modifié,
absent, en trop ou au mauvais mode fait refuser l’instantané, qui n’est jamais
écrasé — le retirer à la main pour le reconstruire.

`previous-sha` n’est publié qu’après une installation réussie, et seulement
quand le code installé change : relancer `installer` sur le SHA déjà en place le
laisse intact (sans `previous-sha`, on n’en invente pas un — pas de faux
retour), et le journal est alors effacé. Si l’installation échoue (même après un premier `pip` qui a
déjà remplacé le paquet et publié `direct_url.json`), la commande ferme en
conservant le journal : `retour` ramène au code quitté qu’il désigne, et non à
un `previous-sha` plus ancien — y compris à la première utilisation, sans
`previous-sha`. Tant que ce journal existe, `installer` refuse avant toute
mutation et oriente vers `retour` : une relance ne peut ni écraser la cible de
récupération ni prendre pour elle le SHA dont le venv porte déjà le
`direct_url.json`. `retour` réinstalle le commit du journal depuis son
instantané puis enchaîne `redemarrer` et `controler` ; après réussite il laisse
`previous-sha` sur le code installé, donc un second `retour` est idempotent. Il
refuse si la cible (journal ou `previous-sha`) est absente, invalide ou
introuvable dans le clone de la VM comme dans le dépôt du poste.

Si la VM n’a pas le commit visé, il est envoyé en paquet git par SSH depuis le
poste. Le fichier distant est créé par `mktemp` (racine, mode privé : ni nom
fixe pré-créable, ni symlink exploitable), puis rendu lisible par le compte
`ameesh` (`chmod 644` sur la cible — le umask du root distant ne décide pas de
la lecture), et retiré après le `fetch`. Le paquet exclut le HEAD du clone de la
VM, dont la présence est prouvée avant l’envoi ; sans base lisible, ou si
l’exclusion vide le paquet, il est autonome — une cible déjà fusionnée dans
`origin/main` passe donc, comme le retour vers un ancêtre absent de la VM.

La séquence de staging écrit chaque événement en JSONL (`date`, `hôte`, `étape`, `verdict`,
`sha`, `precedent`, `detail`) dans
`~/.local/state/ameesh/mise-a-jour/staging-<date>.jsonl`. Le `detail` de
l’étape `sante` est le **rapport JSON** de `sante-courrier.py` (hôte, commit,
verdict, et pour une remise réelle le message de sonde, son destinataire,
l’hôte de ce destinataire et son délai) : c’est la preuve structurée que le
signal ci-dessous exige. Un verdict `sans-sonde` — `SONDE_<HOTE>=jamais`, ou
`auto` sans persona menée — ne prouve aucune remise.

## Signal « staging actif » (#1323)

Le signal est porté par
[`deploy/mise-a-jour/staging.sh`](../deploy/mise-a-jour/staging.sh) : une
séquence **complète et saine** — les quatre hôtes, `installer`,
`redemarrer`, `controler` et sonde de courrier réellement remise — écrit une
attestation durable sous
`~/.local/state/ameesh/mise-a-jour/actif-<commit>.json` et réveille les
rapporteurs déclarés avec `--lots "917 988"` et
`--rapporteurs "mty-amh-docs-01"` (ou `LOTS_VERIF` / `RAPPORTEURS_VERIF` dans
`BASCULE_ENV`).

Le courrier nomme les hôtes, le commit **relu actif** sur chacun après la
séquence, les preuves `installer`/`redemarrer`/`controler`/`sante` et les lots
concernés. Un échec, un retour automatique, une séquence partielle
(`--hotes`), un journal d’une autre exécution ou une santé sans sonde de
remise réelle (`--sans-sonde`, `SONDE_<HOTE>=jamais`, `auto` sans persona
menée) n’émettent **aucun signal de réussite** : chaque événement de la
fenêtre doit porter le commit visé, et le message de sonde est relu en base
(expéditeur système, destinataire, hôte, commit, remise effective). Aucune
clôture de lot n’est automatique : le rapporteur reprend lui-même sa
vérification après déploiement.

Un hôte **sans exécutant** (mode serveur, comme `manaty-mesh-1`) ne peut pas
prouver sa chaîne par une remise locale : sa preuve est le `controler` vert
plus une **sonde croisée**, remise à une persona menée d’un autre hôte
canonique **après son redémarrage** — la remise traverse alors sa base, le
message étant déposé dans la base du serveur et relevé par l’exécuteur de
l’autre hôte. `sante-courrier.py --serveur` produit cette sonde et nomme
l’hôte destinataire (`hote_destinataire`) ; `actif.py` relit la remise en base
et exige qu’elle soit postérieure au `redemarrer` de l’hôte mesuré. Un serveur
qui porte des exécutants garde la règle locale : une santé `sans-sonde`
(aucune persona menée) n’atteste rien, et une sonde croisée n’est acceptée que
pour un hôte à zéro persona.

L’émission est bornée et **idempotente** : un seul courrier par rapporteur et
par commit, même si la séquence est relancée. La clé d’émission est écrite
dans l’attestation (champ `emissions`) **avant** l’envoi ; une relance relit
le courrier déjà déposé portant cette clé et le réconcilie au lieu de le
renvoyer. Cette recherche est **exhaustive** : la fenêtre relue double tant
qu’elle est pleine, une absence n’est donc retenue que prouvée. Si l’absence
ne peut pas être prouvée (base injoignable, ou fenêtre toujours pleine au
plafond), la marque d’émission est **préservée** et rien n’est renvoyé
automatiquement : `actif.py` sort en code 5 et `staging.sh` le signale, le
relais opérateur vérifie le dépôt avant toute relance. Si l’envoi échoue et
que l’absence est prouvée, la clé reste en attente et la relance renvoie une
seule fois. L’attestation est réécrite en place, par un temporaire unique
créé en `0600`.

Si le courrier ne part pas (base indisponible), l’attestation reste écrite et
`staging.sh` le signale : le relais opérateur transmet alors le journal et
l’attestation à la main.

Les migrations de la base commune doivent être appliquées avant l’installation
du code, selon la procédure de [BASCULE.md](BASCULE.md). Les commandes de cette
page sont à exécuter par le responsable du déploiement après revue et
validation CI, une VM à la fois ; `controler` affiche le code installé, le
clone et `previous-sha` avant les unités.
