# Adaptateur VM, installation par le bureau

Ce lot livre le code et une installation préparée, sans activation ni geste
cloud. Le bureau conserve la clé et son détenteur actuel. Ne pas créer de
compte cloud, d'utilisateur agent ou de copie de clé sur les runners.

## Contrat

Le hook des runners est `python -m ameesh.vm_adapter request`. Le scanner
ajoute `<busy|draining|idle-drained> <hôte>` et la génération du drainage.
Le hook enregistre une demande idempotente en base. Il répond `0` seulement
si le worker a déjà réalisé l'état, `75` (`EX_TEMPFAIL`) quand la demande est
en attente, et `1` en cas de refus : une demande en attente est une **attente
asynchrone**, pas une panne. Le scanner enregistre alors `hook_wait_*`, garde
l'état acquitté précédent et les prises fermées ; il n'écrit ni
`release_error` ni `host_release_failed`, et ne rouvre rien. Le worker
inscrit la réalisation dans le journal natif, sous la même
génération/époque ; une demande en attente n'est jamais un acquittement de
mise en veille. L'état et le motif borné sont dans `vm_adapter_requests`.
L'intention d'arrêt est persistée avant l'effet cloud, pour que la détection
de perte ne confonde pas l'archivage volontaire avec une panne. L'attente est
bornée à la génération/époque qui l'a produite : un écrivain N-1 qui change
l'une ou l'autre sans connaître `hook_wait_*` rend l'attente périmée, et la
détection de perte cesse de l'ignorer (#5690). Le worker solde au tick suivant
les demandes d'une vague révolue. Le worker termine la transition native sans
dépendre d'une nouvelle tentative dans le délai de drainage : l'hystérésis de
facturation a son propre délai. Seule cette demande déjà validée permet au
worker de prolonger l'attente ; les preuves de repos, de fraîcheur et
d'absence de travail sont revérifiées avant l'arrêt.

Une intention d'arrêt durable qui ne progresse plus au-delà de
`AMEESH_HOST_RELEASE_ADAPTER_STALL` (900 s par défaut) lève
`host_release_adapter_stalled`, comme un worker dont aucun tick
`vm_adapter_observations` n'est vu depuis ce délai, ou une intention dont la
génération/époque ne correspond plus au contrôle courant (le worker ne l'a
pas réconciliée). Ce dernier cas n'alerte que tant que la porte est fermée et
l'hôte en retrait : une fois le retour acquis, le worker solde la demande
périmée et l'alerte disparaît. Ces alertes nomment le worker et le couple
génération/époque ; elles ne demandent pas de retirer le hook d'une VM
encore arrêtée.
La même alerte part sans attendre (#6074, motif `halt_deadline`,
`archive_refused`, `provider_locked` ou `stopped_in_place`) quand l'arrêt de
l'OS n'est pas prouvé à l'échéance, quand l'archivage n'est pas permis, quand
la VM est `locked`, ou quand elle est en veille sans intention halt de la
vague.
Un retour bloqué en veille (#6080, motif `poweron_refused`) alerte une fois
passé `host_release_adapter_stall` depuis le premier refus : `allowed_actions`
ne permet pas `poweron` (ou manque). La VM n'est pas démarrée, les claims
restent fermés ; dès que `poweron` est permis, le démarrage reprend.
Un canon refusé alerte aussi sans attendre le seuil : `canon unavailable`
quand un canon configuré ne se lit pas (le synchroniser puis le vérifier avec
la version du worker), `no canon configured` quand la configuration du service
n'a pas de clé `canons` (déclarer le clone et sa référence dans le fichier
pointé par `database.env`, que lit aussi la veille GitHub). Aucune VM n'est
arrêtée sans canon lisible.

Un seul hôte opérateur, externe aux VM qu'il gère, exécute le timer. Son
inscription privée fixe les UUID VM/projet et la zone ; aucune identité,
commande ou clé cloud ne vient de la file. Les exécutions concurrentes
prennent un verrou transactionnel par hôte ; une transaction tient le verrou
natif de porte pendant les appels bornés pour empêcher une réouverture entre
la dernière preuve de drainage et l'effet cloud. Chaque appel externe est
borné à dix secondes au maximum. Les contrôles de santé et de réconciliation
s'exécutent hors de ce verrou, puis génération et relevé frais sont revérifiés
sous verrou avant l'acquittement. Un tick ne boucle ni sur l'API ni sur la santé.

L'arrêt se fait en deux phases (#6074), sous la même génération/époque, et
chaque effet est précédé d'une intention durable (marqueur `result` de la
demande `idle-drained`) :

1. `poweroff intent` : hystérésis passée, tentative native durable.
2. `halt intent` : preuves de repos et d'élection revérifiées, `quiesce_argv`
   à 0. Au tick suivant, preuves et quiesce sont revérifiées, puis
   `halt_argv` s'exécute une seule fois (`halt sent`). Son code de retour ne
   prouve rien. La doc Scaleway demande d'arrêter l'OS avant le poweroff
   (« Before powering off your Instance, shut down the OS [...] executing the
   halt command », `pages/instances/how-to/power-off-instance.mdx`). Un
   `poweroff` lancé dans l'invité met la VM en veille, état `stopped in place`
   (« will shut down your Instance and switch it to standby mode »,
   `use-standby-mode.mdx`). Une VM en veille reste facturée.
3. Preuve : l'état fournisseur `stopped in place` est observé après l'intention
   halt de cette vague, **et** `halted_argv` rend 0 (SSH opérateur muet, borné).
   Un SSH muet ne suffit jamais seul : tant que le fournisseur dit `running`,
   aucun poweroff n'est envoyé et le témoin n'est pas consulté.
   `archive intent` est alors persistée.
4. Au tick suivant, le poweroff fournisseur (archivage) part seulement si le
   GET du serveur liste `poweroff` dans `allowed_actions`. Ce champ est
   obligatoire dans l'objet `Server` (SDK `scaleway-sdk-python`,
   `instance/v1/types.py`). Sinon : `archive refused` et alerte, aucune action.
   La réponse POST ne vaut pas preuve : le GET doit ensuite rendre `stopped`
   avant l'acquittement.

`halt_deadline_s` (explicite, au plus 3600 s) borne les phases 2 et 3, compté
depuis l'intention. À l'échéance sans preuve : `halt deadline passed`, une
alerte qui nomme l'hôte et la génération, aucun poweroff, jamais `lost`. Une
preuve qui arrive après l'échéance reste valable. Un `stopped in place` sans
intention halt de la vague est une anomalie : il est alerté et jamais archivé
automatiquement. Un retour demandé pendant ces phases passe par la demande
`busy` : depuis la veille, `poweron` part si `allowed_actions` le permet
(sinon alerte `poweron_refused` après la borne de blocage), puis running,
relevé frais, santé et réconciliation, comme tout réveil. Une interruption se relit depuis le dernier marqueur, et le GET fournisseur
précède toute action.

