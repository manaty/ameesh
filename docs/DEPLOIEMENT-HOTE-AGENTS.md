# Installer, redémarrer et contrôler une VM d’agents

Un hôte d’agents utilise les unités **utilisateur**
`ameesh-runner-agent@<agent>.service`, sous le compte `ameesh`.
Depuis le poste, `deploy/mise-a-jour/vm.sh agents redemarrer` les redémarre
une par une, arrêt prouvé avant chaque démarrage. `agents controler` (ou
`agents verifier`) contrôle les unités et les baux, sans redémarrer personne.
`agents installer [REF]` installe une révision gelée dans le layout du compte
et `agents retour` réinstalle la précédente (#1252).

`agents controler` prouve aussi que **chaque exécuteur vivant tourne sur le
venv installé** : le lanceur `ExecStart` est stable (#1969) et résout
`venv-courant` au démarrage, si bien qu'un processus déjà lancé garde l'ancien
venv après une bascule. Le contrôle lit l'`argv[0]` concret du MainPID dans
`/proc/<pid>/cmdline` et le compare au venv vivant ; un écart nomme l'unité et
rend le contrôle rouge. Sans cela, un hôte dont seul le CLI avait changé de
venv passait pour sain (staging du 24/10/2026, #3311).

Le périmètre de `agents redemarrer` et de `agents controler` est unique (#4437).
Les agents du registre que l’hôte mène (mode `execute`, non retirés, non
arrêtés, unité ni désactivée ni masquée) sont redémarrés avec preuve d’arrêt,
puis contrôlés (unité active, bail repris, venv). Toute autre unité
`ameesh-runner-agent@` **active** sur l’hôte — persona arrêtée (#1633), unité
désactivée encore active (résident expérimental), agent retiré ou déplacé,
candidat d’un rôle élu (#4248) — est rechargée sur le nouveau code par arrêt
prouvé puis démarrage, sans drain ni bail exigé, et son venv est prouvé. Une
unité désactivée encore active ne peut donc plus être ignorée au redémarrage
puis rejetée au contrôle (cas `pilot-dryrun-3389` du 29/10/2026). Une unité
désactivée ou masquée **inactive** n’est jamais démarrée ; une unité masquée
encore active est seulement arrêtée, systemd refusant de la démarrer. Le poste
(`poste.sh`) suit déjà le même principe : ce sont ses unités actives qui font
le périmètre, le registre ne décidant que du bail et de l’arrêt.

La liste du registre ne suffit pas : un rôle élu (§11) a un exécuteur sur
chaque hôte candidat, mais le registre ne porte que l’hôte du bail. `agents
redemarrer` et `agents controler` mènent donc aussi toute unité
`ameesh-runner-agent@` **active** sur l’hôte hors de cette liste : arrêt prouvé
puis démarrage, venv prouvé, sans drain ni bail exigé. Sans cela, le candidat
`mesh-design` d'agents-2 est resté 13 h sur l'ancien code, puis a repris le
bail sur ce code (staging du 28/10/2026, #4248).

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
retirés. Les personas arrêtées sont lues aussi, sans bail : leur unité encore
active est rechargée sur le nouveau code. Il contrôle aussi les unités
manquantes ou inactives des agents menés. Un registre illisible ou une
sélection vide fait échouer la commande. Aucun nom d’agent n’est à maintenir
dans le fichier de paramètres.

```sh
deploy/mise-a-jour/vm.sh agents verifier
deploy/mise-a-jour/vm.sh agents redemarrer
deploy/mise-a-jour/vm.sh agents controler
```

Le redémarrage ferme l’admission de chaque unité par `stop` (SIGTERM) avant
de la démarrer : le tour en cours draine, borné par `TimeoutStopSec` (35 min),
et le démarrage n’a lieu qu’après preuve du point sûr (`ActiveState=inactive`
et `MainPID=0`). Aucune fenêtre « idle » n’est attendue : une persona en tour
ne bloque plus la mise à jour.

Si l’unité est déjà en échec avant l’arrêt, `reset-failed` efface cet échec
antérieur avant `stop`. Un échec de cette remise à zéro ou du drainage, un
état final différent de `inactive` ou un `MainPID` non nul interdit toujours
le démarrage. Un nouvel échec pendant l’arrêt n’est jamais effacé.

Quand `DRAIN_ATTENTE` est supérieur à 0 (0 par défaut : désactivé), c’est un
interrupteur, pas un délai : seul l’agent courant, en tour ou au repos
(`idle`, `queued`), reçoit une pause opérateur courte et nommée (drain, #2152)
juste avant son arrêt prouvé. La pause est levée après son démarrage contrôlé.
Aucun agent suivant n’est mis en pause par anticipation (K = 0, #4000) : au
plus un drain du script existe à la fois, y compris pour les rôles élus.
Le SIGTERM laisse finir le tour courant, borné par `TimeoutStopSec` ; chaque
arrêt journalise sa durée. Si une pause reste suivie, le balayage s’arrête
avant de toucher l’agent suivant. La sortie réessaie la levée conditionnelle,
même après un échec d’arrêt, de démarrage ou de SSH, et rapporte les
`DRAINS_NON_LEVES` si le lien reste indisponible.
`--not-after` est recalculé à chaque pose : il borne l’admission de la pause,
sans faire expirer une pause déjà posée. La valeur ne mesure plus une attente d’un
créneau « idle » ; aucune fenêtre « idle » n’est attendue. Le mesh journalise
la pose et la levée (`operator_pause_events`) ; une pause déjà posée par un
autre acteur n’est ni doublée ni levée, et aucune pause posée par le script ne
survit à sa sortie. La levée est **conditionnelle** : le motif exact de la
pause posée (unique, avec son jeton) est son identité, et `ameesh resume
--operator --expect-reason` ne lève que cette pause-là — la comparaison et la
levée se font dans la même instruction SQL (migration 0458), donc une pause
étrangère posée entre la lecture et la reprise n’est jamais levée. La levée
conditionnelle reste possible si l’agent est passé `externe` pendant le
drain (migration 0459, #2184/N1) : un changement de mode ne rend pas la
pause du drain orpheline ; la pose, elle, reste refusée sur un agent
non-execute. Si la
réponse de la pause se perd (SSH coupé, JSON illisible), le script réconcilie
par l’état réel : motif identique, le drain continue ; sinon il ne redémarre
ni ne lève rien et le dit, en nommant l’agent, le motif exact et la commande
conditionnelle de levée manuelle. Une intention de pose incertaine garde son
motif : une pose de rattrapage le réutilise et un `changed=false` est relu par
l’état réel (motif identique = la nôtre) au lieu d’être classé étranger
(#3120/B2). Sa barrière reste tenue jusqu’à l’échéance même si une pose de
rattrapage est confirmée, pour exclure tout écrivain tardif (#3120/B3). Pose
et levée sont des gestes d’opérateur :
le CLI local refuse une session liée à un agent (`bad_actor`), donc `vm.sh`
doit être lancé hors session d’agent ; l’acteur d’audit consigné est
l’opérateur du poste (`--human`), jamais le compte `ameesh` de la VM
(#2184/N6). Si la pose est refusée, une persona en tour est laissée en
l’état sans redémarrage et les autres agents du balayage continuent ; une
persona au repos, qui n’a pas besoin du drain, redémarre sans lui (arrêt
prouvé) et le script le dit (#3120/B1) ; un échec de levée est consigné,
retenté à la sortie, et le code de retour le signale
(#2184/N2) ; le verdict « NON levé » n’est écrit qu’après la dernière
tentative (#2198/N3). Un CLI de
cible antérieur à #2152 (sans `--expect-reason`) ou à #2184/R3 (sans
`--not-after`) n’est jamais utilisé pour
drainer : le script arrête l’agent sans drain plutôt que de laisser une pause
orpheline. La pose porte une échéance absolue (`--not-after`) comparée par
la base sous le verrou de ligne : une commande distante retardée est refusée
(`refusal=not_after`). Pour une pose incertaine, le script attend l’échéance,
la borne d’instruction (`DRAIN_BORNE_INSTRUCTION`, 30 s) et la dérive
d’horloge (`DRAIN_DERIVE`, 5 s) avant de relire l’état et de reprendre. Les
appels de drain sont bornés (`DRAIN_APPEL_DELAI`, 60 s), transport SSH
compris, et couverts par un keepalive (#2184/N3) ; leur borne locale vaut
`DRAIN_APPEL_DELAI + DRAIN_MARGE_LOCALE` (20 s), pour que le `timeout`
distant rende son code avant que le client ne tue le groupe (#2198/N2). Les
lectures (`list`, `show`, `--help`) ont leur propre borne, plus courte
(`DRAIN_LECTURE_DELAI`, 30 s — #2198/N4). Un signal reçu par le client
(SIGINT/SIGTERM) est transmis au groupe de l’enfant, lancé dans sa propre
session, qui n’est donc pas orphelin (#2198/N1). Il relit le placement et
le mode avant chaque redémarrage, y compris en mode serveur (#2198/N5) :
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
d’hôte pendant le contrôle fait échouer la preuve, sauf pour un rôle élu
repris par un autre hôte candidat à une époque supérieure : c’est la relève
normale du §11, consignée en WARNING (reprise locale non concluante) sans
retour arrière. Une réservation `@restart` écrite par l’exécuteur arrêté
n’y change rien : un hôte encore sur l’ancien code l’ignore et elle expire
en 90 s. L’échec tient si aucun autre hôte candidat ne détient un bail
vivant au moment du contrôle. Une persona dont l’unité est désactivée
(`UnitFileState=disabled` ou `masked`, résident expérimental) n’a pas
d’exécuteur actif attendu : ni unité active ni bail ne sont exigés, et une
unité inactive n’est jamais démarrée. Si elle tourne encore, elle est rechargée
sur le nouveau code (arrêt prouvé puis démarrage) et son venv est prouvé, comme
toute unité active hors du périmètre mené (#4437) ; une unité masquée encore
active est seulement arrêtée, systemd refusant de la démarrer. Un agent retiré
du registre (`retired_ts` dans `ameesh list`) est également hors périmètre ;
s’il tourne encore, la même règle le recharge. Une unité absente ou illisible
reste une erreur. En cas d’échec, la commande indique l’unité et la commande
`journalctl --user` à lancer sous `ameesh` sur la VM.

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

Sur un hôte d’agents, le banc Pg local (`scripts/test.sh`, conteneur
`ameesh-pg` sur `127.0.0.1:55432`) lit `~/.pgpass` (0600) : sans l’entrée
`127.0.0.1:55432:agent_mesh:agent_mesh:<mot de passe du conteneur>`, les
tests Pg ciblés échouent en `fe_sendauth` (#4065). Le mot de passe n’est
jamais affiché ni versionné.

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

`installer` prépare ensuite le venv versionné du code visé et bascule le lien
`venv-courant` ; à la fin d’une bascule réussie, `bascule.py menage` retire les
anciens `venv-<sha7>` en gardant la cible de `venv-courant`, le nom de
`venv.precedent` et tout venv qu’un processus vivant nomme par un chemin
absolu. Le lien `venv-courant` lui-même n’est jamais candidat (#3792).

Après une installation réussie, et seulement en mode agents, `installer` retire
l’agent GnuPG inutile de la VM : si `~/.gnupg/private-keys-v1.d` est absent ou
vide, il masque les sockets utilisateur `gpg-agent.socket`,
`gpg-agent-ssh.socket`, `gpg-agent-extra.socket`, `gpg-agent-browser.socket` et
`keyboxd.socket` (`systemctl --user mask --now`) puis tue l’agent (`gpgconf
--kill gpg-agent`). Une clé privée présente, ou un état illisible, laisse tout
en place et le journalise : le doute reste. L’étape est idempotente (une
relance d’`installer` la rejoue) et vient après l’installation du code : #3195,
dont ce lot dépend, garde les worktrees de lots ouverts avant que ce retrait ne
débloque le ménage des sorties de build. `poste.sh` n’y touche jamais : sur le
poste de l’owner, `gpg-agent` tient ses clés.

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
signal ci-dessous exige. Le rapport sépare les causes de courrier non remis :
`souffrance` (courrier actif chez une persona **menée** et libre, donc retard
de remise), `sans_executeur` (courrier actif chez une persona opérationnelle
sans bail vivant : aucun exécuteur ne peut ouvrir son tour), `boites_mortes`
(personas opérationnelles arrêtées, non bloquant) et `canal_humain` (sessions
humaines sans exécuteur, porte-clé compris, non bloquant). L’événement porte
aussi ce rapport comme objet
`rapport`, et une notification d’échec le reprend en clair : un KO de sonde
garde son message, son destinataire et son motif, jamais un simple « santé en
échec » (#2190). Un verdict `sans-sonde` — `SONDE_<HOTE>=jamais`, `auto` sans
persona menée, ou toutes les personas menées occupées ou en pause opérateur —
ne prouve aucune remise.

La sonde ne vise qu’une persona **menée et libre** (bail vivant, pas en tour,
pas en pause opérateur) : une persona occupée ne reçoit le courrier qu’à la fin
de son tour, et une persona en pause garde `status` `idle` mais son exécuteur
refuse le tour ; la sonde mesurerait leur file d’attente, pas la chaîne de
l’hôte. Si aucune persona menée n’est libre, le verdict est `sans-sonde` et
nomme les personas occupées ou en pause ; le délai de 300 s ne s’applique qu’à
une remise partie vers une persona libre, et exige toujours la réponse outil et
la fin propre du tour (#2190). Le courrier actif en attente chez une persona
en pause n’est pas compté comme bloqué : son tour est refusé par construction.
Le courrier actif d’une persona **opérationnelle sans bail vivant** n’est pas
une souffrance de la chaîne : aucun exécuteur ne peut ouvrir son tour. Il est
néanmoins un KO, dans `sans_executeur`, parce qu’un courrier opérationnel doit
avoir un destinataire actif ; le rapport nomme le statut, l’âge du plus ancien
message et le temps depuis le dernier tour, et la sortie est de démarrer
l’exécuteur ou de relancer l’agent (`ameesh resume <agent>`), jamais d’élargir
le délai ni de retirer la garde. Un agent `blocked` sans bail vivant et hors
pause budget est nommé avec son erreur, jamais silencieusement omis. Une
session humaine sans exécuteur (`mode=externe`, dont le propriétaire porte-clé
`key_role=owner` ou la ligne de secours `harness=none`) ne compte ni comme
boîte morte ni comme souffrance : son courrier est listé dans `canal_humain`,
avec l’humain responsable, l’alerte poussée (`stopped_with_mail` ou
`idle_with_mail`) et la lecture sans marquage (`agent-mail inbox <nom>`) ; ce
courrier ne bloque pas la séquence. Un profil sans responsable déclaré prend le
canal humain du propriétaire, lu dans le registre (`key_role=owner`) : le
rapport ne laisse plus `canal: "human:?"` quand le propriétaire est connu. Une
session de chat (`chat-<humain>`) reste un canal humain, mais elle lit son
courrier à l’ouverture de la conversation : son entrée porte `canal: "chat"` et
`alerte: null`, jamais `idle_with_mail`/`stopped_with_mail` qu’aucune alerte
poussée ne couvre (#2957). Les profils humains sont aussi exclus de
`sans_executeur`, même si une ligne `mode=execute` portait `key_role=owner` ou
`harness=none` : ils ne sont jamais comptés deux fois (#2957).

## Signal « staging actif » (#1323)

Le signal est porté par
[`deploy/mise-a-jour/staging.sh`](../deploy/mise-a-jour/staging.sh) : une
séquence **complète et saine** — les quatre VM, plus le poste s’il est en ligne,
`installer`, `redemarrer`, `controler` et sonde de courrier réellement remise —
écrit une attestation durable sous
`~/.local/state/ameesh/mise-a-jour/actif-<commit>.json` et réveille les
rapporteurs déclarés avec `--lots "917 988"` et
`--rapporteurs "mty-amh-docs-01"` (ou `LOTS_VERIF` / `RAPPORTEURS_VERIF` dans
`BASCULE_ENV`).

La séquence est lançable **depuis une VM** (bureau-goal ou une VM d’agents),
pas seulement depuis le poste. Le poste ne se met à jour qu’en local
(`poste.sh`, unités systemd du poste) : exécutée ailleurs, `staging.sh` le
déclare absent (`absent`/`reporte` dans le journal), saute ses étapes
`verifier`, sauvegarde, `role`, `installer`, `redemarrer`, `controler` et
`sante`, et n’attend pas son retour. Les hôtes joignables de la séquence sont
déployés normalement ; l’attestation porte leurs preuves et le champ
`reportes` nomme le poste, la raison et sa mise à jour « à son retour, jamais
en dessous ». Un poste absent ne retient ni staging ni lots ; un poste présent
mais omis de `--hotes` reste une séquence partielle, sans attestation. Un
**serveur sans accès root** (`manaty-mesh-1`, aucun exécutant, sauvegarde
seulement) peut être reporté de la même façon avec
`--absent manaty-mesh-1=raison` : les trois VM d’agents restent requises et
attestées, le serveur est nommé dans `reportes` ; sans cette option, la
séquence exige les quatre VM. Les migrations sont alors appliquées
depuis une VM d’agents (`HOTE_MIGRATIONS`, par défaut `ameesh-agents-2`) :
instantané `git archive` de `src`, `ameesh migrate --dry-run --additive-only`
puis `--additive-only`, comme le chemin bureau-goal ; un échec arrête avant
toute installation. Le clone de travail des migrations (`DEPOT_VM`, par défaut
`~/work/ameesh-release`) doit exister sur `HOTE_MIGRATIONS`, sous le compte
`ameesh` : le préflight le vérifie avant la sauvegarde, les migrations et tout
arrêt d’exécuteur ; absent ou illisible, la séquence s’arrête sur un refus
nommé qui donne le geste exact, sans retour automatique et sans rien modifier.
Provisionner un hôte neuf comprend donc ce clone complet (jamais `--depth` ni
`--shallow`, canon §7) :

```sh
ssh root@<vm> 'runuser -u ameesh -- git clone https://github.com/manaty/ameesh.git /home/ameesh/work/ameesh-release'
```

Un `DEPOT_VM` différent se déclare dans l’environnement du lanceur. Lancée hors
du poste et des VM d’agents (bureau-goal,
sans venv ameesh ni route vers la base), la séquence fait tourner la sonde de
courrier, la notification **et `actif.py`** sur `HOTE_OUTILS` (par défaut
`HOTE_MIGRATIONS`), par ssh, sous l’utilisateur `ameesh`, avec l’instantané du
commit ; le journal de la séquence lui est recopié (#3976, #3981). Une
sonde qui **n’a pas pu tourner** (python absent, code 126/127, ssh en échec,
base injoignable : verdict `non-concluant`) est non concluante (canon §3) :
aucun retour de l’hôte, trace `sante`/`non-concluant`, séquence arrêtée en 3
et notifiée ; seule une sonde qui a tourné et constaté un défaut vaut
« malsain ». La sauvegarde locale du poste est remplacée par un dump
du serveur seul : `ameesh-sauvegarde.service` lancé sur `manaty-mesh-1`, un
nouveau dump exigé (chemin, taille, SHA-256 dans la trace), gardé sur la VM
sans rapatriement ; un échec arrête avant les migrations ; serveur reporté
(`--absent manaty-mesh-1=…`), le dump est sauté, sans autre événement à son
nom que `absent`/`reporte` (une autre étape contredirait l’absence). Nommé dans
`--hotes` alors qu’il est absent, le poste est ignoré : la sélection est celle
des VM nommées. Le tick de release ne relance une séquence déjà attestée avec
le poste reporté que s’il tourne sur le poste (même détection d’hôte), pour le
rattraper ; ailleurs, il ne la rejoue pas. L’hôte local est `HOTE_LOCAL`, sinon
`AMEESH_HOST`, sinon le champ `host` de la configuration ameesh, sinon
`hostname -s` ; `HOTE_LOCAL=pc-smichea` dans `BASCULE_ENV` lève toute
ambiguïté sur une VM dont la configuration recopierait celle du poste.

Sans `HOTES_STAGING` (bureau-goal n’a qu’un fichier par VM d’agents), la
topologie est composée des `bascule-a1.env`, `bascule-a2.env` et `bascule-agents-3.env` du dossier de
`BASCULE_ENV` (`AGENTS_SSH`, `HOTE_AGENTS`) et de `SERVEUR_SSH` (ou `VM`)
pour `manaty-mesh-1` ; `pc-smichea` reste absent et reporté. Un
`SERVEUR_SSH` absent est refusé explicitement, sans adresse supposée.

Sur le poste, `redemarrer` et `retour` passent par des unités transitoires
`poste-maj-staging-<pid>-<action>` (`systemd-run --user --collect --wait --pipe`,
#3039). Le staging attend leur résultat et conserve leur sortie dans son
journal ; un échec suit le retour automatique puis la notification habituels.
Cette enveloppe dans `staging.sh` couvre aussi le retour depuis le tick
`ameesh-release.service`, sans modifier la garde #2950 de `poste.sh` qui
refuse les appels directs depuis une unité `ameesh-*`.

**Sans retour automatique (`--sans-retour[=ROLLBACK]`, #4616).** Après une
migration non additive (fenêtre de migration, go owner), réinstaller le
`previous-sha` sur la base déjà migrée serait dangereux : l’ancien code lit mal
le nouvel état. Avec `--sans-retour`, un échec d’hôte n’appelle jamais
`retour` : il est tracé (`retour`, verdict `desactive`), la séquence s’arrête
sur cet hôte et le courrier urgent au pilote donne la marche manuelle. L’hôte
en échec n’est pas ramené : son commit actif est à vérifier (le commit visé si
la bascule a eu lieu, l’ancien si `installer` a échoué avant). Marche
manuelle, dans cet ordre : arrêter les exécuteurs des hôtes déjà passés ;
**rollback SQL d’abord**, en une transaction (`ROLLBACK` nommé en argument, par
exemple
`--sans-retour=src/ameesh/migrations/rollback/0518_generic_delivery_state.sql` ;
sans argument, le courrier renvoie au rollback de la migration posée dans
`src/ameesh/migrations/rollback/`) ; **puis** retour de code de chaque hôte déjà
passé (`vm.sh agents retour`, `vm.sh retour` ou `poste.sh retour`, avec le
`BASCULE_ENV` de l’hôte) et `controler` ; enfin relancer les exécuteurs et
vérifier (doctor, courrier). Une séquence saine menée ainsi passe
`--sans-retour` à `actif.py` : l’attestation, sa copie partagée et le courrier
portent « retour automatique désactivé ». Sans l’option, rien ne change.

Le courrier nomme les hôtes prouvés, le commit **relu actif** sur chacun après
la séquence, les preuves `installer`/`redemarrer`/`controler`/`sante`, les lots
concernés et, s’il y a lieu, le poste reporté avec sa raison. Un échec, un
retour automatique, une séquence partielle de VM (`--hotes`), un journal d’une
autre exécution ou une santé sans sonde de remise réelle (`--sans-sonde`,
`SONDE_<HOTE>=jamais`, `auto` sans persona menée, ou toutes occupées ou en
pause) n’émettent **aucun signal de réussite** :
chaque événement de la fenêtre doit porter le commit visé, et le message de sonde est
relu en base
(expéditeur système, destinataire, hôte, commit, remise effective). Aucune
clôture de lot n’est automatique : le rapporteur reprend lui-même sa
vérification après déploiement.

Un hôte **sans exécutant** (mode serveur, comme `manaty-mesh-1`) ne peut pas
prouver sa chaîne par une remise locale : sa preuve est le `controler` vert
plus une **sonde croisée**, remise à une persona menée et libre d’un autre hôte
canonique **après son redémarrage** — la remise traverse alors sa base, le
message étant déposé dans la base du serveur et relevé par l’exécuteur de
l’autre hôte. `sante-courrier.py --serveur` produit cette sonde et nomme
l’hôte destinataire (`hote_destinataire`) ; `actif.py` relit la remise en base
et exige qu’elle soit postérieure au `redemarrer` de l’hôte mesuré. Un serveur
qui porte des exécutants garde la règle locale : une santé `sans-sonde`
(aucune persona menée libre) n’atteste rien, et une sonde croisée n’est
acceptée que pour un hôte à zéro persona.

L’émission est bornée et **idempotente** : un seul courrier par rapporteur et
par commit, même si la séquence est relancée. La clé d’émission est écrite
dans l’attestation (champ `emissions`) **avant** l’envoi ; une relance relit
le courrier déjà déposé portant cette clé et le réconcilie au lieu de le
renvoyer. Cette recherche est **ciblée** : le destinataire, l’expéditeur
système et la clé sont filtrés dans la requête, puis la première ligne est
lue (`LIMIT 1`), sans fenêtre sur l’historique de la boîte et sans plafond —
une boîte de plus de 20 000 courriers ne cache donc plus un dépôt ancien.
Une absence n’est retenue que si cette lecture aboutit à vide. Si la lecture
échoue (base injoignable, erreur de requête), l’absence n’est pas prouvée :
la marque d’émission est **préservée** et rien n’est renvoyé
automatiquement ; `actif.py` sort en code 5 et `staging.sh` le signale, le
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

## Minuteries de rôle de l’observation GitHub (#3327)

L’observation GitHub (veille des tickets, relevé CI, projection des issues) est
portée par la minuterie d’un rôle élu, `mesh-design` par défaut, et non plus par
le seul service `ameesh-notify` de l’hôte désigné. Après `installer`, poser les
minuteries sur chaque VM candidate :

```sh
deploy/mise-a-jour/vm.sh agents timers
deploy/mise-a-jour/poste.sh timers        # sur le poste, autre candidat
```

`ameesh role timers --install` n’écrit rien sur un hôte qui n’est pas candidat.
Sur un candidat, il complète les seuls couples projet→dépôt manquants depuis
les cartes `github_watch` du canon (`nexlink` → `manaty/nexlink` sur une VM qui
n’avait que `ameesh`), après sauvegarde `config.json.role-timers.bak` et sans
écraser un mapping existant ; `--no-sync` le désactive. Le reste de la clé
`github` (`workflows`, `base`, `exclude_terms`) reste à copier à l’identique
depuis la configuration de l’observateur historique : le relevé CI de Nexlink a
besoin de `base: [develop]`. Ce n’est pas un secret ; le jeton `GH_TOKEN` reste
dans `notify.env`. Le drop-in posé fait exclure à `ameesh-notify` les passes
portées par les minuteries (`--skip issues,github-watch,ci,alerts`) : la prise
reste dans le service, GitHub, la CI et la livraison des alertes passent par
la minuterie du rôle. #3383 : la minuterie `alerts` exige sur chaque candidat
la clé `notify` de l’owner (route `ntfy`, jeton dans `notify.env` en 0600),
identique d’un candidat à l’autre : le responsable du rôle et
`notify.default_human` doivent y avoir un canal `ntfy` ou `slack` utilisable
(`desktop` et `app` ne comptent pas) ; sinon `timers` et `controler`
nomment l’humain et la raison en avertissement, sans échouer (#3402, canon
§3 : un secret absent ne fait pas retomber le staging en arrière). Un
candidat sans route ne porte pas la passe `alerts` : si le bail lui échoit,
l’un des candidats routés l’exécute (#3568, `role run --pass alerts`), et
`ameesh role status` le nomme.

`controler` (`vm.sh agents controler`, `poste.sh controler`) exécute
`ameesh role timers --check` et échoue sur une minuterie absente, désactivée,
hors d’état (contenu différent) ou un drop-in manquant. Voir
[EXPLOITATION.md, « Pose automatique »](EXPLOITATION.md#pose-automatique--ameesh-role-timers-3327).

## Release automatique aux coupures (#2448)

Pour les correctifs fusionnés qui retiennent plusieurs lots entre deux
coupures, voir [le relevé et le geste staging hors créneau](STAGING-ATTENTES.md).
La coupure conserve le contenu actif si sa candidate verte est plus ancienne.

À 11:00 et 23:00 (Europe/Paris), la tête de `main` est taguée et déployée en
staging sans agent. Le calendrier n'est pas écrit dans une unité systemd : il
vient du bloc mécanique du canon compilé (`processus/mecanique.yaml`,
`mechanics.cutoffs`), lu au commit exact du canon appliqué. Sans ce document,
la commande refuse — aucun repli sur une heure locale.

**Configuration de l'hôte opérateur** (pc-smichea seulement) :

```json
"release": {
  "host": "pc-smichea",
  "projects": {
    "ameesh": {
      "repo": "/home/ameesh/work/ameesh",
      "target": "main",
      "github": "manaty/ameesh",
      "report": "smichea",
      "staging_timeout_s": 7200
    }
  }
}
```

`repo` est un clone dédié du projet, propre et à jour (le service y crée un
worktree de version, pousse la cible et lance `deploy/mise-a-jour/staging.sh`
depuis un worktree du commit publié). `staging` est facultatif (défaut
`deploy/mise-a-jour/staging.sh`, relatif à `repo`). `window_s` (défaut 1800)
borne le retard toléré après une coupure ; `lock_wait_s` (défaut 3600) borne
l'attente du verrou d'écriture de la cible. `version_bump` vaut `level` (le
plancher de `scripts/version-level.py`, appliqué par le pilote pour #2448) ou
`patch` (dérogation explicite) ; un niveau `major` n'est jamais publié sans
go owner. Le **coupeur** reste sur le poste ; quand il est éteint, le staging
des VM peut être lancé à la main depuis une VM (bureau-goal) avec le même
`BASCULE_ENV` — `HOTE_LOCAL` y nomme la VM, jamais `pc-smichea` —, ce qui
atteste les quatre VM et reporte le poste à son retour (`#3848`).

**Activation** (une fois, par l'opérateur) :

```sh
install -m 0644 deploy/systemd/ameesh-release.service \
                   deploy/systemd/ameesh-release.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ameesh-release.timer
ameesh release calendar                # prochaine coupure et canon lu
ameesh release cutoff --dry-run        # plan, notes, aucun effet
```

**Alerte « créneau manqué » (#2601)** : le même tick relit la cadence des
paquets (`packets.cutoffs`) et réveille le composeur et le pilote quand un
créneau passe sans paquet alors que des lots sont prêts (voir
[ORCHESTRATEUR.md](ORCHESTRATEUR.md)). Sans bloc `packet_watch`, rien ne
tourne : un #2601 fusionné reste éteint (canon §5). Le bloc s'ajoute à côté de
`release`, sur l'hôte opérateur seulement ; aucune unité de plus :

```json
"packet_watch": {
  "host": "pc-smichea",
  "projects": {
    "ameesh": {
      "notify": ["<composeur>", "<pilote>"],
      "grace_s": 900,
      "target": "main",
      "repo": "/home/ameesh/work/ameesh"
    }
  }
}
```

`repo` est requis quand l'hôte n'a pas de bloc `radar` (cas de pc-smichea) :
sans lui, chaque tick échoue sur la liste calculée (`project 'ameesh' is in
neither radar.projects nor packet_watch.projects.ameesh.repo`). Le clone de
`release.projects.<projet>.repo` convient : il n'est lu qu'à travers un clone
jetable `--shared`. `target` évite de dépendre de `origin/HEAD`. Vérification
après l'ajout :

```sh
ameesh release packet-watch --dry-run      # grace, busy, served, nothing-ready, would-alert… ; jamais error
journalctl --user -u ameesh-release -n 5   # une ligne « packet-watch ameesh: … » par tick
```

**Retirer le coupeur d'un hôte** : retirer son bloc `release`, jamais couper
`ameesh-release.timer`. Le packet-watch tourne dans le tick même sans bloc
`release` (le tick affiche `release: off` puis exécute le packet-watch seul) ;
désactiver la minuterie arrête aussi la fusion automatique des paquets — le
geste du 24/10 14:31Z sur agents-2. `ameesh doctor` signale un hôte
`packet_watch` dont `ameesh-release.timer` n'est pas actif, ainsi qu'un dépôt
`radar.projects.<projet>.repo` auquel il manque une tête gelée (checkout non
fetché, désignation au gel impossible).

**Ce que fait un tick** : il prend `B`, la tête de la cible si la CI du
commit exact est verte, sinon le plus récent commit vert de la chaîne
first-parent (un commit de release `[skip ci]` hérite de l'état de son premier
parent). Il calcule le niveau de version (`level <dernier tag> B`), génère
`docs/versions/<version>.md` depuis les manifestes des paquets fusionnés (lots
inclus et écartés), puis crée `V` sur `B` : version + notes, `[skip ci]`, avec
le trailer `ameesh-release:`. Si la cible vaut encore `B`, `V` part en
fast-forward ; sinon un commit de fusion `M = merge(main, V)` porte la version
et les notes sur la tête courante, sans réécrire `main`. `V` et l'étiquette
`v<version>` partent dans **une seule poussée atomique**, sans force : un rejet
donne un seul recalcul, puis un rapport. Le staging déploie **le tag** (le
contenu vert `V`), jamais la tête de `main`, puis crée la release GitHub. Un
retour automatique de l'hôte en échec est celui de `staging.sh`. Le rapport
part à l'owner (`report`) avec la version, les lots, les écarts et la santé par
hôte ; il est aussi écrit sous `~/.local/state/ameesh/release/reports/`.

**Idempotence** : l'état local
(`~/.local/state/ameesh/release/state.json`) évite de reprendre une coupure
déjà faite ; les faits durables sont l'étiquette et l'attestation
`actif-<sha>.json`. Une étiquette créée à la coupure ou après, une release
GitHub manquante ou un staging interrompu sont **réconciliés** au tick suivant
sans publier une seconde version. La release prend le **même** bail d'écriture
que le train (#2446), `land.lock_key(project, 'main')`, avec un owner propre et
sans partage d'epoch ; elle attend qu'il soit libre dans sa borne avant la
poussée atomique, puis le rend. `staging.sh` refuse deux séquences simultanées
(code 75, `staging.lock`).

**Preuve** : le journal `~/.local/state/ameesh/mise-a-jour/staging-<date>.jsonl`,
l'attestation `actif-<sha>.json` (quatre VM et sonde de courrier remise, plus le
poste quand il est en ligne ou le champ `reportes` sinon) et le rapport JSON du
tick. Depuis une VM, cette attestation est écrite sur `HOTE_OUTILS` et
`actif.py` dépose aussi une forme compacte dans le journal partagé
`ameesh_events` (type `staging.attested`) ; le poste qui fait la coupure la
relit sans ssh et la matérialise dans son état local (#3981). Aucune prod : le
staging n'est pas une release de prod (canon §14), un go owner reste requis
pour la prod.

La recette PgBouncer autorise aussi `ameesh-agents-3` sous l’utilisateur
`ameesh`. Elle prévoit pour cet hôte 32 connexions ordinaires et 8 de réserve
(`max_db_connections = 40`), et conserve un plafond de 80 pour chacun des
trois hôtes de runners antérieurs. Si ces quatre hôtes passent exclusivement
par ces pools, leur budget cumulé est de 280 sous le plafond serveur de 300.

Au relevé en lecture seule du bureau du 27/10/2026 vers 11:00Z (courrier
#150697), seul `pc-smichea` dispose de PgBouncer, plafonné à 80 ; les trois
VM d’agents se connectent directement à Postgres. Les connexions observées
pour ameesh étaient respectivement de 60 sur agents-1, 50 sur agents-2,
14 sur le poste et 0 sur agents-3 ; ce relevé ponctuel ne borne pas leur
croissance. Le budget théorique de la recette ne plafonne donc pas la flotte
déployée. Cette modification n’installe aucun pool et ne redimensionne aucune
installation ; une installation ultérieure exige de vérifier aussi les
connexions directes et les autres clients du serveur.

### Suivi indépendant et exécutant élu du staging (#4880)

Avant toute modification d'hôte, `staging.sh` écrit un événement partagé
`staging.sequence`, avec un identifiant d'exécution et une échéance fixe
(`STAGING_TIMEOUT_S`, 7200 secondes par défaut). Chaque étape publie la
dernière étape et le verdict par hôte ; les sorties, commandes et secrets
restent hors de ce suivi. La passe ordinaire d'alertes lit ces événements
depuis la base : un lanceur tué, figé ou privé de réseau n'a pas besoin de
survivre pour que le dépassement soit signalé au pilote. Une sortie sans
attestation reste `unattested`, jamais `completed`. Un sous-ensemble demandé
explicitement se termine `partial`, sans prétendre attester le mesh.

Le chemin avec relève utilise `ameesh.staging_executor` : la demande vit dans
`staging.request`, et un service utilisateur dédié l'exécute depuis un arbre
git du commit exact. L'agent qui soumet la demande ne lance aucun SSH de
déploiement dans son tour. Installer `deploy/systemd/ameesh-staging.service`
et `.timer` sur **chaque candidat**, avec une configuration privée
`~/.config/ameesh/staging-executor.json`, par exemple :

```json
{
  "key": "mesh-staging",
  "project": "ameesh",
  "role": "mesh-design",
  "candidates": ["candidat-a", "candidat-b"],
  "repo": "/chemin/du/clone-dedie",
  "target": "origin/main",
  "bascule_env": "/chemin/prive/bascule.env",
  "arguments": ["--sans-poste"],
  "timeout_s": 7200,
  "start_timeout_s": 300,
  "quiescence_probe": ["/chemin/prive/prouver-arret-staging"]
}
```

Les candidats sont des noms d'hôtes de configuration, jamais des constantes
du code. Ils doivent avoir le même `key`, le même rôle élu, accès à la base,
un clone dédié contenant le commit demandé et les accès de déploiement
validés (`bascule*.env`, clés et droits nécessaires). Cette documentation
ne place aucun secret et ne prouve pas que ces accès existent.

La minuterie passe chaque minute ; seul l'hôte qui détient le bail vivant
du rôle commence une tentative. Un second bail partagé, renouvelé avec une
échéance monotone locale, protège l'exécution pendant un changement de rôle.
Une demande en attente qui n'est pas prise dans `start_timeout_s`, un refus
de reprise ou un échec lève une alerte urgente au pilote.

Le programme `quiescence_probe` doit vérifier **et terminer si nécessaire**
l'ancien exécutant et ses opérations distantes, y compris les unités
transitoires du poste. Il reçoit `STAGING_PREVIOUS_HOST` et
`STAGING_PREVIOUS_RUN`, et ne rend zéro qu'après preuve de leur arrêt. Un
PID local absent ou un bail expiré ne suffit pas : les SSH et unités
distantes peuvent survivre. Sans programme, avec un résultat négatif, ou
avec des accès manquants, la reprise refuse toute modification et publie
son blocage. L'arbre source d'une exécution qui perd son bail est conservé.

Une fois la configuration et les accès approuvés, l'opérateur active les
services sur les candidats et soumet un SHA complet :

```sh
systemctl --user enable --now ameesh-staging.timer
python3 -m ameesh.staging_executor \
  --config "$HOME/.config/ameesh/staging-executor.json" submit SHA_COMPLET
```

`ameesh-staging.service` est une unité `app.slice`, hors du cgroup du tour.
Vérifier son `ControlGroup`, son journal et les événements partagés lors de
la qualification réelle. Pour la release automatique, renseigner
`release.projects.<projet>.staging_executor` avec le chemin de ce JSON : la
coupure soumet la même demande et attend l'attestation partagée au prochain
passage. Sans cette option, le chemin historique reste synchrone dans
`ameesh-release.service` et bénéficie du suivi indépendant de `staging.sh`.

Les deux voies ne se chevauchent pas (#4885). Le verrou `flock` de
`staging.sh` ne vaut que pour un hôte, et le bail d'exécution ne couvre que
l'exécutant élu ; le début de **chaque** séquence (exécutant élu, lancement à
la main depuis n'importe quel hôte, release synchrone) passe donc par une
écriture exclusive du premier événement `staging.sequence`, sous verrou
consultatif de la base : tant qu'une autre séquence est `running`,
`staging.sh` sort en 75 sans rien modifier, et l'exécutant élu publie
l'attente (`another staging sequence running`, alerte au pilote) sans geste
d'hôte, puis reprend la demande au passage suivant. L'échéance d'une
séquence alerte le pilote mais ne libère pas l'exclusion : rien ne termine un
`staging.sh` lancé à la main à cette échéance, et il peut encore modifier
des hôtes. Seuls un état terminal ou `STAGING_PREVIOUS_RUN=<run>` la lèvent,
pour une séquence dont l'arrêt est prouvé : l'exécutant la pose après sa
sonde de quiescence ; l'opérateur qui relance à la main, ou qui débloque la
release synchrone (elle attend en `ReleaseBusy` d'ici là), ne la pose
qu'après la même vérification. La séquence remplacée est alors close en
`failed`, dans la même transaction. Un début est toujours une nouvelle
séquence : un identifiant déjà présent dans le journal (`STAGING_RUN_ID`
hérité ou réutilisé, même terminé) est refusé en 75, sinon deux lanceurs qui
le partagent passeraient tous deux.

Le retour du premier hôte puis la reprise par un second candidat sont
testés avec des hôtes simulés. La qualification physique et la mise en place
des accès sur plusieurs hôtes restent des opérations distinctes : ne pas
confondre ces tests avec une relève effectivement installée.
