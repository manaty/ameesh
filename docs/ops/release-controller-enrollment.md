# Enrôler le contrôleur de release indépendant

Cette opération administrative complète le bootstrap ; elle ne remplace ni sa
signature, ni les décisions de release que le contrôleur exécutera ensuite.
`enroler-controleur.py` publie sept documents privés à des destinations fixes,
admet la fabrique complète, puis démarre une seule unité. Le service produit
lui-même `readiness.json`. Une valeur déposée par un opérateur ne constitue pas
une preuve de disponibilité.

## État du pilote et topologie retenue

Au constat bureau du 30 octobre 2026, 19:36 UTC, lot #4989
([ticket #3736](https://github.com/manaty/ameesh/issues/3736)), les sept documents,
les références de sauvegarde, la clé privée et les known_hosts dédiés sont
absents sur agents-3. Les fichiers `turn.json`, le sceau, le verrou permanent et
l'unité proviennent du bootstrap signé #169540. L'installation est retenue à
`validate`, avant `enable` ; aucun démarrage réussi n'est établi.

L'arbitrage mesh-design du 30 octobre, consigné au lot à 19:38, demande une
**nouvelle clé de transport locale**, jamais une copie des trois clés de
bureau-goal. Sa génération et sa délégation sont des opérations
administratives distinctes à signer : `keygen` génère la nouvelle privée
localement, puis `prepare` inclut seulement sa publique et son empreinte dans
le mandat d'enrôlement. L'outil n'édite aucun `authorized_keys`. Les destinations doivent autoriser la nouvelle clé
avec `restrict`, une source mesurée et la commande forcée existante, dont le
dispatcher exige encore le manifeste signé et l'intent du contrôleur.

Le bootstrap #169540 ne couvre pas cette nouvelle clé : ses octets signés
n'autorisent que la clé du bureau depuis `78.232.62.207/32`. Ajouter une clé
locale ou modifier cette source requiert un nouveau bundle/mandat ; reprendre
l'ancien `enable` ne doit pas écraser une délégation plus récente. L'arbitrage
mesh-design du 30 octobre, consigné à 19:59, impose l'ordre suivant : génération
locale signée, nouveau bundle et décision de remplacement signée, documents et
démarrage, validation réelle, puis activation des deux clés. La supersession
native relève du compagnon #4991 ; la continuation de bootstrap relève de #4989.
Ces deux mécanismes doivent être livrés avant la reprise réelle. Les clés de
bureau-goal restent sur cet hôte. La nouvelle privée reste hors sauvegarde ;
sa perte impose un réenrôlement. La révocation dans les destinations relève du
mandat de délégation correspondant ; l'arrêt du service décrit ici conserve
ses documents et ne révoque pas une clé.

Le nouveau plan conserve `controller_key` et `controller_source` pour le bureau
et ajoute `local_transport`, objet contenant seulement `controller_key` et
`controller_source`. La deuxième clé doit être distincte et sa source une seule
adresse IPv4 `/32` ou IPv6 `/128`, mesurée par le bureau et différente de la source du bureau. Le rendu produit
exactement deux lignes `restrict,from=...,command=...` vers le même wrapper.
La question de bootstrap lie aussi la source locale et le digest de sa publique ;
la validation de la configuration effective SSH porte sur les deux sources.
Un ancien plan sans `local_transport` conserve son rendu à une clé.

## Inventaire privé et sources

Les sept fichiers sont des JSON UTF-8 réguliers, à lien unique, `root:root
0600`, sous `/etc/ameesh-release`. Tous leurs parents sont root, sans lien ni
écriture de tiers. Les documents sources ont les mêmes protections dans
`/var/lib/ameesh-release-enrollment/OPERATION`, répertoire `root:root 0700`.
Aucun contenu privé ne va dans Git, une PR, un ticket, une sortie ou un courrier.

| Document | Champs exacts et provenance à préparer par le bureau |
| --- | --- |
| `enrollment.json` | `host`, `machine_id` mesurés ; `policy` relue (host, unités, gestionnaire system/user explicite, unité contrôleur, délai) ; `database` identifiant logique de la base. La politique inclut l'unité propre au canari de `turn.json`. |
| `database.json` | `backend="postgres"`, `dsn`, `schema` ; connexion indépendante explicitement enrôlée, jamais une configuration ambiante ou un profil d'exécuteur promu implicitement. Le bureau indique la source privée et les droits accordés, sans valeur dans les traces publiques. |
| `service.json` | `runtime_tree_sha256`, `source_tree_sha256` des arbres root immuables déjà publiés. Aucune reconstruction ou modification de ces arbres par l'enrôleur. |
| `card.json` | `units`, `properties`, `guard`, `seconds` selon `verification-release.validate_card` ; propriétés provenant d'un relevé effectif relu, pas d'une supposition sur une unité système ou utilisateur. |
| `destinations.json` | Table bornée nom → destination sous `/etc/systemd/system/` ou `/etc/ameesh/`, distincte et validée par `configuration-release.validate_destinations`. Le mandat autorise cet inventaire, pas une publication de configuration de release. |
| `transport.json` | `host`, `address`, `port`, `user="ameesh-release"`, `host_key_alias`, `known_hosts`, `known_hosts_sha256`, `identity_file`, `seconds` ; endpoint réel et clé hôte vérifiée par un canal indépendant, nouvelle privée locale et connue seulement de root. Fichiers de clé et known_hosts déjà protégés, root 0600. Pas de copie depuis bureau-goal. |
| `backup.json` | `dsn` identité de sauvegarde de la **même cible** que `database.json` ; `restore_dsn` identité de restauration ; `retention_seconds`, `restore_capacity`, `remote` (`null` ou `host`, `upload_credentials`, `restore_credentials`). Origine de chaque identité et chaque fichier credential à documenter dans l'inventaire privé, jamais leur valeur. |

`turn.json` reste le document existant, validé par `pilote-sante` ; le mandat
lie son empreinte et ne le remplace pas. `restore_capacity` référence
l'attestation fraîche du producteur pour la cible de restauration ; l'enrôleur
n'en fabrique pas. Les credentials distants, s'ils sont utilisés, proviennent
des identités upload et restore distinctes déjà enrôlées.

Le contrôle ne crée ni compte SQL ni compte Unix, ne copie aucun secret depuis
une autre machine et ne choisit aucun credential absent. Le dossier de stage,
les known_hosts et les identités DB/restauration sont
préparés par le bureau sous leurs mandats propres ; la clé utilise la phase
signée ci-dessous. **Leur absence est un refus
nommé dans le plan de travail, pas une raison de fabriquer une readiness.**

## Génération locale séparément signée

Le bureau prépare d'abord le stage 0700 et `input.json` 0600, avec les seuls
champs `version=1` et `expires_at`. `key-question OPERATION` produit une question
préfixée `Approve generating this host-local release transport key below:`,
liée à l'opération, l'hôte, la machine, la clé hôte, le digest du code, la fenêtre,
l'empreinte du turn et le compte/UID non privilégié de l'exécuteur.
Les options sont `keygen` et `leave`. Après décision signée, exécuter
`keygen OPERATION --decision N` avec le runtime isolé et l'enrôleur installés.

La privée neuve est créée uniquement dans
`/var/lib/ameesh-release-enrollment/OPERATION/transport/identity`, root 0600,
parent root 0700. La génération utilise `ssh-keygen` Ed25519 sans passphrase ;
un intent durable précède l'appel. La publique est dérivée de la privée par
le binaire fixe et seule elle, avec son SHA256, est retournée. Une sonde
`runuser --user COMPTE -- test -r CHEMIN` doit renvoyer le refus de lecture par
le compte des agents, en succès comme en refus de génération. Un résultat
incertain n'est jamais régénéré ou supprimé automatiquement. Une reprise
complète revalide la signature et la publique sans remplacer la privée.

Le mandat d'enrôlement et la délégation de destination utilisent cette publique ;
la privée ne quitte pas son hôte et ce stage est exclu des sauvegardes. Sa
présence sans reçu de génération complet est refusée. La génération ne publie
ni délégation ni secret de base, et ne démarre aucun service.

## Préparation protégée

Après livraison du code par paquet et staging, le bureau installe l'enrôleur et
ses compagnons root, hors checkout agent, puis enrôle leur digest dans
`/etc/ameesh-admin/backend.json` sous une décision exacte comme celle du backend
administrateur. La présence d'un fichier Python root ne suffit pas : le digest
du répertoire de code doit être celui enrôlé. Les arbres déjà publiés ne sont
jamais enrichis sur place.

Le bureau prépare les sept JSON source avec le même `input.json` et la même
fenêtre que la génération ; tous sont 0600 dans le stage 0700 de l'opération.
Les valeurs privées viennent des références recensées ci-dessus. Aucune
commande ne reçoit DSN, mot de passe ou clé en argument.

Depuis le runtime isolé root déjà publié, avec `-I -B`, exécuter l'enrôleur
installé root, en lui passant `prepare OPERATION`. Aucun argument ne sélectionne
une destination, un programme, une unité ou une racine alternative. La sortie
contient le bundle et la question exacte ; `bundle.json` est conservé dans le
stage. Le bundle lie : opération, hôte, machine-id, clé hôte, digest du backend,
expiration, politique, arbres, empreintes des sept fichiers, publique/empreinte de la clé locale, empreinte du turn,
unité exacte et cinq répertoires privés fixes. Aucun corps de document privé
n'apparaît dans cette sortie.

La préparation refuse une unité différente, un arbre writable ou différent,
un fichier existant à écraser, une clé/known_hosts absente ou non protégée,
un turn hors politique, une sauvegarde d'une autre base, un service déjà actif,
une file d'opérations non vide ou une réservation de release en cours.

## Mandat exact et exécution

Le texte à signer est **exactement** le champ `question` produit par `prepare` :

```text
Approve the exact independent release controller enrollment below:
<JSON canonique du bundle produit sur l'hôte>
```

Les options sont celles de `enroler-controleur.OPTIONS` (`enroll`, `leave`). Le
bureau crée la demande `ameesh decide ask --needs-signature` sur le lot ; il
utilise la question sauvegardée sans la réécrire ni en modifier les octets.
Le signataire doit couvrir toute la fenêtre `expires_at`. Un courrier, une
réponse non signée ou l'ancienne décision de bootstrap ne vaut pas ce mandat.

Après vérification de la signature, le bureau appelle le même enrôleur root et
le même runtime `-I -B`, arguments `enroll OPERATION --decision N`. L'outil :

1. Acquiert exclusivement le **verrou permanent existant** du sceau, sans le
   créer, remplacer, tronquer ni supprimer ; revalide le bundle et l'autorité.
2. Publie chaque fichier absent en 0600, intent durable avant écriture et reçu
   après fsync. Une reprise ne réécrit pas un fichier déjà conforme ; un résultat
   partiel ou divergent reste à réconcilier. Aucun ancien journal de bootstrap
   n'est édité.
3. Prépare ou vérifie les seuls répertoires privés fixes : opérations,
   controller, configuration, backup, retention. Aucune correction de mode ou
   propriétaire d'un répertoire préexistant n'est tentée.
4. Ouvre la connexion indépendante installée et appelle la fabrique du **code
   immuable publié**, `HostFactory.admit`, avec toutes ses capacités ; tout
   manque refuse le démarrage.
5. Recharge systemd et démarre uniquement `ameesh-release-controller.service`,
   sans l'activer au boot. Un intent de start incertain ne déclenche pas un
   second start : la reprise observe la même instance ou demande réconciliation.
6. Attend au plus 30 secondes la vraie readiness, liée à l'hôte, aux arbres,
   au boot et à l'InvocationID active dans `system.slice`. Le reçu conserve cette
   instance ; une instance différente n'est pas adoptée comme la précédente.

La publication des documents et le démarrage ne constituent pas une release :
la file doit être vide et chaque futur job exige sa propre décision signée.
Le pilote #3392 ne reprend que sous le mandat de bootstrap approprié, après
preuve native d'admission et contrôle de sa délégation effective.

## Retour arrière et preuves

`stop-question OPERATION` rend une **autre** question, préfixée
`Approve stopping this exact enrolled release controller below:` et contenant
le même bundle. Les options distinctes sont `stop` et `leave`. Une nouvelle
décision signée autorise `stop OPERATION --decision N`, dans la fenêtre du
bundle. La signature enroll ne peut pas autoriser stop, ni inversement.

L'arrêt refuse une file non vide, une réservation ou une instance différente
de celle enregistrée. Il arrête cette seule unité, vérifie inactive et conserve
les documents, le journal de reprise, les arbres, le verrou et tous les journaux
natifs. Il ne révoque pas une clé chez une destination ; cette révocation exige
l'opération de délégation distincte. Un start dont aucune instance n'a pu être
observée reste uncertain : conserver l'intent et faire signer un mandat de
réconciliation précis, sans suppression manuelle de journaux.

Dans le lot : lien de la décision et validité réelle, empreinte du bundle,
identités de l'hôte et du backend, hashes/modes/propriétaires des documents,
résultat d'admission, LoadState/ActiveState/Slice/MainPID/InvocationID, boot,
readiness native et preuve du verrou inchangé. Aucun contenu privé. Le bureau
relève aussi le refus de lecture de la clé par le compte des agents, en succès
comme en refus de l'opération de clé. La qualification réelle suit le staging ;
les tests locaux simulent services et credentials et ne la prouvent pas.

Les sorties `prepare`, `key-question` et `stop-question` fournissent aussi les
options exactes à transmettre à la décision, libellés compris. Une question
d'enrôlement dépassant la limite native de 4 000 caractères est refusée
nommément avant demande de signature ; réduire le périmètre de la politique.
La sonde de lecture privée exige le code 1 de `test -r` sans aucune sortie :
un diagnostic de `runuser` ne prouve pas l'isolation. Les refus propres à
l'enrôleur sont rendus par leur libellé ; les autres exceptions restent masquées.

L'arrêt peut recevoir une fenêtre signée neuve après expiration de l'enrôlement :
`stop-question OPERATION --expires-at UNIX_SECONDS` lie cette nouvelle échéance
au bundle original et conserve `enrollment_expires_at` dans la question. Cette
signature n'autorise que `stop` et ne prolonge ni l'enrôlement, ni le démarrage.
Les documents, la clé, les reçus et l'identité de l'instance restent vérifiés.

Avant la continuation de bootstrap, le backend administratif doit être
réenrôlé avec le code intégrant `decisions.require_current` de #4991. Un backend
ancien ne connaît pas la supersession : une décision remplacée y resterait
exécutable. La nouvelle demande indique `--supersedes ID --supersedes-option
install` ; seule la réponse signée `install` active le remplacement. La
continuation décrite ci-dessous vérifie les deux liens effectifs réciproques,
puis rapproche les anciens reçus sans les réécrire.

## Continuation native du bootstrap interrompu

Le backend fournit désormais `continuation-question NEW --previous OLD
--decision OLD_DECISION`, puis `continue-install NEW --decision NEW_DECISION`.
Le bureau prépare d'abord NEW par le parcours `prepare` habituel : mêmes arbres,
compte, politique, unité, turn et sceau ; seul `local_transport` peut changer la
délégation. L'inventaire des fichiers précédents provient de l'état réel. Les
arbres de stage restent disponibles pour le préflight. NEW et OLD sont deux
identifiants distincts, jamais un renommage ou une édition de l'ancien stage.

La première commande conserve `continuation.json` et produit la **nouvelle**
question à signer, préfixée `Approve this exact bootstrap continuation and
replacement delegation:`. Elle lie le digest du nouveau bundle, l'opération
précédente, sa décision, l'empreinte exacte de son journal et le digest du code
administratif enrôlé. Le bundle complet reste joint à la demande pour relecture.
La demande emploie les options bootstrap exactes et `--supersedes OLD_DECISION
--supersedes-option install`. La question bootstrap ordinaire de NEW ne vaut
pas autorisation de continuation.

L'ancien journal doit contenir les six premiers reçus natifs complets, avec
éventuellement celui de validation ; il doit rester incomplet, sans intent
d'activation. Un intent `validate` est permis. La chaîne append-only des gestes
relie l'opération à la décision précédente ; la question exacte et la preuve
signée historique de celle-ci sont aussi vérifiées, sans exiger une fenêtre
ancienne encore active. Une entrée de démarrage refusée ne vaut pas cette
preuve. Plusieurs décisions signées pour la même ancienne opération demandent
une réconciliation explicite : remplacer une seule laisserait une autre
autorité d'activation possible. Avant chaque étape, la continuation
vérifie que l'ancienne décision nomme la nouvelle comme remplaçante et que la
nouvelle nomme l'ancienne, avec réponse `install` signée et preuve encore valide.
Une demande en attente, une seule moitié du lien ou un ancien reçu modifié refuse.

Sous le verrou permanent et le verrou de réservation de l'hôte, la réservation
passe de OLD à NEW. Une réponse perdue après ce transfert se reprend sur NEW.
Les anciens journaux et reçus ne sont ni déplacés ni réécrits. Les nouveaux
reçus constatent les arbres immuables et le compte effectifs, rapprochés des
intents natifs précédents ; ils ne republient pas les arbres et ne recréent pas
le compte. Les fichiers de configuration ont de nouveaux snapshots dans NEW.
La validation et l'activation des deux clés restent les dernières étapes,
avec la readiness réelle. Chaque étape conserve intent et résultat dans le
journal des gestes. Une dérive héritée est contrôlée à chaque reprise et avant
validation/activation, même si le nouveau journal possède déjà ses reçus.

## Ré-enrôlement après arrêt

Une nouvelle opération d'enrôlement peut ajouter `previous_operation` à son
`input.json`. Elle conserve une fenêtre fraîche et son propre stage, sa nouvelle
clé générée sous mandat et ses sept documents. `prepare` exige le reçu terminal
`stopped` de l'opération précédente, sans intent restant, lié à son bundle et à
son InvocationID enregistré ; le service doit être arrêté et la file vide.
Tous les documents installés doivent encore correspondre aux empreintes du
bundle précédent. Un service seulement inactif, sans reçu d'arrêt signé, ne
suffit pas.

Le nouveau mandat d'enrôlement lie aussi l'ancienne opération, son bundle, son
reçu d'arrêt, son instance et les empreintes des documents remplacés. La
publication atomique remplace uniquement ces documents exacts, avec intent
préalable ; une réponse perdue reprend ce même intent. Un fichier divergent ou
disparu refuse. Les anciens stages, privées et reçus restent en place : la
révocation des anciennes clés sur les destinations demeure une opération de
délégation séparée. L'opération ne réécrit ni les arbres ni le verrou permanent.

Le nouveau démarrage doit produire un InvocationID différent de celui arrêté.
L'enrôleur enregistre l'identité observée immédiatement après l'appel de start,
y compris si sa réponse se perd. Sans identité enregistrée à cet instant, une
reprise ne peut pas adopter une instance active apparue ensuite ; l'intent
reste incertain et nécessite un mandat de réconciliation. Une identité déjà
observée doit rester identique pendant la readiness et les reprises.

## Identités PostgreSQL séparées (#5162)

Le dossier privé d'identités est indépendant du stage d'enrôlement :
`/etc/ameesh-release-identities`, root 0700. Le bureau y dépose `input.json`
root 0600, régulier, non lié, avec les champs exacts suivants :
`version: 1`, `operation`, `schema`, `source_owner_dsn`, `restore_owner_dsn`,
`controller_role`, `backup_role`, `restore_role`, `group_role`. Les noms SQL
sont des identifiants minuscules distincts, sans préfixe `pg_`. Les deux DSN
administrateurs sont des références privées explicites avec host, dbname,
user et port pour la source ; le socket de restauration utilise le port par
défaut, sans champ port ni hostaddr dans son DSN ; aucun profil ambiant, service libpq ou `options` n'est admis.
Aucun DSN ne se transmet sur la ligne de commande. Les DSN de logins produits exigent
`require_auth=scram-sha-256` : aucune négociation password/MD5/trust n'est
acceptée. Les connexions administratives munies d'un mot de passe ont la même
exigence ; l'administration locale peer utilise le socket sans mot de passe.
La restauration utilise **uniquement** `/run/ameesh-release-restore`, jamais
le port TCP du banc des agents. La politique est conservée dans le DSN transmis
à pg_restore. Voir le [contrat require_auth de libpq 16](https://www.postgresql.org/docs/16/libpq-connect.html#LIBPQ-CONNECT-REQUIRE-AUTH).


Après livraison par paquet et staging, installer les fichiers wheel
`share/ameesh/postgres/enroler-identites.py` et
`share/ameesh/sql/role-release-controller.sql` depuis le même SHA relu,
sous `/opt/ameesh-admin/deploy`, racine root non modifiable par les agents.
Avec le Python root isolé
où psycopg est installé, la commande est :

```sh
/opt/ameesh-release/runtime/bin/python -I -B /opt/ameesh-admin/deploy/postgres/enroler-identites.py enroll
```

L'installation root doit conserver le voisin
`/opt/ameesh-admin/deploy/sql/role-release-controller.sql`. La commande prend un
verrou privé exclusif, génère les secrets localement et écrit durablement
`credentials.json` root 0600 **avant** tout CREATE ROLE. Le fichier contient le
DSN du contrôleur (`database`, document directement compatible database.json),
`backup_dsn`, `restore_dsn`, les mots de passe et l'empreinte de l'entrée.
Son contenu reste privé ; seule son empreinte est publiée. L'inventaire privé
note cette origine lors de la préparation des sept JSON de #4989.

Le groupe NOLOGIN est appliqué par le contrat SQL existant, sans rôle parent.
Le contrôleur LOGIN n'hérite que de ce groupe ; la sauvegarde reçoit USAGE des
schémas et SELECT des relations/séquences de la base source, aucune écriture.
L'audit refuse les privilèges supplémentaires, colonnes comprises, les rôles
parents imprévus, les propriétés administratives, la propriété d'objets,
les fonctions SECURITY DEFINER accessibles et les tables sous RLS que l'identité
ne peut sauvegarder complètement. Les droits PUBLIC comptent dans cet audit. Seul le SELECT public d'un objet
membre d'une extension nommée dans la liste explicite du code est admis en plus
pour le contrôleur (uniquement `pg_stat_statements` aujourd'hui), sans grant SELECT direct au login ou à son
groupe. Cette exception ne dispense d'aucun contrôle d'écriture, de propriété
ou de fonction SECURITY DEFINER. Un refus `IdentityError` expose sa raison
locale fixe ; les erreurs du serveur et les DSN restent masqués.
La commande ne répare pas les droits PUBLIC d'une base partagée : un refus
exige une correction explicite sous son propre mandat. Réappliquer après toute
migration qui ajoute des relations à sauvegarder.

La cible de restauration est un **cluster PostgreSQL dédié jetable sur l'hôte
du contrôleur**, distinct du cluster vif. L'outil mesure les deux
`system_identifier` et refuse leur égalité, même avec des noms de base ou des
adresses différents. Le consommateur crée une base aléatoire puis la supprime :
son identité nécessite CREATEDB, droit de cluster ; elle reste NOSUPERUSER,
NOCREATEROLE, NOREPLICATION, NOBYPASSRLS et sans rôle parent. Aucun login de
restauration n'est créé sur la source. Le cluster de vérification ne doit
héberger aucune autre charge. Topologie #5165 (décision mesh-design du 31/10, #176428) : initdb/pg_ctl directs, version
choisie au moins égale au serveur source, données sous
`/var/lib/ameesh-release-restore/<major>/data`, configuration root
`/etc/ameesh-release-restore`, socket privé `/run/ameesh-release-restore`
(0700), propriétaire OS `ameesh-restore` nologin ; unité
`ameesh-release-restore.service`. Le cluster n'écoute sur aucun port TCP
(`listen_addresses = ''`) et le DSN du socket n'indique aucun port. Le producteur root tourne toutes les 60 s
et écrit au chemin exact `backup.json.restore_capacity`. Pour cette commande
d'identités root, l'administration peer doit autoriser explicitement root vers
le rôle administrateur par le seul socket privé ; un accès réservé à l'OS
`ameesh-restore` ne suffit pas et ne doit pas être promu implicitement.
Le nom SQL administrateur est explicite dans `restore_owner_dsn` et doit être
superutilisateur. Le provisionneur écrit exactement ces trois lignes pg_hba
(décision mesh-design du 31/10, #176452) :

```text
local all ameesh-restore peer
local all <rôle administrateur> peer map=<carte root>
local all <rôle de restauration> scram-sha-256
```

pg_ident contient une seule correspondance explicite :
`<carte root> root <rôle administrateur>`, sans regex ni autre entrée.
Les noms du rôle administrateur et de la carte sont ceux du provisionnement.
Toutes les bases sont nécessaires aux restaurations dans des bases aléatoires.
Aucune ligne host, hostssl ou hostnossl, ni autre ligne local, n'est configurée.
Le rôle de restauration vient de l'entrée privée.
Son provisionnement et le producteur de
`restore_capacity` relèvent du lot compagnon #5165 : la mesure doit être faite
sur le volume PostgreSQL cible ; les octets libres d'agents-3 ne prouvent pas
ceux d'un serveur distant. Le chemin d'attestation effectif et le mapping peer administratif restent
à relever dans le reçu du provisionnement avant la préparation de backup.json.

Les mêmes invocations avec `audit` contrôlent les droits effectifs sans les
modifier ; avec `leave`, elles retirent les trois logins et leurs seuls droits
attendus. La commande refuse les droits supplémentaires et toute propriété
inattendue, sans DROP OWNED ni suppression de données. Le groupe NOLOGIN partagé
reste en place. Les credentials et le reçu privé `retired.json` restent comme
preuves ; une opération retirée ne se réactive pas implicitement. Une nouvelle
opération utilise une nouvelle source privée et de nouveaux noms.