Jamais `stop_in_place` par l'API, ni suppression, redimensionnement ou
création. Une fois `stopped`, le calcul cesse d'être facturé ; disques et IP
restent facturés. Sources opérateur, lues dans `scaleway/docs-content`
(`pages/instances/how-to/power-off-instance.mdx`, `use-standby-mode.mdx`,
`concepts.mdx`) et :
[états Scaleway](https://www.scaleway.com/en/docs/instances/reference-content/instance-states/),
[actions Instance](https://www.scaleway.com/en/developers/cli/instance/) et
[facturation](https://www.scaleway.com/en/docs/instances/reference-content/understanding-instance-pricing/).

Le type de volume change la durée de l'arrêt. AG3 (GP1-S, relevé GET du
bureau du 06/11/2026) a un volume unique `l_ssd` local de 150 Go. Le
poweroff le copie vers le stockage de volumes (« Powering off may take some
time, depending on the amount of data in the Instance's local volume »), et
le poweron le restaure : `stopping` et `starting` peuvent durer. Tant que le
fournisseur dit `stopping`, la demande est rafraîchie : un archivage qui
avance ne lève pas `host_release_adapter_stalled`. `locked` lève l'alerte en
nommant l'hôte, sans `lost`. Les durées réelles viennent du premier cycle
mesuré, jamais d'une estimation ; le délai de réveil mesuré inclut la
restauration.

Le drainage natif reste à sa durée canonique. L'hystérésis est distincte :
minimum allumé, repos prouvé au moins égal au maximum du drainage, du minimum
allumé et de deux délais de réveil ; minimum éteint avant rallumage. La première
observation et tout trou de suivi réinitialisent prudemment la preuve de durée.
Le délai initial est 900 secondes (étude #5900 §7, 05/11/2026), remplacé par
le délai jusqu'à la santé/réconciliation du premier vrai réveil observé.

Le worker anticipe le travail non exécutable ailleurs, les réveils de lots
et les créneaux de release inscrits pour cet hôte, dans cette fenêtre de délai.
Il pose seulement une intention de retour et une demande `busy`. Aucune prise
ne se rouvre avant une VM running, un relevé en base postérieur au retour
observé, la santé et la réconciliation. Le scanner natif impose encore son
relevé frais postérieur à l'acquittement busy avant réouverture. Une génération
périmée, un hold ou une panne laisse les claims fermés. Un hôte `lost` relève
de la récupération native, jamais d'un forçage par l'adaptateur.

## Garde d'entrée R1 — baux des runners

La séquence R1 (installation puis premier arrêt/réveil réel) ne part que sur
la version qui contient #7799 : sur un hôte `draining` prises fermées, un
runner dont l'agent est au repos (aucun tour en cours) cesse de renouveler et
rend son bail entre deux tours ; un runner dont un tour court le finit, puis
rend son bail. Aucun agent n'est déplacé et aucun runner n'est arrêté par
cette voie. Le bail rendu, l'agent est réclamé de nouveau à la réouverture des
prises (`returning`/`available`), par le même exécuteur ou un autre. Un
exécuteur médié lit le même prédicat par le contrat
(`host_lifecycle.claims_open`, contrat 1.13, hôte forcé, agent borné à
l'admission) : serveur d'abord, exécuteurs ensuite ; un ancien serveur ne sert
pas la lecture et l'exécuteur conserve alors son bail, journalisé une fois.

Garde : `host_release.release_blocker` refuse `idle-drained` tant qu'un agent
placé tient un bail, et son motif nomme les agents (`a lease is still held:
claude1, impl-11`). Le worker de l'adaptateur appelle la même garde au moment
de l'effet (`vm_adapter.stop_evidence`, avant `quiesce_argv` et `halt_argv`) :
il refuse le halt et journalise le motif nommé. Après la demande de drainage
et le délai de stabilisation, si des agents sont encore nommés, R1 est
**refusé** : ne pas halter, ne pas forcer la libération ; lire l'alerte
`host_release_overdue` (qui nomme les détenteurs) et le journal du worker,
puis traiter la cause — exécuteur resté sur une version antérieure à #7799,
tour long, ou panne de base. Un `idle-drained` non atteint n'autorise ni
arrêt ni archivage. La version **réellement lancée** sur chaque cible
(`ameesh version`, version des runners) se vérifie et se consigne avant le
premier geste, comme le mode.

Un refus de cette garde n'est pas un trou de suivi : la lecture fournisseur du
tick est rejouée dans sa propre transaction avant que l'échec ne sorte
(#8382), donc `on_since` et la continuité de 90 s ne repartent pas à zéro.
Seuls un fournisseur illisible ou un worker arrêté réinitialisent la preuve de
durée.

## Préparer l'installation (bureau, feu distinct)

1. Appliquer les migrations additives 0547 et 0552 (#6017) avant le nouveau code. Installer la
   version revue dans `/opt/ameesh-vm-adapter/venv`, dans un arbre que les agents
   ne peuvent modifier. Aucune installation n'est effectuée par ce lot.
2. Choisir **l'hôte du détenteur actuel de la clé**, hors des VM cibles.
   Relever son nom réel et le compte opérateur existant. Remplacer `User=`
   dans le service ; les exemples ne sont pas exécutables tels quels.
3. Inscrire les UUID réels, les zones et les paramètres dans
   `/etc/ameesh-vm-adapter/enrollment.json`, fichier privé du compte opérateur,
   sans lien symbolique. Laisser `enabled: false` jusqu'au feu d'activation.
   Vérifier que les seules VM inscrites sont les burst autorisées ; ne pas
   changer le décompte ou la charge d'une VM dans ce lot.
4. Garder `/etc/ameesh-vm-adapter/scaleway.json` sous le contrôle du détenteur
   existant (0600), contenu `{"secret_key":"…"}`. `LoadCredential` produit sur ramfs
   une copie privée appartenant au compte du service ; sur tmpfs (systemd 255),
   une copie root 0440 avec ACL de lecture limitée à ce compte, jamais aux runners. Pas de token en argument,
   dans les courriers, dans le dépôt ou dans les erreurs. Écrire
   `database.env` (0600) avec le chemin `AMEESH_CONFIG` de la connexion mesh
   de l'opérateur. Le DSN ne contient aucune clé cloud. Ce fichier de
   configuration doit déclarer `canons` : le chemin du clone synchronisé et sa
   référence, par exemple
   `"canons": [{"path": "/home/agent/canon", "ref": "origin/main"}]`. Sans
   cette clé, l'adaptateur refuse tout arrêt (`no canon configured`, distinct
   d'un canon configuré mais illisible) et la veille GitHub (#8059), qui lit
   la même configuration et donc les mêmes cartes, ne collecte rien. Vérifier
   les deux lectures sous le compte du service avec sa configuration
   (`AMEESH_CONFIG=… ameesh canon check`) avant le premier drainage ; le
   résultat doit nommer le ou les canons attendus, jamais `no canon
   configured`.
5. Avant d'inscrire **chaque cible burst**, préparer son journal systemd
   persistant : poser un drop-in `/etc/systemd/journald.conf.d/persistent.conf`
   repris du modèle `remote/journald.conf.example` (`Storage=persistent` et un
   `SystemMaxUse=128M` d'exemple), en dimensionnant les bornes pour le disque
   de la cible et le volume de ses journaux : conserver le boot précédent
   jusqu'au contrôle de retour, sans saturer le disque. Le bureau applique la
   configuration en redémarrant `systemd-journald`, puis demande le vidage
   vers le stockage persistant (`journalctl --flush`) avant le premier arrêt
   réel.
   Au premier arrêt/réveil autorisé, vérifier **sous le compte VM non root
   réellement utilisé par `previous_boot`** (le `user` de l'accès SSH de la
   `cible.json`, membre du groupe `systemd-journal`, sans root ni `sudo`) que
   `journalctl --list-boots` liste le boot précédent et que
   `journalctl -b -1 -n 1 -o cat --no-pager` rend sa dernière ligne. Une
   vérification faite sous root ou avec `sudo` ne prouve pas ce droit : le
   contrôle `previous_boot` tourne sous ce compte SSH.
   `Storage=volatile` ou un journal précédent absent ne satisfait pas ce
   préalable ; ne pas contourner le contrôle de santé pour inscrire la cible.
6. Fournir cinq commandes fixes par VM, détenues par l'opérateur et non
   modifiables par les agents. Elles n'héritent d'aucune variable de secret
   cloud ; argv absolu, pas de shell, sorties supprimées, échec = attente.
   `quiesce_argv` vérifie en lecture seule que l'OS peut s'arrêter proprement
   (aucun tour, aucune écriture en cours), sans toucher à une autre VM.
   `halt_argv` arrête l'OS de la seule VM cible par l'accès SSH opérateur
   (`poweroff` dans l'invité, qui la met en veille côté fournisseur), en au
   plus 10 s ; il ne bloque pas sur la déconnexion. `halted_argv` rend 0
   seulement si l'accès SSH opérateur de cette VM ne répond plus dans sa
   borne (témoin, jamais preuve seule). `health_argv` vérifie version/mode réellement actifs,
   runners vivants, doctor vert et courrier livré ; après un arrêt de
   l'adaptateur, il vérifie aussi que le journal du boot précédent
   (`journalctl -b -1`) se termine par un arrêt propre (#6074, lecture seule :
   aucune récupération de journal ni de système de fichiers). `reconcile_argv` termine
   et vérifie la réconciliation native des exécuteurs/identités, sans ouvrir
   les prises ni forcer un état lost. Utiliser l'accès SSH déjà autorisé,
   avec clé et known_hosts opérateur fixes, aucun login/secret d'agent.
   `probe_agent` nomme la seule persona de l'hôte que `health_argv` fait
   tourner (#6017) : au réveil, l'adaptateur passe l'hôte en `returning`
   prises fermées et n'admet qu'elle ; un échec de santé le remet en
   `draining`, sans lost. Un tour de sonde dure plus qu'un appel borné
   (jusqu'à 300 s pour le courrier) : `health_argv` rend `75`
   (`EX_TEMPFAIL`) tant que le reçu strict de ce tour, lié à la génération,
   à l'époque et à `probe_agent`, n'est pas complet (#6073). L'hôte reste
   alors `returning`, même sonde, sans refus, jusqu'à `health_deadline_s`
   (explicite, au plus 3600 s, compté depuis l'admission de la sonde) ;
   au-delà, retour en `draining` comme un échec. Le wrapper opérateur lance
   la sonde **une seule fois**, en asynchrone, et ne rend jamais `0` sur une
   livraison seule ni sur une preuve ancienne ; tout autre code est un échec.
   La source revue de ces cinq commandes est `commandes/vm-commande.py`
   (#6252, section suivante) : installer ce fichier au SHA fixé, et une
   `cible.json` privée par VM dans `/etc/ameesh-vm-adapter/cibles/`.
7. Faire relire les mappings, les cinq commandes, les durées
   (`halt_deadline_s`, `health_deadline_s`), les créneaux,
   les permissions des fichiers et l'unité avec `systemd-analyze verify`.
   Tester le cycle cloud simulé avant tout feu réel (séquence halt → preuve
   → poweroff). Faire valider séparément le premier arrêt/réveil par le
   bureau, avec ses temps mesurés : halt jusqu'à `stopped in place`,
   archivage jusqu'à `stopped`, restauration jusqu'à la santé.
8. Au feu d'installation seulement : rendre l'inscription active, poser les
   unités, recharger systemd et activer le timer sur ce seul hôte. Déclarer
   le hook sans secret dans la configuration des seuls hôtes autorisés,
   dans l'ordre de la section « Déclarer le hook d'une cible » (#7656).
   Aucune commande de cette étape n'a été exécutée par l'auteur du lot.

### Veille GitHub au repos, à côté du worker (#8059)

BG reste allumé au repos (décision owner #230087) et porte déjà ce worker :
il porte aussi la veille GitHub déterministe, sans agent ni bail
(`deploy/systemd/ameesh-github-watch.{service,timer}`), **unité système**
installée comme le worker : `User=` = compte opérateur existant, même
`EnvironmentFile=/etc/ameesh-vm-adapter/database.env` (connexion mesh de
l'opérateur, `AMEESH_CONFIG`), même venv `/opt/ameesh-vm-adapter/venv`. Le
collecteur s'efface tant que le rôle élu de la veille a un bail vivant.
Préalables sur BG : `github.host` = `bureau-goal` (et, si le canon renomme le
rôle, `github.watch_role`) dans la configuration lue par `database.env`, cartes
`github_watch` synchronisées, et un jeton GitHub en lecture seule.

Jeton : `GH_TOKEN` (ou `GITHUB_TOKEN`) dans
`/etc/ameesh-github-watch/notify.env`, root:root 0600. systemd lit ce fichier
avant de passer au compte du service : le compte opérateur ne peut pas le lire
sur disque ; il n'est ni dans l'unité, ni dans la configuration, ni dans un
argument. `notify` lit `GH_TOKEN`/`GITHUB_TOKEN` et ne lit pas
`$CREDENTIALS_DIRECTORY` : le fichier d'environnement est donc retenu plutôt
que `LoadCredential`. La pose du jeton est une décision de l'owner ; un agent
ne le copie jamais. Le compte doit écrire les curseurs de veille : la
connexion limitée `bureau_goal` ne le peut pas.

Installation, sur BG, en root, comme le worker (aucune commande de cette
étape n'a été exécutée par l'auteur du lot) :

1. `install -m 0644 deploy/systemd/ameesh-github-watch.service
   deploy/systemd/ameesh-github-watch.timer /etc/systemd/system/`, puis
   remplacer `User=` par le compte opérateur existant — le même que
   `ameesh-vm-adapter.service`, jamais un compte d'agent.
2. Poser le jeton : `install -d -m 0700 /etc/ameesh-github-watch`, puis
   `install -m 0600 -o root -g root <fichier 0600 du jeton>
   /etc/ameesh-github-watch/notify.env` (contenu `GH_TOKEN=…`).
3. `systemctl daemon-reload`, puis
   `systemctl enable --now ameesh-github-watch.timer`.
4. Vérifier : `systemctl status ameesh-github-watch.timer`,
   `systemctl start ameesh-github-watch.service`, `journalctl -u
   ameesh-github-watch -n 20 --no-pager`. Sans jeton, le journal porte
   « no token or gh » et le curseur ne bouge pas.
5. Retour arrière : `systemctl disable --now ameesh-github-watch.timer`,
   retirer les deux unités de `/etc/systemd/system/`, `systemctl
   daemon-reload`. Le curseur en base reste ; le courrier déjà déposé n'est
   pas retiré.

Procédure et témoin détaillés : `docs/EXPLOITATION.md`, « Veille GitHub au
repos, sur BG ».

## Commandes par VM (#6252)

`commandes/vm-commande.py <verbe> <cible.json>` porte les cinq commandes ;
l'inscription les appelle par le python du venv opérateur, en `-I`
(`enrollment.example.json`). Bibliothèque standard, plus `ameesh` du même
venv pour lire la base native en lecture seule. Il n'écrit rien sur la
sortie ; ses enfants reçoivent seulement `PATH` et `LANG`.

La `cible.json` de chaque VM (modèle `commandes/cible.example.json`) est
privée au compte opérateur (0600, sans lien). Elle fixe :
- l'hôte ameesh et `probe_agent`, identiques à l'inscription ;
- l'accès SSH : exécutable, destination, compte, port, clé (0600) et
  `known_hosts` (non inscriptible par d'autres) ;
- `connect_timeout_s` et `budget_s` (au plus 9,5 s, sous la borne de 10 s du
  worker) ;
- `ameesh_config`, la configuration de lecture de la base, sans clé cloud,
  privée elle aussi ;
- `state_dir`, le `StateDirectory` de l'unité (0700) ;
- `previous_boot_end` et les argv distants, chacun d'exécutable absolu.

Chaque appel prend d'abord le verrou opérateur de sa VM (`flock` non
bloquant sur `lock.<hôte>.json` dans `state_dir`). Deux invocations
concurrentes du worker ne décident donc jamais ensemble : la santé tourne
avant le verrou du worker. Une VM déjà tenue rend 75 pour `health` et 1
pour les autres commandes, sans aucun effet.

SSH tourne en `-F none` et `BatchMode`, avec `StrictHostKeyChecking=yes`,
`IdentitiesOnly`, sans agent ni transfert. Chaque argv distant est cité
(`shlex.join`). La clé à commande forcée et le compte VM non root sont obligatoires
(voir « Commandes natives dans la VM »).

- `quiesce` : contrôle fermé en `draining`, aucun `turn_resources` en cours
  sur l'hôte, puis chaque argv distant `quiesce` à 0.
- `halt` : mêmes gardes. Le marqueur `halt.<hôte>.json` est écrit d'abord,
  puis l'argv distant `halt` part (par exemple
  `systemctl poweroff --no-block`). Une déconnexion (255) ou la borne
  atteinte sont attendues ; le code ne prouve rien.
- `halted` : rend 0 seulement si l'argv distant `alive` ne répond pas dans sa
  borne (délai, connexion perdue). Un refus d'authentification ou de clé
  d'hôte n'est pas un silence ; `ssh` absent ne l'est pas non plus.
- `health` : lit le contrôle natif (`returning`, prises fermées,
  `return_probe` = `probe_agent`, génération, époque) et l'admission de la
  sonde (`probe admitted`). La première fois pour cette admission, il écrit
  `probe.<hôte>.json` avec un nonce neuf, **avant** le lancement. Il appelle
  ensuite `probe_start` une seule fois, sans attendre la fin, et rend 75.
  Ensuite, `probe_receipt` doit rendre en dernière ligne le reçu JSON de **ce**
  nonce. Ce reçu porte `host`, `probe_agent`, `generation`, `release_epoch`,
  `nonce`, `verdict` (`ok`, `ko` ou autre) et les booléens `delivered`,
  `acknowledged` et `turn_ended`. Un reçu absent, en cours, incomplet
  (livraison seule) ou d'un autre lancement rend 75 ; `ko` rend 1. Seul un reçu
  complet `ok` mène aux argv distants `health` (version/mode, runners, doctor,
  courrier), tous à 0. Après un `halt`, la dernière ligne de `previous_boot`
  (`journalctl -b -1`) doit correspondre à `previous_boot_end`. Une
  réadmission (après un refus) relance une seule sonde, avec un nouveau nonce.
  Base ou transport indisponible (borne atteinte, `ssh` non lancé, 255 de
  `ssh` lui-même) : 75, borné par `health_deadline_s`, y compris pendant les
  contrôles de santé et la lecture du journal précédent. Seule une commande
  distante qui répond non zéro est un échec (1).
  #8072 : `health` écrit une seule ligne bornée sur sa sortie, que le worker
  journalise après validation (`health host=… nonce=<8 hex> stage=…`, suivie
  de `(exit N)`). Elle nomme l'étape atteinte (`control`, `admission`,
  `launched`, `receipt`, `checks`, `previous-boot`, `done`), la réponse de
  `probe_receipt` (`answer` : `ok`, `transport`, `failed`), son `verdict` et
  ses trois booléens (`true`, `false`, `absent` pour un reçu étranger), puis
  `checks` et le rang (`step`) du premier argv `health` qui n'a pas rendu 0.
  Elle ne contient que des mots fixes : ni octet distant, ni chemin, ni nonce
  complet, ni secret. Une sortie qui ne correspond pas exactement est écartée
  sans être lue (« no valid line »). Les fichiers
  `/var/lib/ameesh-vm-remote/<nonce>.json` ne portent que le lancement : le
  reçu est calculé à chaque appel, il ne s'y lit pas.
- `reconcile` : contrôle fermé en `returning` sur `probe_agent`, puis chaque
  argv distant `reconcile` à 0 ; n'ouvre jamais les prises et ne force jamais
  `lost`.

Les cinq commandes ne tournent que dans le worker, qui les appelle aux étapes
prévues. Seule `halt` agit sur la VM. Les valeurs de `previous_boot_end` et la
fin réelle du journal d'un arrêt propre se vérifient au premier cycle mesuré
par le bureau.

## Commandes natives dans la VM (#6276)

`remote/vm-remote.py` fournit les aides natives `probe-start`, `probe-receipt`,
`reconcile`, `quiesce`, `runners` et `mail`. Aucun fichier exécutable nommé
`probe-start` n'est nécessaire : le dispatch de la commande forcée reconnaît
ces chemins exacts, analyse les argv avec `shlex.split` et appelle le code
Python correspondant. Il n'évalue jamais la chaîne SSH et ne lance aucun
shell. L'interpréteur est le Python isolé (`-I`) du venv root existant.

`probe-start` vérifie l'hôte, la persona, le contrôle `returning` fermé,
la génération, l'époque et l'admission `probe admitted`, sous la garde native.
Il dépose une demande native idempotente à cette seule persona. Le runner
existant prend le tour en asynchrone : aucun second runner ni harnais n'est
créé par SSH, aucun utilisateur agent n'est créé. Un verrou local non bloquant
sérialise les demandes ; le nonce et l'identifiant du message sont durables.
Une reprise après dépôt et avant écriture locale retrouve le même message et
son horodatage original, sans second tour.

`probe-receipt` exige la remise effective du message, son lien natif
`meta.consigne.turn`, une ressource du tour sur le bon hôte et la bonne
persona, puis l'accusé exact `VM-PROBE-ACK <nonce>` de cette persona envoyé
entre remise et fin du tour. La demande indique la commande d'accusé à
exécuter : `agent-mail send <probe_agent> --ack "VM-PROBE-ACK <nonce>"`.
L'accusé est adressé à la persona elle-même, dont l'inscription sur cet hôte
est vérifiée avant le dépôt ; `--ack` ne réveille aucun nouveau tour.
L'identité d'émission `vm-adapter` ne reçoit aucun courrier et ne nécessite
aucune inscription comme destinataire. `done`, `ended_at`, l'état final `idle` sans erreur et les trois
booléens sont nécessaires à `ok`. Livraison seule, nonce/époque/génération
étrangers ou réadmission plus récente ne donnent jamais `ok`. Un tour fini
en échec donne `ko`. Les reçus ne contiennent ni corps de courrier, ni DSN,
ni identifiant de session.

`reconcile` laisse le runner effectuer ses traitements natifs de démarrage
et périodiques (`report_boot_orphans`, `settle_dead_turns`,
`reconcile_turn_cgroups`). Il ne rend 0 qu'après une présence du runner sur
ce retour, fraîche selon `fresh_seconds`, sans ressource `running` ou
`orphan`, sans identité d'origine déplacée/fencée ni handoff actif.
Sinon il rend 75, jusqu’à `reconcile_deadline_s` (entier explicite de 1 à
3600 secondes, depuis `probe admitted` de cette génération/époque) ; à
l’échéance il refuse (1), sans effet. Il n'appelle pas `ameesh host reconcile` : cette
commande est destinée à `lost` et peut rouvrir les prises. Aucune ressource
n'est supprimée ni soldée par l'aide, aucun bail révoqué de force. Le worker
revérifie génération et relevé sous son verrou avant d'acquitter le retour.
`quiesce` reste une lecture du contrôle `draining` et des ressources ; il
ne reprend pas le verrou que le worker tient déjà pendant son appel SSH.

**Préalable sur chaque cible** (#6270, note pilote du 08/11 04:18) :
activer le journal persistant avant le premier arrêt. Le modèle
`remote/journald.conf.example` fixe `Storage=persistent` et un
`SystemMaxUse=128M` d'exemple, à dimensionner par le bureau pour la cible.
Le bureau pose ce drop-in root non inscriptible par les agents, valide la
configuration et vérifie après redémarrage, **sous le compte VM non root
réellement utilisé par `previous_boot`** (le `user` SSH de la `cible.json`,
membre du groupe `systemd-journal`, sans root ni `sudo`), que
`journalctl --list-boots` expose le boot précédent et que sa dernière ligne
est lisible. Une lecture root ou `sudo` ne prouve pas ce droit. Sans preuve
du boot précédent, la santé refuse : un journal volatile ou un simple état
SSH silencieux ne remplace pas cette preuve.
Aucun redémarrage de journald ni changement d'une VM n'a lieu dans ce lot.

Préparation par le **bureau**, hors de ce lot et sur feu distinct du pilote :

1. Choisir un compte VM dédié non root (exemple : `vm-operator`), sans mot de
   passe utilisable et sans groupe d'agent ni groupe sudo général. Son shell
   doit permettre à sshd de lancer la commande forcée. Ne pas reprendre le
   compte des runners. Ne pas copier de clé privée ni de secret cloud dans
   cette VM. Le compte opérateur du contrôleur et le compte opérateur VM
   sont deux inscriptions différentes.
2. Installer code et venv sous `/opt/ameesh-vm-adapter`, root:root, non
   inscriptibles par le compte VM, les agents, groupe ou autres. Installer
   `remote/enrollment.example.json` rendu en
   `/etc/ameesh-vm-adapter/remote.json`, root:vm-operator 0640, et le fichier
   de connexion JSON désigné, root:vm-operator 0640 ; aucun lien, parents
   root non inscriptibles. Ce rôle DB doit lire les faits natifs concernés
   et envoyer le seul courrier de sonde ; le choix et la réduction du rôle
   effectif sont revus avec le bureau avant activation (#6002).
   Pour #6298 : JSON de connexion root:vm-operator 0640, passfile libpq
   distinct 0600 appartenant à vm-operator (libpq ignore un passfile 0640),
   TCP `10.77.0.1:5432`, `sslmode=require`, schéma réel du mesh. Utiliser le
   rôle réduit `vm_probe`, jamais un DSN d'agent ; sa pose reste distincte.
   Installer `remote/checks/health.py` dans `checks/health.py`, ainsi que
   `deploy/mise-a-jour/venv-processus.py` dans `checks/venv-processus.py`,
   root:root 0644 sous les parents root non inscriptibles de l'étape 2.
   Rendre `checks/health-producer.example.json` dans
   `/etc/ameesh-vm-adapter/health-producer.json`, root:root 0644 (aucun secret) :
   venv stable du compte ameesh, chemin absolu de son EnvironmentFile et du
   reçu partagé. Ne pas copier cet EnvironmentFile : le producteur le lit
   sous ameesh. Les unités runners ne sont plus dans la carte (#8069) : le
   producteur lit celles que le gestionnaire utilisateur active ou fait
   tourner (`checks/diagnostics.md`) ; la clé `units` d'une ancienne carte
   est ignorée.
3. Créer uniquement `/var/lib/ameesh-vm-remote` sous le compte VM, 0700.
   Le compte y écrit ses reçus et son verrou, jamais dans le dépôt ni dans
   les répertoires des agents. Archiver les anciens reçus hors activation
   lorsque leur vague est révolue ; ne pas enlever celui du retour courant.
   Créer aussi `/var/lib/ameesh-vm-health`, ameesh:vm-operator **2750**, par
   root à la pose. Le bit setgid donne aux nouveaux reçus le groupe lecteur,
   sans ajouter ameesh à ce groupe ; vm-operator n'a aucun droit d'écriture.
   Installer les modèles `checks/ameesh-vm-health.{service,timer}` comme
   unités **utilisateur d'ameesh** root non inscriptibles par vm-operator,
   activer le minuteur sous ce compte et son gestionnaire utilisateur
   persistant. Aucune unité système root ni règle sudo supplémentaire.
4. Rendre les modèles `remote/authorized_keys.example` et
   `remote/sshd-match.example` avec la **clé publique** du détenteur opérateur
   existant et le login retenu. Le fichier de clés et ses parents sont root
   non inscriptibles par ce login ; sshd refuse mot de passe, terminal,
   environnement utilisateur et transferts. Les deux commandes forcées
   sont identiques. Valider avec `sshd -t` avant toute pose. Le parseur
   refuse toute commande, option, argument ou chemin hors liste.
   `PermitUserEnvironment no` doit rester dans la configuration **globale**
   de sshd : cette directive est interdite dans un bloc `Match`.
   Vérifier la configuration effective avec
   `sshd -T -C user=vm-operator,host=localhost,addr=127.0.0.1` (adapter le
   login) : `permituserenvironment no`, `allowagentforwarding no`,
   `allowstreamlocalforwarding no`, `allowtcpforwarding no`, `permittunnel no`
   et `permitlisten none`. Un écart refuse l'activation.
5. Rendre `remote/sudoers.example` avec ce seul login et valider
   `visudo -cf <fichier rendu>`. La seule ligne autorisée est
   `/usr/bin/systemctl poweroff --no-block`, sans joker ni variante.
   Ajouter le compte au groupe `systemd-journal` pour lire le boot précédent :
   aucune règle sudo pour journalctl, Python, shell ou runuser.
6. Conserver les argv fixes `checks.version-mode` et `checks.doctor` du
   modèle : `/usr/bin/python3 -I /opt/ameesh-vm-adapter/checks/health.py`
   suivi du seul nom de contrôle. Rendre `checks/health-reader.example.json`
   dans `/etc/ameesh-vm-adapter/health-reader.json`, root:vm-operator 0640 :
   UID numérique réel d'ameesh, reçu partagé, période 60 secondes et chemin
   stable du venv. Aucun bloc supplémentaire dans `remote.json`, aucune
   version ni aucun SHA figés dans une carte root. Le lecteur résout
   `venv-courant` (ou le venv stable avant adoption) et exige la même cible
   que celle prouvée pour les runners par le producteur. Un changement de
   cible invalide donc immédiatement le reçu précédent. Les métadonnées
   version/SHA sont celles de ce venv prouvé, jamais d'un checkout local.
   La cible du lien et ses parents doivent être observables par vm-operator ;
   si cet accès est refusé, le contrôle refuse et la pose reste à résoudre
   avec le bureau, sans ajout automatique de droits ni de sudo.
   Ce contrôle vérifie la cohérence installée, pas une cible de vague.

   Le producteur énumère les unités runners via `systemctl --user` : celles
   que `default.target` veut et celles chargées, activées ou non inactives
   (#8069, aucune liste de carte). Pour chaque MainPID actif il appelle
   le même `venv-processus.py` que staging (dont l'attestation privée 0600
   pour les processus non dumpables), puis lit sa cmdline, jamais son
   environnement. Le mode `execute` ou `dry-run` vient du `run` vivant et de
   son option `--dry-run`, pas d'une étiquette déclarative staging/prod.
   Seul le mode observé `execute` peut publier un verdict vert.
   Les métadonnées proviennent du venv ainsi prouvé. Le doctor **complet**
   tourne via `systemd-run --user` avec l'EnvironmentFile d'ameesh, sans
   exposer son contenu. Les MainPID et modes sont revérifiés ensuite ;
   panne, accès refusé ou dépassement de délai ne publient aucun vert.

   Le reçu atomique 0640 contient uniquement schéma, boot_id, horodatage,
   version, SHA, mode, venv résolu, PIDs et verdicts. Les lecteurs refusent un reçu absent,
   illisible, mal formé, d'un autre boot, daté du futur, vieux de plus de
   trois périodes, ou dont UID, permissions, mode ou venv divergent.
   Version et SHA doivent être présents et valides.
   Doctor exige en plus son verdict vert. Le dispatch borne les lecteurs
   à **5 s**, environnement PATH/LANG seul, sorties supprimées. Un producteur
   tué laisse au plus le reçu précédent jusqu'à son expiration ; une sonde
   échouée le remplace par un reçu négatif. Les lecteurs ne consultent ni
   base, ni bus utilisateur, ni fichiers privés des runners.

   Limite acceptée (#200433, mesh-design, 08/11/2026) : ameesh peut forger
   son propre reçu. Cette sonde vérifie la santé de l'hôte ; elle ne protège
   pas contre le compte ameesh compromis. Aucune lecture privilégiée ne
   remplace une preuve absente. Ne jamais remplacer un contrôle par `true`.
   Le bureau vérifie les deux codes de sortie sous vm-operator et un reçu
   frais après activation du minuteur ; cette preuve reste exigée par #6002.
7. Reporter le login dans `commandes/cible.json` et conserver les argv du
   modèle. Faire relire permissions, argv et preuves de santé avant le feu
   d'activation. Aucune création de compte, pose, commande sudo ou connexion
   VM n'est exécutée par #6276.

Justification de chaque commande admise par la clé forcée :

| Commande | Effet et privilège |
| --- | --- |
| `remote/probe-start <host> <probe> <gen> <epoch> <nonce>` | Dépôt natif idempotent de la sonde admise, non root. |
| `remote/probe-receipt <nonce>` | Lecture du reçu strict de cette demande, non root. |
| `remote/quiesce` | Contrôle fermé et absence de ressources en cours, lecture seule. |
| `remote/reconcile` | Vérification de la fin de la réconciliation native, lecture seule. |
| `remote/runners` | Même contrôle de présence fraîche du runner et de repos sur ce retour. |
| `remote/mail` | Même reçu strict que la santé ; un courrier quelconque ne suffit pas. |
| `remote/version-mode` | Contrôle local version/mode inscrit, non root, borné. |
| `remote/doctor` | Contrôle doctor inscrit, non root, borné. |
| `/usr/bin/true` | Disponibilité SSH seulement, aucune preuve de santé. |
| `/usr/bin/journalctl -b -1 -n 1 -o cat --no-pager` | Dernière ligne du boot précédent, groupe systemd-journal, sans sudo. |
| `/usr/bin/sudo -n /usr/bin/systemctl poweroff --no-block` | Seul effet privilégié : arrêt de cette VM, après contrôle draining/repos ; le worker détient la garde de vague. |

`tests/test_6276_vm_remote.py` étend le cycle simulé de #6252 avec les
véritables aides et la base native du banc ; aucun SSH, sudo, cloud ni VM
réelle. Les cas couvrent le reçu incomplet/étranger, la réadmission,
l'idempotence, les commandes interdites, l'échec de santé et la réconciliation
qui garde les prises fermées. Le boot précédent reste à qualifier au premier
cycle mesuré par le bureau.

## Déclarer le hook d'une cible (#7656)

Sans `lifecycle_hook`, `host_release` garde le chemin manuel L232e : un
drainage, même posé par le pilote, n'envoie aucune demande à l'adaptateur.
La fenêtre R1 du 17/11 a été perdue pour cette raison. Le hook se déclare
sur la carte Host (`policy.lifecycle_hook`), sans secret :

    @ameesh-python -m ameesh.vm_adapter request

Un hôte contrôlé (`draining`, `returning`) avance depuis le scanner de
**n'importe quel** runner, pas seulement celui de la cible. Le jeton
`@ameesh-python` est remplacé par le Python du processus scanner,
sans recherche dans `PATH`. La version incluant #8068 doit être déployée sur
chaque runner et sur le serveur médié avant de déclarer ce hook ; chaque
runtime doit contenir le module. Une absence donne `host_release_failed`
à la tentative de cet hôte, et les prises restent fermées.

Il faut aussi que les runs de la cible soient connus. Sans cela, ni le
drainage automatique, ni `idle-drained`, ni la preuve d'arrêt du worker
(`stop_evidence`) ne passent. `policy.runs_interface: none` ne vaut que
si rien ne tourne hors tours et baux. **Un hôte prêteur (`policy.lend`) ne
peut pas le déclarer** : le canon le refuse (#5905), et `lending` l'emporte
sur `runs_interfaces` en configuration, sauf l'interface comptante `guest`
(#7687) : `none` ne peut pas transformer une prêteuse en zéro prouvé. AG3
déclare `policy.runs_interface: guest` : le comptage des jobs d'offload vivants
(shell guest et slices `ameesh-job-*`). Un comptage absent, périmé ou
illisible reste **inconnu**, jamais zéro : ni drainage automatique, ni
`idle-drained` sur ce doute. AG3, prêteur qui porte le guest d'offload de la
prévalidation au gel, est dans ce cas depuis #8418.

Déclarer le hook arme deux effets à toute heure :
- le drainage automatique (`_auto_drain`, burst inactif depuis
  `idle_after_s`) sur un hôte aux runs connus ;
- la demande à l'adaptateur pour tout drainage, manuel compris.
  `request` n'inspecte pas l'inscription : il met la demande en file et rend
  75. Si le worker est inactif (timer arrêté, `enabled: false`), l'hôte reste
  `draining`, prises fermées, en `hook_wait`. Seule l'alerte
  `host_release_adapter_stalled` le signale, au bout de
  `AMEESH_HOST_RELEASE_ADAPTER_STALL`.

Le drainage automatique ne part que sur un hôte `burst` dont le relevé est
frais et dont la fenêtre de repos est prouvée sans trou depuis
`idle_after_s` (1800 s par défaut) : `guest_jobs = 0`, aucun tour, aucun bail
de lot, aucune retenue, et l'hôte n'est ni détenteur ni réserve d'un rôle élu
(`baseline_guard`). L'hystérésis d'allumage (`min_on_s`) et la durée de
drainage native restent celles de l'adaptateur. Un job guest vivant, un
relevé illisible ou un run non déclaré bloquent le drainage **et** la preuve
`idle-drained` ; le doute ne vaut jamais zéro.

Ce qui ne peut pas servir de garde-fou permanent :
- une retenue (`ameesh hosts HOST --hold`) : elle ferme les baux de tous
  les agents de l'hôte (`claimable`) et expire au bout de 24 h ;
- `capacity: baseline` : `stop_evidence` exige un hôte burst, ce qui
  bloquerait aussi la fenêtre.

Jusqu'au R1 v3 (#6002), l'ordre retenu était de **ne rien déclarer hors
fenêtre** : hors fenêtre, le hook était absent et `lend` présent ; la
déclaration passait par une PR canon « fenêtre » (manaty/home#297), fusionnée
au feu par le pilote, et retirée par une PR inverse (manaty/home#298). Le
drainage automatique était refusé deux fois (`no lifecycle hook`, `declared
runs unknown: lending`), et un drainage manuel se rouvrait au retour par
L232e (tests `test_4148_host_release`, `test_5905_runs_interface`).

Depuis #7687 (comptage des jobs guest) et #8418, le mode permanent remplace
cette fenêtre pour AG3 : la carte garde `lend` et déclare `runs_interface:
guest` et le hook. C'est la carte qui est l'activation durable ; sa PR
inverse est le retour arrière, sans fenêtre à refermer.

### AG3 en permanent (#8418, tranche 1 de #8029)

La carte `hotes/ameesh-agents-3.md` porte, dans `policy:` :

    lend: ...                       # conservé
    runs_interface: guest           # compte les jobs d'offload vivants
    lifecycle_hook: "@ameesh-python -m ameesh.vm_adapter request"

`@ameesh-python` est remplacé par le Python du scanner (#8068) ; le runtime
doit contenir `ameesh.vm_adapter` sur chaque hôte au runner vivant.

Ordre d'activation (aucun geste cloud) :

1. **Bureau, avant la fusion de la carte** : copier
   `/etc/ameesh-vm-adapter/enrollment.json` (0600), passer `"enabled": true`,
   puis `systemctl enable --now ameesh-vm-adapter.timer` sur bureau-goal.
   Vérifier une passe worker récente dans le journal et une ligne
   `vm_adapter_observations` pour AG3 (sans elle, un hook resterait fermé en
   `hook_wait`). Hors coupure (10:00Z, 22:00Z) et hors CI de paquet.
2. **Pilote** : fusionner la PR canon sur sa tête gelée
   (`gh pr merge --match-head-commit`), puis `ameesh canon sync --fetch
   --json` sur chaque hôte au runner vivant. `ameesh hosts ameesh-agents-3`
   doit rendre `runs_interface: guest (Host card)` et le hook.
3. **Observation** : quand AG3 reste sans tour, sans bail de lot et sans job
   guest pendant `idle_after_s`, le scanner démarre un drainage `automatic`.
   Le worker acquitte `draining`, puis `idle-drained`, arrête l'OS, prouve
   l'arrêt et archive ; une cause de réveil (courrier d'un agent placé, lot
   assigné, job CI de ses runners, `return_ts`) redemande `busy`, rallume,
   admet la sonde, vérifie santé et réconciliation, puis rouvre les prises.
   Le lot #8418 exige cette observation une fois en staging.

Retour arrière : si AG3 est arrêté ou en retour, d'abord le ramener et
attendre `available` **en gardant le hook** ; puis fusionner la PR inverse
(hook et `runs_interface` retirés, `lend` conservé), faire la synchronisation
prouvée, et seulement ensuite, si le worker n'est plus nécessaire, arrêter
le timer et remettre `enabled: false`. Ne jamais retirer le hook d'une VM
encore arrêtée (« Contrôle et retour arrière »).

### Séquence R1 (plan de #6002, remplace l'étape « drain » de v3.4.1)

> R1 est clos (28/11 02:31Z, #6002). Cette séquence est conservée comme
> historique de la fenêtre ; le mode permanent d'AG3 est décrit ci-dessus.

Deux PR canon home sont préparées, relues par une autre famille que Claude
et gelées avant le feu : « fenêtre » (manaty/home#297) et « R2 »
(manaty/home#298), qui en est l'inverse exact (son revert, sans écart avec
`main`). Le pilote fusionne #297 au feu et #298 à R2.

**Hôtes au runner vivant** : AG1, AG2, AG3 et le serveur médié.
manaty-mesh-1 ne porte actuellement que le collecteur hosts, sans runner.
pc-smichea (lost, éteint) est hors champ : il est consigné comme tel,
sans vérification. S'il revient pendant la fenêtre, le feu s'arrête (sortie,
étape 7).

**Synchronisation prouvée** : après chaque fusion, lancer `ameesh canon
sync --fetch --json` sur chaque hôte au runner vivant, puis consigner le
champ `source` (`home @ <commit>`) qui nomme le commit fusionné. Un runner
resté sur l'ancien canon garde `lend` après la fenêtre, ou le hook après R2.

1. Gardes d'entrée, vérifiées et consignées au feu, hors coupure (10:00Z,
   22:00Z) :
   - composeur suspendu, aucune CI de paquet ;
   - aucun job guest d'offload vivant sur AG3 (compte `ameesh-guest` : aucun
     processus) ;
   - aucun contrôle `draining`/`returning` sur AG3 (`ameesh host status
     ameesh-agents-3`), aucune retenue ;
   - sur chaque hôte au runner vivant et sur le serveur médié, relever le
     Python du processus scanner et vérifier qu’il est exécutable, contient
     #8068 et importe `ameesh.vm_adapter` avec cet interpréteur. Selon la
     disposition, le runtime est celui de la VM sous
     `~/.local/share/ameesh/venv` ou du serveur sous `/opt/ameesh/venv` ;
     vérifier le processus réellement lancé, pas seulement le chemin prévu ;
   - #297 et #298 gelées et relues ;
   - aucun lot ouvert assigné à un agent placé sur AG3 (#7874) :
     `ameesh host release-check ameesh-agents-3` rend 0. Sinon, chaque lot
     nommé est fini ou déplacé (réassigné, ou son agent déplacé) avant
     l'étape 4, puis la commande est relancée et sa sortie consignée.
     Sans cette garde, la première passe du worker demande le retour
     (`wake_due`, branche `return_targets` : du travail ouvert qui ne peut
     tourner que sur AG3), comme au R1 du 19/11. Ce comportement de
     l'adaptateur est voulu et reste inchangé, à une exception près (#8129) :
     pendant un drainage manuel à retour daté (`--for`), un courrier non lu
     n'appelle le retour que s'il est `--urgent` ; le courrier ordinaire
     attend l'heure annoncée (lot actionnable, invite en attente et
     redémarrage demandé rappellent toujours l'hôte ; un drainage
     automatique se réveille sur tout courrier). La ligne « réveil immédiat »
     de la commande nomme les agents qui déclencheraient ce retour ;
     pour ce même drainage manuel daté, le courrier ordinaire, passif compris, ne bloque
     pas non plus le halt ; le courrier urgent, une demande de redémarrage et
     les autres preuves d'activité restent bloquants. Si la preuve du courrier
     urgent manque, l'arrêt reste non concluant (#8145) ;
   - la version lancée sur AG3 contient #7874 : la sonde du retour
     (`probe_agent`) figure parmi les agents réclamables d'un retour
     `returning` prises fermées. Avant #7874, elle n'était jamais réclamée,
     et le retour bouclait sur `health deadline passed`.
   - la sonde (`probe_agent` de la cible) n'est pas en pause :
     `ameesh show <probe_agent>` n'a pas de ligne `paused`.
     Un agent en pause ne prend aucun tour (L87b), donc pas celui de santé.
     Depuis #8072, le découpeur de file n'endort plus la sonde d'une fenêtre
     ouverte (retour `returning` prises fermées, verrou de release) et lève
     son sommeil automatique. Avant l'admission, il ne la connaît pas : une
     pause posée avant l'étape 4 se lève ici (`ameesh resume <probe_agent>
     --operator --reason …`), et se consigne.
     Une pause humaine n'est jamais levée par le découpeur ;
   - reçu de santé d'AG3 vert (#8069), relevé avant l'étape 4 : sur AG3,
     `/var/lib/ameesh-vm-health/health.json` porte `version_mode` et
     `doctor` à `true`, sans `reason`, daté de moins de trois périodes du
     minuteur (`period_s` de `health-reader.json`) ; les deux contrôles
     fixes de l'inscription (`checks.version-mode`, `checks.doctor`), lancés
     sous le compte VM, rendent 0. Un reçu rouge arrête R1 avant tout
     drainage : sa raison (`étape:Classe`, `checks/diagnostics.md`) se
     traite d'abord. #8069 doit être en place, par deux voies distinctes :
     (1) AG3 tourne un venv ameesh qui contient #8069 (`ameesh version`),
     pour que `ameesh host status` porte `return_probe` ; (2) le bureau a
     reposé `checks/health.py` dans `/opt/ameesh-vm-adapter/checks`,
     root:root 0644, depuis le commit fusionné, au feu pilote, puis relevé
     un reçu vert. Le déploiement staging du venv ne met pas à jour ce
     fichier, posé par root à l'étape 2. Sans (2), la carte `units` posée à
     l'installation, périmée dès qu'un agent change, rend le reçu rouge
     (`inventory:ValueError`, R1 du 22/11), et un retour prises fermées le
     laisserait rouge faute de bail des autres runners. Retirer `units` de
     la carte reste facultatif ; ce geste est au bureau.
Avant le feu R1, sur l'hôte du contrôleur, lancer `_canon_read` avec le
venv et la configuration réellement utilisés par le worker ; consigner la
sortie `1 canon, unknown False` ainsi que le SHA du canon local. La lecture
par un autre venv ou un autre hôte ne prouve pas celle du worker.
Avec le compte et l'environnement du service (en particulier `AMEESH_CONFIG`),
renseigner `CANON_DIR` avec le chemin du clone configuré, puis utiliser son
interpréteur et relever le SHA :

```sh
/opt/ameesh-vm-adapter/venv/bin/python - <<'PYCODE'
from ameesh import config, host_release
canons, unknown = host_release._canon_read(config.load())
print(len(canons), 'canon, unknown', unknown)
PYCODE
git -C "$CANON_DIR" rev-parse HEAD
```


La fenêtre doit couvrir le repos `max(idle_after_s, min_on_s, 2 ×
wake_delay_s)`, puis l'archivage, la restauration et la santé. La borne
au drainage est `max(idle_after_s, 2 × wake_delay_s) + wake_delay_s` : elle
ne connaît pas `min_on_s`, privé au worker. Celui-ci contrôle la durée exacte
au premier passage et signale en base une durée insuffisante. Un opérateur
peut passer outre la borne avec `--force-court REASON`, avec motif enregistré.
Relever les durées mesurées dès le premier cycle réel ; elles déterminent la
largeur des cycles suivants.

2. Le bureau active l'inscription (`enabled: true`, copie avant) et démarre
   le timer (R1 de D1). Avant l'étape 3, vérifier ses passes dans le journal :
   les ticks inactifs ne produisent pas de `vm_adapter_observations`. Après
   drainage, vérifier les observations ; un hook sans worker laisse l'hôte fermé.
3. Le pilote fusionne #297, puis fait la synchronisation prouvée. Sur AG3 et
   sur un autre hôte, la projection (`ameesh hosts ameesh-agents-3`) donne
   ce hook et `runs_interface: none (Host card)`.
4. Le drainage frais vient **après** l'étape 3 : `ameesh host drain
   ameesh-agents-3 --for 3h`. Un drainage antérieur au hook a consommé une
   partie de `host_release_drain_bound` (2100 s) depuis son `requested_ts` :
   le lever (`ameesh host undrain`) et en poser un nouveau. Si le drainage
   automatique est parti entre-temps, il tient lieu de drainage frais : ne
   pas en poser un second.
5. Suite de v3.4.1 sans changement : préchauffe (hystérésis), halt, preuve,
   archivage, poweron, santé dans les 15 min. Relever T_halt, T_archive,
   T_restore et T_health. Un retour qui n'aboutit pas se lit dans le journal
   du worker (#8072), ligne `health` de chaque passe : `stage=launched` puis
   `verdict=pending` tant que le tour de sonde court ; `verdict=ok` suivi de
   `checks=failed step=N` désigne un contrôle de santé rouge (au R1 du 22/11,
   la sonde avait joué ses tours et le refus venait de `health.py`, #8069) ;
   `verdict=ko`, un tour de sonde fini en échec. Après un refus, le runner de
   la sonde écrit « host claims closed (draining or returning) » : c'est la
   conséquence du retour en `draining`, pas sa cause.
6. Clôture : une fois AG3 revenu (`available`, prises ouvertes, santé
   verte), le pilote fusionne #298, puis fait la synchronisation prouvée.
   Ensuite le bureau arrête le timer (R2) et remet `enabled: false`.
7. Sortie d'incident, en moins de 15 min (même borne que le retour par
   poweron) :
   - AG3 n'a pas été arrêté (`draining`, VM running) : fusionner #298, faire
     la synchronisation prouvée, puis `ameesh host undrain ameesh-agents-3`.
     Sans hook, L232e rouvre les prises.
   - AG3 est arrêté ou en retour : d'abord le retour par poweron (busy,
     santé, réconciliation), **en gardant le hook**. Sans lui, L232e
     rouvrirait les prises sur une VM éteinte (« Contrôle et retour
     arrière »). Ensuite seulement : #298, synchronisation, R2.

Pendant la fenêtre, la prévalidation au gel est indisponible (son seul hôte
est AG3). C'est un résultat non concluant, qui ne retient rien (§3). Depuis
#7687 (comptage des jobs guest) et #8418, le hook est déclaré en
permanence : le mode décrit ci-dessus rend un burst après 30 min sans
travail, hors fenêtre, sans retirer `lend`.

## Mode veille de la base (#8420, #8029)

Les VM `ameesh-agents-1` et `-2` portent les deux standbys synchrones du hub
(deploy/serveur/replication/README.md, « Mode veille »). Avant d'arrêter la
première, la liste synchrone du hub doit être vidée, sinon chaque `COMMIT`
attendrait un standby éteint. Une cible qui porte une réplique se déclare
ainsi dans l'enrôlement :

- `"replica": true`, avec `standby_enter_argv` et `standby_exit_argv`
  (commandes fixes et absolues, comme les autres) ; sans `replica: true`,
  ces deux clés sont refusées ;
- `standby_timeout_s` au niveau de l'enrôlement, obligatoire dès qu'une
  cible est une réplique (entre `io_timeout_s` et 120 s).

Comportement du worker :

1. **Entrée, échec fermé.** Après chaque `quiesce` de la phase d'arrêt,
   `standby_enter_argv` s'exécute : avant de poser l'intention d'arrêt, puis
   à nouveau juste avant `halt_argv`. Une sortie non nulle (ou le délai)
   donne `standby enter refused` : ni intention d'arrêt, ni arrêt. Le tick
   suivant rejoue (le script est idempotent). Si l'intention était déjà posée,
   la borne `halt_deadline_s` court et son alerte `halt_deadline` nomme
   l'hôte, sans arrêt.
2. **Sortie après le retour sain des deux répliques.** Quand le retour d'une
   réplique est sain et réconcilié (`ready`), le worker vérifie que toutes
   les cibles `replica` sont revenues (portes ouvertes, ou retour de la
   génération courante terminé). Sinon il attend le retour de l'autre
   (`ready; standby exit waits for the other replica`). Sinon il lance
   `standby_exit_argv` (`mode-veille.sh sortir`, sans effet hors veille).
3. **Échec de la sortie : alerte, le retour tient.** Le retour n'est jamais
   défait. Le journal du worker porte
   `ready; standby exit failed (synchronous list left empty)` et un événement
   `vm.standby_exit_failed` (hôte, génération, époque) est écrit. La liste
   reste vide : état sûr, durabilité dégradée. L'opérateur rejoue
   `mode-veille.sh sortir` après `etat.sh` ; le retour suivant d'une
   réplique le rejoue aussi.

Commande opérateur. `mode-veille.sh` tourne en root sur le hub, et ses
attentes (`ATTENTE_TOURS` 600 s, `ATTENTE_REVEIL` 900 s) dépassent la borne
du worker. Les argv pointent donc sur un enveloppeur fixe, root:root 0755,
qui raccourcit ces attentes sous `standby_timeout_s`. Le worker rejoue à
chaque tick, et un processus tué au délai ne passe pas par le retour arrière
du script. Exemple, sur le hub :

```bash
#!/bin/bash
# /usr/local/sbin/ameesh-mode-veille (entrer|sortir)
set -euo pipefail
case ${1:-} in entrer | sortir) ;; *) exit 2 ;; esac
export AMEESH_REPLICATION_ATTENTE_TOURS=40 AMEESH_REPLICATION_ATTENTE_REVEIL=40
exec /opt/ameesh/src/deploy/serveur/replication/mode-veille.sh "$1" \
  --raison "vm-adapter #8029"
```

Une règle sudoers n'autorise que ces deux argv au compte du worker. Si le
contrôleur n'est pas le hub, l'enveloppeur est une commande SSH forcée vers le
hub, du même modèle que `vm-commande.py`. Aucun secret ne passe en argument.

**Préalable avant tout essai réel (bureau).** Vérifier l'état réel de la
réplication (EP8, #6860 livré, adoption #6959 en attente) avec
`deploy/serveur/replication/etat.sh` sur le hub. Si elle ne tourne pas, ne
déclarer aucune cible `replica` : `mode-veille.sh` refuserait (primaire hors
hub, ou aucun standby à rattraper au réveil) et aucune VM ne s'arrêterait. Ce
lot livre le code et ses témoins, sans essai réel.

Limite : l'échec de sortie est un événement et une ligne de journal, pas
encore une alerte de `host_release.alerts`. Un retour qui rouvre les portes
sans le worker (`ameesh host undrain`) ne lance pas `sortir` : le rejouer à
la main.

## Contrôle et retour arrière

Lire `vm_adapter_requests`, `vm_adapter_observations`, `host_lifecycle` et
le journal systemd. Aucune action n'est déclarée faite sur la seule réponse
POST : le GET suivant doit prouver l'état. Après interruption, le tick relit
le fournisseur avant d'agir ; un résultat perdu ne rouvre pas les claims.

Pour suspendre : le bureau arrête le timer et laisse les portes fermées.
Avant de retirer un hook ou de revenir à l'ancien binaire, le bureau remet
chaque VM arrêtée en marche par le chemin autorisé, vérifie santé et
réconciliation, et termine le retour natif. **Ne pas retirer le hook d'une
VM encore arrêtée** : le chemin historique sans adaptateur n'a pas cette
preuve cloud. La migration reste en place et les écrivains N-1 l'ignorent ;
l'attente périmée qu'ils laissent est ignorée par le nouveau lecteur, grâce à
la borne génération/époque.
