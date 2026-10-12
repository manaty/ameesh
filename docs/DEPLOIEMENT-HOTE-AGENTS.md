# Redémarrer et contrôler une VM d’agents

Un hôte d’agents utilise les unités **utilisateur**
`ameesh-runner-agent@<agent>.service`, sous le compte `ameesh`.
Depuis le poste, `deploy/mise-a-jour/vm.sh agents redemarrer` les redémarre
une par une, hors tour. `agents controler` (ou `agents verifier`) contrôle
les unités et les baux, sans redémarrer personne.

Dans le fichier privé `$BASCULE_ENV` (par défaut
`~/.config/ameesh/bascule.env`), renseigner la cible SSH **root** et le nom
exact de l’hôte dans le registre :

```sh
AGENTS_SSH=root@adresse-de-la-vm
HOTE_AGENTS=nom-hote-du-registre
```

Le mode agents ne demande ni `VM` ni `PERSONAS_VM`. Il lit `ameesh list
--json` **sur la VM**, avec la configuration de l’utilisateur `ameesh`, et
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

Une unité active ne suffit pas. Après un redémarrage, le contrôle partagé
avec le poste et le serveur (`deploy/mise-a-jour/bail.sh`, lots #502/#503)
attend un propriétaire de bail et une époque supérieure à celle relevée
avant le redémarrage, jusqu’à 24 lectures espacées de 5 secondes. Un agent
arrêté n’a pas à reprendre de bail. Le contrôle sans redémarrage exige un
propriétaire, une époque positive et `lease_expires_ts` dans le futur
(horloge du poste). Un changement d’hôte pendant le contrôle fait échouer
la preuve. En cas d’échec, la commande indique l’unité et la commande
`journalctl --user` à lancer sous `ameesh` sur la VM.

Ce mode couvre le redémarrage et le contrôle du code déjà installé.
`installer` et `retour` restent les opérations du serveur du mesh ; elles
sont refusées en mode agents. Les migrations de la base commune doivent
être appliquées avant l’installation du code, selon la procédure de
[BASCULE.md](BASCULE.md). Les commandes de cette page sont à exécuter par
le responsable du déploiement après revue et validation CI.
