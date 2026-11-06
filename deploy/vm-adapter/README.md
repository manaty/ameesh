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

Un seul hôte opérateur, externe aux VM qu'il gère, exécute le timer. Son
inscription privée fixe les UUID VM/projet et la zone ; aucune identité,
commande ou clé cloud ne vient de la file. Les exécutions concurrentes
prennent un verrou transactionnel par hôte ; une transaction tient le verrou
natif de porte pendant les appels bornés pour empêcher une réouverture entre
la dernière preuve de drainage et l'effet cloud. Chaque appel externe est
borné à dix secondes au maximum. Les contrôles de santé et de réconciliation
s'exécutent hors de ce verrou, puis génération et relevé frais sont revérifiés
sous verrou avant l'acquittement. Un tick ne boucle ni sur l'API ni sur la santé.

L'arrêt est `poweroff`, jamais `stop_in_place`, arrêt du seul OS, suppression,
redimensionnement ou création. L'API doit ensuite rendre `stopped` avant
acquittement. Le calcul cesse alors d'être facturé ; disques et IP restent
facturés. Voir les sources opérateur :
[états Scaleway](https://www.scaleway.com/en/docs/instances/reference-content/instance-states/),
[actions Instance](https://www.scaleway.com/en/developers/cli/instance/) et
[facturation](https://www.scaleway.com/en/docs/instances/reference-content/understanding-instance-pricing/).

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
5. Fournir trois commandes fixes par VM, détenues par l'opérateur et non
   modifiables par les agents. Elles n'héritent d'aucune variable de secret
   cloud ; argv absolu, pas de shell, sorties supprimées, échec = attente.
   `quiesce_argv` vérifie l'arrêt propre de l'OS avant archivage, sans toucher
   à une autre VM. `health_argv` vérifie version/mode réellement actifs,
   runners vivants, doctor vert et courrier livré. `reconcile_argv` termine
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
6. Faire relire les mappings, les trois commandes, les durées, les créneaux,
   les permissions des fichiers et l'unité avec `systemd-analyze verify`.
   Tester le cycle cloud simulé avant tout feu réel. Faire valider séparément
   le premier arrêt/réveil et son temps mesuré par le bureau.
7. Au feu d'installation seulement : rendre l'inscription active, poser les
   unités, recharger systemd et activer le timer sur ce seul hôte. Déclarer
   le hook sans secret dans la configuration des seuls hôtes autorisés.
   Aucune commande de cette étape n'a été exécutée par l'auteur du lot.

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
