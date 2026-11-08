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
   existant (0600), contenu `{"secret_key":"…"}`. `LoadCredential` le rend
   lisible au seul service, jamais aux runners. Pas de token en argument,
   dans les courriers, dans le dépôt ou dans les erreurs. Écrire
   `database.env` (0600) avec le chemin `AMEESH_CONFIG` de la connexion mesh
   de l'opérateur. Le DSN ne contient aucune clé cloud.
5. Avant d'inscrire **chaque cible burst**, préparer son journal systemd
   persistant : un drop-in `/etc/systemd/journald.conf.d/persistent.conf`
   contient par exemple :

   ```ini
   [Journal]
   Storage=persistent
   SystemMaxUse=512M
   SystemKeepFree=1G
   ```

   Adapter ces bornes au disque de la cible et au volume de ses journaux :
   conserver le boot précédent jusqu'au contrôle de retour, sans saturer le
   disque. Le bureau applique la configuration en redémarrant
   `systemd-journald`, puis demande le vidage vers le stockage persistant
   (`journalctl --flush`) avant le premier arrêt réel.
   Au premier arrêt/réveil autorisé, vérifier que `journalctl --list-boots`
   liste le boot précédent et que `journalctl -b -1 -n 1 -o cat --no-pager`
   permet au contrôle `previous_boot` de lire sa fin propre (#6074).
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
   le hook sans secret dans la configuration des seuls hôtes autorisés.
   Aucune commande de cette étape n'a été exécutée par l'auteur du lot.

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
configuration et vérifie après redémarrage que `journalctl --list-boots`
expose le boot précédent et que sa dernière ligne est lisible par le groupe
systemd-journal. Sans preuve du boot précédent, la santé refuse : un journal
volatile ou un simple état SSH silencieux ne remplace pas cette preuve.
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
3. Créer uniquement `/var/lib/ameesh-vm-remote` sous le compte VM, 0700.
   Le compte y écrit ses reçus et son verrou, jamais dans le dépôt ni dans
   les répertoires des agents. Archiver les anciens reçus hors activation
   lorsque leur vague est révolue ; ne pas enlever celui du retour courant.
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
6. Fournir les deux contrôles locaux **réels**, root non inscriptibles,
   `checks.version-mode` et `checks.doctor`, argv fixes dans `remote.json`.
   Le premier vérifie la version et le mode effectivement lancés, le second
   doctor vert ; le dispatch les borne à 5 s, environnement PATH/LANG seul,
   sorties supprimées. Les chemins d'exemple ne sont pas des contrôles
   installés : absents, ils refusent. Ils ne doivent jamais être remplacés
   par `true`. Leur preuve reste exigée par #6002 avant activation.
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
