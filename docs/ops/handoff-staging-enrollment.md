# Manifeste d'enrôlement staging du handoff

Ce manifeste prépare une décision et n'autorise aucune création ni installation.
`MANQUANT` signifie refus : aucune interpolation, identité par défaut ou reprise
sur le compte `ameesh`. Le bureau est l'opérateur des gestes autorisés (canon
§10/§14). L'entrée signée est `deploy/mise-a-jour/entree-operateur.py`, avec
`installateur-bootstrap.py` et `role-executor.sh` ; leur présence ne remplace
pas une décision portant sur les nouveaux comptes et canaux.

## Sources et portée par hôte

| Hôte | Source d'identité et exécution | État établi | Champs manquants avant geste |
| --- | --- | --- | --- |
| `ameesh-agents-1` | Canon `hotes/ameesh-agents-1.md`, WireGuard `10.77.0.3` ; `DEPLOIEMENT-HOTE-AGENTS.md` : compte `ameesh`, unités user `ameesh-runner-agent@<agent>.service` | Lecture locale 25/10/2026 : `ameesh` uid/gid `1001:1001`, groupes `ameesh,docker` ; home `/home/ameesh` `0750` ; template `/home/ameesh/.config/systemd/user/ameesh-runner-agent@.service` `0644` ; unité `ameesh-runner-agent@pilot-dryrun-3389.service` loaded/active ; comptes `ameesh-handoff` et `ameesh-secrets`, `/etc/ameesh-handoff`, `/etc/ameesh-secrets`, `/opt/ameesh-release`, `/var/lib/ameesh-handoff` absents | Mandat signé, choix de l'agent canari (l'unité observée n'est pas réaffectée), compte harness, destination, clé, source réseau, runtime root et documents |
| `ameesh-agents-2` | Canon `hotes/ameesh-agents-2.md`, WireGuard `10.77.0.4` ; même contrat d'unités user sous `ameesh` | `ameesh host show` : aucun exécuteur enrôlé ; aucun contrôle distant de comptes, groupes, fichiers ou unités effectué | Lectures `id`, `stat`, propriétés systemd par le bureau ; agent, home, compte harness, unité, caps, clé, source réseau et mandat |
| `manaty-mesh-1` | Canon `hotes/manaty-mesh-1.md` : base mesh, `max_agents: 0`, unités système historiques `ameesh-runner@` | `ameesh host show` : aucun exécuteur enrôlé, aucun agent admis | Hors destination de ce canari : admission canon explicite nécessaire avant toute proposition d'exécuteur ; comptes, groupes, chemins et canal non établis |
| `pc-smichea` | Canon `hotes/pc-smichea.md` : poste owner, racines par placement ; ne pas transposer `/home/ameesh` | `ameesh host show` : aucun exécuteur enrôlé ; aucun contrôle distant effectué | Utilisateur/groupe/home/unité réels, compte pilote distinct, agent, caps, canal et mandat ; L232d séparé avant login |

`ameesh host list` répond « aucun exécuteur enrôlé, aucun code en attente ».
Le registre vivant nomme aussi `pilot-dryrun-3389` (deepseek, agents-1,
`/home/ameesh/work/pilot-dryrun-3389`) et `pilot-resident-3389` (claude,
agents-2, `/home/ameesh/work/pilot-resident-3389`), ainsi que sa variante
`pilot-resident-3389-codex` (codex). Leur `session_account` est nul : ce n'est
pas une identité de compte pilote établie. Les citer n'autorise pas leur
réaffectation ou leur arrêt ; le pilote doit sélectionner le canari.

Cette vue et les unités natives sont des faits différents : une unité active
ne vaut pas enrôlement de transport, et un nom admis dans le canon ne prouve
pas son état vivant. Aucune commande SSH de découverte n'a été improvisée.

## Inventaire à signer pour chaque hôte retenu

La proposition minimale porte d'abord sur les deux VM d'agents ; source,
cible et agent canari restent à désigner explicitement. Les deux autres hôtes
restent exclus tant que leurs champs ne sont pas établis. Le bureau doit
produire un exemplaire **complet par hôte**, lié au machine-id observé, avec
les valeurs suivantes. Une signature sur des champs `MANQUANT` ne permet pas
l'installation.

