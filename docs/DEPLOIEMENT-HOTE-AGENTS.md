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
SSH s’il y manque (`paquet-git.sh` : les exclusions se calculent sur les objets
déjà présents chez le destinataire, jamais sur `origin/main`, dont la cible
staging est justement un ancêtre), puis extrait dans `src-<court>` et installé
dans le venv
(`--no-deps` d’abord, dépendances ensuite) ; le commit courant est écrit dans
`previous-sha` avant toute modification. `agents retour` réinstalle le commit
de `previous-sha`, redémarre puis contrôle ; il refuse sans précédent
enregistré. Les migrations passées avant l’installation sont additives,
l’ancien code les ignore.

## Séquence staging des quatre hôtes

`deploy/mise-a-jour/staging.sh [REF]` enchaîne la mise à jour des quatre hôtes
dans l’ordre du canon §14 : `ameesh-agents-1`, `ameesh-agents-2`,
`manaty-mesh-1`, puis `pc-smichea` **en dernier**. C’est un staging, pas une
release : aucun go de l’owner n’est demandé.

```sh
deploy/mise-a-jour/staging.sh --a-blanc   # plan seul, rien n’est touché
deploy/mise-a-jour/staging.sh             # cible HEAD du dépôt
deploy/mise-a-jour/staging.sh <REF>       # commit, branche ou étiquette
```

Déroulé : `verifier` en lecture seule sur les quatre hôtes ; `sauvegarder`
puis `migrer` sur le poste (migrations **additives** seulement, la base est
commune ; `poste.sh migrer --additif-seulement REF` garde ce refus même quand
le poste est déjà à la cible) ; puis, hôte par hôte, `installer`, `redemarrer`,
`controler` et la santé. La santé d’un hôte est celle du canon §14 : unités
redémarrées et baux
repris (`controler`), `ameesh doctor --harness` vert pour le poste, et une
sonde de courrier réelle remise à une persona menée de l’hôte
(`deploy/mise-a-jour/sante-courrier.py`) — elle prouve la chaîne
message → runner → harnais, pas seulement l’unité active. Un courrier actif
non remis depuis plus de `SOUFFRANCE_COURRIER` (900 s) est un KO.

Un échec sur un hôte déclenche son `retour` automatique (`previous-sha`),
contrôle ce retour, **arrête la séquence** et envoie un courrier urgent à
`NOTIF_STAGING` (défaut `mesh-design`). Les hôtes suivants ne sont pas
touchés ; le poste n’est atteint que si les VM sont saines. Un retour qui
échoue lui-même est signalé de la même façon. Relancer la commande après un
arrêt est sûr : un hôte déjà au commit visé n’est pas réinstallé et son
`previous-sha` est conservé.

Chaque événement est écrit en JSONL (`date`, `hôte`, `étape`, `verdict`,
`sha`, `precedent`, `detail`) dans
`~/.local/state/ameesh/mise-a-jour/staging-<date>.jsonl`. Le `detail` de
l’étape `sante` est le **rapport JSON** de `sante-courrier.py` (hôte, commit,
verdict, et pour une remise réelle le message de sonde, son destinataire et
son délai) : c’est la preuve structurée que le signal ci-dessous exige. Un
verdict `sans-sonde` — `SONDE_<HOTE>=jamais`, ou `auto` sans persona menée —
ne prouve aucune remise.

## Signal « staging actif » (#1323)

Une séquence **complète et saine** — les quatre hôtes, `installer`,
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

L’émission est bornée et **idempotente** : un seul courrier par rapporteur et
par commit, même si la séquence est relancée. La clé d’émission est écrite
dans l’attestation (champ `emissions`) **avant** l’envoi ; une relance relit
le courrier déjà déposé portant cette clé et le réconcilie au lieu de le
renvoyer. Si l’envoi échoue, la clé reste en attente et la relance renvoie
une seule fois. L’attestation est réécrite en place, par un temporaire unique
créé en `0600`.

Si le courrier ne part pas (base indisponible), l’attestation reste écrite et
`staging.sh` le signale : le relais opérateur transmet alors le journal et
l’attestation à la main.

`HOTES_STAGING` (BASCULE_ENV) déclare la topologie, une entrée par hôte, dans
l’ordre canonique ; `SONDE_<HOTE>` (majuscules, `-` et `.` remplacés par `_`)
fixe la persona sondée, par défaut la première persona menée de l’hôte.
Options : `--hotes "nom …"` (sous-ensemble, ordre canonique conservé ; noms
normalisés — un seul exemplaire par hôte, séparateurs espace, tabulation ou
nouvelle ligne ; un sous-ensemble qui contient `pc-smichea` est refusé — le
poste ne s’installe que dans la séquence complète des quatre hôtes, VM saines
d’abord),
`--lots "n …"` et `--rapporteurs "agent …"` (signal « staging actif »),
`--sans-sonde` (santé sans sonde de courrier — aucun signal « staging
actif »),
`--sans-sauvegarde`. Le script exige que `VM` soit l’adresse de
`manaty-mesh-1` ; `PERSONAS_VM` peut rester vide.

Les commandes de cette page sont à exécuter par le
responsable du déploiement depuis le poste, après revue et validation CI.
