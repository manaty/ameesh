# H8 — pont Unix local privilégié

Source : décision #137536, option b, signée le 25/10/2026 à 09:31Z,
approbation #108 ; lot #3509, manaty/ameesh#2692. La décision autorise le
code et sa relecture. La décision d'enrôlement distincte doit encore nommer
les hôtes, opérateur, identités, unités, racines, SHA, canari et retour arrière.

## Frontière

Le runner reste sous `ameesh`. Le companion H7 utilise `LocalDispatcher`,
jamais `HostEffects` en processus local. L'adresse n'est pas configurable :
`/run/ameesh-handoff/local.sock`. Son parent est root non modifiable par les
agents, le socket root:ameesh 0660. Le client contrôle les propriétaires et
le pair root par `SO_PEERCRED`. Le service contrôle l'UID du pair par le même
mécanisme ; PID, UID ou compte déclarés dans la requête JSON n'ont aucune
autorité.
Le groupe du socket autorise seulement la connexion, l'UID exact reste exigé.

Le service systemd `Accept=yes` traite une connexion puis quitte. Le JSON
est borné à 65536 octets, doublons et champs supplémentaires refusés ; le
client ferme son côté écriture pour terminer le message. Une seule requête,
aucune commande, argv, variable d'environnement, unité ou chemin nouveau
n'est accepté. La politique root `/etc/ameesh-handoff/local.json` contient
exactement ces clés (valeurs à établir par l'opérateur) :

```json
{"uid": 1001, "host": "ameesh-agents-1", "agents": ["agent-canari-a-designer"]}
```

L'exemple n'est pas une admission. `uid` est l'UID réel du compte qui exécute
les runners (`ameesh` aujourd'hui, ou un UID dédié si le plan d'enrôlement le
retient) ; `host` celui de `execution.json`, `agents` la liste approuvée. Le fichier
est root:root 0600 et tous ses parents root sans écriture groupe/autres.
Chaque connexion recharge cette politique et la recoupe avec `execution.json` :
hôte identique et agents inclus dans les comptes enrôlés ; agent et hôte sont vérifiés avant
l'effet. L'inventaire exige l'UID/hôte mais ne modifie rien. H7 relit ensuite
`execution.json` root pour les comptes Unix, unités, racines et outils admis.
Ses verrous, clôtures d'époques, journal d'intent et reçus demeurent communs
aux appels locaux et distants : une opération retardée ne contourne pas une
époque plus récente. Un rejet de transport ne fabrique jamais un reçu négatif
ou un succès : un effet peut avoir commencé et exige alors réconciliation.

Délai global serveur 55 s, client 60 s ; durée systemd 65 s et arrêt 5 s,
`KillMode=control-group`. Les enfants sont aussi bornés par H7. Au plus huit
connexions actives par socket ; chaque requête d'un agent partage le verrou
H7. Le journal systemd ne reçoit qu'une catégorie fixe (`refused`, `uncertain`
ou `unavailable`) avec le PID et l’UID du pair obtenus du noyau (−1 avant
identification), jamais le corps reçu, les chemins d'un refus ou les secrets.
Une erreur de contrat survenue après l'appel de l'effet est classée `uncertain`,
jamais `refused` : le reçu manquant peut suivre un effet, et le client
réconcilie ; un refus sans effet reste `refused`. Les reçus et intents privés
restent dans le journal H7 enrôlé.

## Installation par l'opérateur après relecture et enrôlement signé

1. Épingler le SHA relu dans le plan signé. Préparer depuis ce SHA le runtime
   indépendant `/opt/ameesh-release/runtime`, paquet `ameesh` et dépendances
   compris ; rien ne provient du checkout modifiable du runner. Préparer les
   scripts compagnons dans `/opt/ameesh-release/lib` et les deux fichiers de
   `deploy/mise-a-jour/systemd/` dans `lib/systemd/`. Toute la chaîne et le
   contenu sont root, sans écriture groupe/autres ; scripts 0644, dossiers
   0755. Le runtime doit être au même SHA que les compagnons. Le paquet wheel
   fournit les fichiers sous `share/ameesh/mise-a-jour`, pas leur installation
   privilégiée automatique. Pas de `.pth`, dépendance ou lien vers un arbre
   d'agent. L'installateur refuse les liens de dossiers et vérifie les cibles
   des liens de fichiers ; il refuse effectivement les `.pth`, FIFOs, sockets et
   autres fichiers spéciaux. Le scellement cryptographique du contenu du runtime
   est suivi par le lot #3526 (manaty/ameesh#2692), préalable distinct à l'enrôlement.
2. Installer les documents H7 et `local.json` validés par la décision, les
   comptes et groupes réels, journaux root privés. Établir notamment le bus
   user `/run/user/<uid>/bus` : le service root emploie `runuser` et les deux
   variables XDG/DBUS explicites de H7 pour l'unité user admise. Ne pas étendre
   les droits du runner ni ajouter sudo. Le service doit pouvoir joindre les
   bus, dépôts Git et bases enrôlés ; sa politique ne crée pas ces accès.
3. Exécuter en root le script installé avec le runtime épinglé :
   `/opt/ameesh-release/runtime/bin/python -I -B /opt/ameesh-release/lib/installer-local-handoff.py install`.
   Il impose 0755 au dossier du socket, vérifie le bundle root, l'UID, le document H7 et l'absence d'unités
   préexistantes, copie exclusivement les deux unités fixes en 0644, consigne
   leurs empreintes dans `local-install.json` root 0600, recharge systemd et
   active le socket. Aucun agent, login ou handoff n'est lancé par ce geste.
4. Consigner SHA réel, propriétés systemd, UID pair, refus d'un compte tiers,
   inventaire, puis canari H7 selon le plan signé. Qualifier interruption,
   réconciliation et retour arrière sur le même SHA avant élargissement.

## Révocation et retour arrière

Pour retirer une admission, modifier atomiquement la politique root : les
nouvelles connexions refusent immédiatement. Pour couper aussi les requêtes
déjà admises, arrêter le socket puis toutes les instances
`ameesh-handoff-local@*.service` ; une interruption peut laisser un intent,
à réconcilier avant reprise, jamais à effacer pour forcer un succès.

Exécuter le même installateur avec `rollback`. Il vérifie les empreintes des
unités contre le reçu, désactive et arrête le socket, arrête toutes les
instances, puis retire uniquement les unités qu'il a installées et son reçu.
Un fichier modifié exige examen opérateur, pas d'écrasement. Les documents,
journaux, code et runtime sont conservés. En cas d'installation partielle,
le reçu conserve les unités écrites ; le rollback accepte les unités absentes,
arrête toutes les instances même sans fichier socket et retire les fichiers
restants après vérification du reçu. Aucun retour au chemin local en processus n'existe : sans socket le
runner retient l'opération. La version précédente et les journaux H7 restent
le point de reprise selon le plan d'enrôlement signé.