| Champ | Valeur ou contrat exact |
| --- | --- |
| Opérateur | `bureau`, mandat d'enrôlement staging signé : **MANQUANT** |
| Décision existante | #135297 option a, signature owner vérifiée : login et déplacement reportés après câblage et relecture ; ce n'est pas une délégation SSH nouvelle |
| Code | Dépôt `https://github.com/manaty/ameesh.git`, arbre H7 et dépendances H1–H6 ; SHA final à épingler après intégration, **MANQUANT** au plan d'installation |
| Machine | Nom exact ci-dessus, machine-id : **MANQUANT** ; adresse réseau canon seulement pour agents-1/2 |
| Exécuteur | Utilisateur `ameesh` sur VM ; uid/gid réels par hôte, home privé, agent/harness/compte exact et caps : **MANQUANT** hors observations ci-dessus |
| Unité | Scope `user`, `ameesh-runner-agent@<agent signé>.service` sur VM ; instance canari **MANQUANTE** ; aucune unité nouvelle implicitement créée |
| Dépôt de travail | Identité de repository, branche et SHA propre poussé, racines autorisées et cwd exact : **MANQUANT** |
| Ancienne version | Chemin venv et SHA réellement actifs, documents précédents et inventaire des fichiers créés : **MANQUANT** par hôte |

## Comptes, groupes et documents : plan proposé, pas état installé

Les créations suivantes demandent la nouvelle décision. Aucun uid/gid,
membre supplémentaire ou mot de passe n'est présumé. UID/GID numériques,
home de transport, shell et état de verrouillage seront consignés par le
bureau dans le plan exact ; absence = refus. Pas d'appartenance `sudo`,
`docker` ou groupe d'agents pour les comptes de transport.

| Objet | Propriété/mode proposés ou imposés par le contrat |
| --- | --- |
| Compte/groupe `ameesh-handoff` | Compte de transport dédié H2 ; uid/gid **MANQUANTS** ; aucun login interactif ni droit général |
| Compte/groupe `ameesh-secrets` | Compte distinct L232c ; uid/gid **MANQUANTS** ; aucun partage de clé ou délégation avec handoff/restart |
| `/etc/ameesh-handoff` | Racine fixe H1 ; proposition `root:root 0755` pour consultation des clés publiques SSH ; documents privés séparément protégés |
| `enrollment.json`, `hosts.json`, `repositories.json`, `secrets.json`, `transports.json` sous cette racine | Contrat H1 : réguliers, non liés, lien unique, `0600` ou `0640`, propriétaire contrôleur ou root ; proposition VM `root:ameesh 0640` pour admission du runner ; contenu exact **MANQUANT**, aucune référence réelle au canari |
| `/etc/ameesh-handoff/execution.json` | Contrat H2 : identité d'hôte, `state`, `config`, `release_store`, `accounts`, `roots`, `tools`, `secrets` fermés ; proposition `root:root 0600` pour l'exécuteur root ; contenu exact **MANQUANT** |
| Journaux handoff | `execution.json.state` est un chemin absolu enrôlé ; proposition `/var/lib/ameesh-handoff`, `root:root 0700`, fichiers `0600` ; valeur à approuver, pas une constante implicite du code |
| Clé privée et known_hosts du contrôleur H2 | Chemins absolus sous `/etc/ameesh-handoff`, noms **MANQUANTS** ; `0600`, propriétaire uid effectif du contrôleur ; chaîne de parents non liée, détenue par root/contrôleur, sans écriture groupe/autres ; known_hosts épinglé par digest public |
| `/etc/ameesh-secrets` | Contrat du bundle `delegation-secrets.py` : `root:root 0755` pour recherche authorized_keys ; documents restent privés |
| `policy.json`, `helper.json`, `transport.json` sous cette racine | L232c/H4 : root-owned `0600` ou `0640` selon lecteur autorisé ; contenu fermé exact **MANQUANT** ; `policy` nomme chaque destination `(host,account,path,owner,group,mode,dir_mode)` ; aucune destination ni référence réelle choisie par ce manifeste |
| `/var/lib/ameesh-secrets` | Contrat L232c `root:root 0700` ; rotations et intents `0600`, aucun contenu ni digest de secret dans les preuves |
| Destination canari L232c | Bridge `0600` dans `0700`, détenus par compte/groupe exacts ; ou store système `0640` dans `0750` selon politique ; chemin/compte **MANQUANTS**, pas de remplacement de fichier non géré |
| Runtime indépendant | Contrat L232b/L232c `/opt/ameesh-release/runtime/bin/python`, root-owned, tous parents sans écriture non autorisée ; sources adjacentes root-owned `0644`, répertoires `0755` ; installation **MANQUANTE** |
| Exécuteur handoff privilégié | Proposition de chemin `/opt/ameesh-release/lib/hote-handoff.py`, dans runtime root isolé `-I -B`, sources/transitifs et paquet `ameesh` au même SHA ; aucun chargement du checkout modifiable des agents |

Les permissions du contrôleur doivent être cohérentes avec le lecteur réel :
un runner `ameesh` ne peut pas lire une clé root `0600`. Ne pas élargir la clé
ni ajouter silencieusement le runner à un groupe. H8 remplace l'appel local en processus par le socket Unix privilégié fixe
([contrat et installation](../HANDOFF-LOCAL.md)). La décision #137536 b
autorise son développement ; son enrôlement reste à nommer ici : UID du
client, hôte, agents admis, SHA du runtime root, politique 0600 et unités
systemd. Ce manifeste n'attribue pas automatiquement au client local H2 les
privilèges root du dispatcher distant. Un socket absent ou refusé, ou un
besoin de privilège local non satisfait, reste un refus à résoudre
explicitement avant qualification.

## Clés et restrictions du canal

Le client H2 ne fournit **aucune commande distante** : le canal doit imposer
le dispatcher par configuration administrateur. Le contrat de transport
fournit `host,address,port,user,host_key_alias,known_hosts,known_hosts_sha256,
identity_file,seconds`. `user` vaut exactement `ameesh-handoff`, borne
`seconds` entre 5 et 360. Clé publique Ed25519, empreinte publique, clé d'hôte,
source CIDR autorisée et chemins exacts sont **MANQUANTS pour chaque paire**.
Ils doivent être ajoutés au bundle signé avant toute installation ; aucune clé
privée ne quitte son fichier protégé ou n'entre dans ce manifeste.

Proposition handoff, dérivée des restrictions de `delegation-release.access_files`
mais délégation nouvelle à approuver : wrapper
`/usr/local/libexec/ameesh-handoff-ssh` `root:root 0755`, authorized_keys
`/etc/ameesh-handoff/authorized_keys` `root:root 0644`, sudoers
`/etc/sudoers.d/ameesh-handoff` `root:root 0440`, Match SSH
`/etc/ssh/sshd_config.d/62-ameesh-handoff.conf` `root:root 0600`.
La clé porte `restrict,from="<source signée>",command="<wrapper fixe>"`.
Le wrapper refuse arguments et `SSH_ORIGINAL_COMMAND`, vide l'environnement,
et lance uniquement le Python root isolé `-I -B` et le dispatcher fixe,
sans argument. La règle sudo n'admet que cette commande exacte, sans SETENV.
Le Match impose clé publique seule, `AuthorizedKeysCommand none`, pas de
mot de passe/KbdInteractive, `ForceCommand` fixe, `DisableForwarding yes`,
`PermitTTY no`, `PermitUserRC no`, `MaxSessions 1`.

Le canal secrets utilise **son bundle existant distinct**
`delegation-secrets.py` : wrapper `/usr/local/libexec/ameesh-secrets-ssh`,
`/etc/sudoers.d/ameesh-secrets`, clé `/etc/ameesh-secrets/authorized_keys`,
Match `/etc/ssh/sshd_config.d/61-ameesh-secrets.conf`, dispatcher
`/opt/ameesh-release/lib/hote-secrets.py`. Mêmes propriétaires/modes et
restrictions ci-dessus, clé et source signées propres au canal. Le bureau
rend et fait relire les fichiers exacts ; aucun copier-coller des ellipses
ou champs manquants n'est installable. `visudo` et `sshd -T` sous l'identité
cible doivent valider les règles effectives avant activation de la clé.

## Installation, canari et retour arrière

1. Avant geste : décision complète par hôte, deux avis requis, inventaire des
   états précédents et propriété de tous les parents ; pas de création par
   l'agent auteur. Le bureau pose les documents via son entrée signée.
2. Installer le code épinglé dans un nouvel environnement (`umask 022`).
   Le canari paquet de `HANDOFF-STAGING.md` passe hors checkout, sans secret.
   Le bureau qualifie ensuite l'agent et les canaux **nommés**, avec valeur
   synthétique uniquement, et vérifie absence dans sorties/journaux/preuves.
3. Refus à démontrer sans effet : document/identité/clés absents ou altérés,
   permissions ouvertes, unité non admise, époque révoquée, compte non prêt.
   L'absence actuelle ne s'efface pas par changement d'utilisateur SSH.
4. Retour : retirer d'abord la clé de contrôleur ajoutée ; réconcilier et
   quiescer toute intention avant rejeu. Restaurer uniquement les snapshots
   signés dont le fichier courant correspond encore au bundle ; retirer
   seulement les fichiers créés et encore identiques. Garder journaux/fences,
   reçus, état DB et destinations installées ; ne pas supprimer les comptes
   ou homes par défaut. La suppression d'un compte est un geste distinct.
5. Restaurer le venv précédent et ses documents, vérifier processus réel,
   unités, baux et santé. Après switch, reprise en avant sur la cible : aucun
   réveil aveugle de la source. La qualification staging réelle reste à faire
   par le bureau après l'enrôlement, pas par le canari local de H7.

Références : [déploiement des hôtes](../DEPLOIEMENT-HOTE-AGENTS.md),
[L232c](../L232C-SECRET-STORE.md), [L232d](../L232D-OAUTH-ADAPTERS.md),
[L232b](../L232B-RELEASE-CONTROLLER.md), [handoff](../HOST-HANDOFFS.md),
[étude H7](../design/etudes/handoff-reel-decoupage.md),
[qualification du paquet](../HANDOFF-STAGING.md).
