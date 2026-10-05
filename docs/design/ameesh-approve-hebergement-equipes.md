---
type: Specification
title: "ameesh-approve : trois hébergements, une identité WebAuthn par équipe"
status: stable
author: codex3
date: 2026-10-05
source: "décision produit du propriétaire (2026-10-05 06:22), voir la décision 0026 ; rédigé par codex3, intégré par mesh-design"
---

# Portée et décision rapportée

Une équipe choisit où tourne son service d'approbation humaine : sur son PC
avec relais Nexlink et petit abonnement, sur une page Nexlink dédiée, ou sur
son propre serveur. Ces choix concernent **ameesh-approve** ; le placement des
exécuteurs et de leur base reste une configuration distincte
([0011](decisions/0011-hebergement-configurable.md)). Le montant et les
conditions de l'abonnement ne sont pas spécifiés ici.

Ce document précise le contrat à réaliser. Les primitives existantes sont
vérifiées sur `private/main` 148bd01 ; le provisionnement des trois offres,
l'allocation des domaines et la bascule automatique ne sont pas implémentés
par cette spécification. La décision [0017](decisions/0017-approbation-via-page-nexlink.md)
décrit le choix initial du chantier sur `ameesh.nexlink.ph`, remplacé par la
décision rapportée à 06:30 : notre installation utilise
`manaty.ameesh.nexlink.ph`. `ameesh.nexlink.ph` est réservé : ni RP ID ni contenu,
comme tout apex de zone d'approbation. [0025](decisions/0025-une-session-par-lot.md)
concerne les sessions d'agents et ne règle pas cet hébergement.

# Contrat par équipe

Décision Q1 rapportée à 06:38 : l'équipe est une entité ameesh autonome,
avec identité stable propre et **lien optionnel** vers une page Nexlink.
Une installation indépendante, notamment mode 3, ne nécessite pas de page,
de compte ni d'abonnement Nexlink. L'identité d'équipe n'est donc pas un
`page_id` obligatoire ; l'association de page relève du connecteur Nexlink,
conformément à [0003](decisions/0003-ameesh-et-nexlink.md).

Pour démarrer avec Nexlink, rattacher l'équipe à une page est le parcours
simple : nom par défaut `<handle>.<zone>`, approbateurs humains = admins de
la page, abonnement de la page. Le handle choisit le nom **initial** ; un
renommage de page ne change pas silencieusement l'hôte ni le RP ID déjà enrôlé.
L'association authentifiée page/équipe doit être explicite et stable. Le
traitement d'une page déjà associée à une autre équipe reste à spécifier ;
aucune fusion de registres de confiance automatique.

Réciproquement, **toute nouvelle page Nexlink vient avec ameesh installé**.
Le provisionnement à la création prépare l'équipe, le canon, le stockage
isolé, la configuration et le runtime de ses processus métier ; en mode 2,
ils sont provisionnés sur le serveur de page avec approve séparé des agents.
Installation ne signifie ni enrôlement d'un humain à sa place, ni achat
implicite d'un hébergement payant, ni publication d'approve avant sa recette.
L'enrôlement reste la cérémonie humaine ; une page peut être installée avec
approbation en attente, sans exécuter les actions exigeant un reçu. Le sort
des pages existantes nécessite un lot de reprise idempotent, pas un effet
implicite de la création d'une nouvelle page.

Les admins sont la source des droits humains du connecteur : leur identité
et leurs authentificateurs sont reliés au canon, sans faire d'un agent un
approbateur humain. Admission, retrait et changement d'admin doivent être
propagés avec provenance vérifiable ; un retrait interdit les nouvelles
approbations et l'exécution de reçus qui n'ont plus l'autorité requise. Les
délais et contrôles aux étapes demande/signature/exécution sont à spécifier
dans ce pont, avant de remplacer les contrôles du canon par une projection.

Une instance sert les projets d'une équipe, ses humains autorisés et ses
exécuteurs, éventuellement sur plusieurs PC. Plusieurs équipes sur une même
machine conservent des instances, identités système, secrets, états et accès
aux données séparés. Aucun routage d'équipe ne dépend d'un `team_id` fourni
librement par un client, d'un chemin ou du seul `action_id`.

Le service lit lui-même les actions et le registre de confiance dans la base
ameesh de l'équipe (`AMEESH_DSN`, `AMEESH_SCHEMA`). Cette base peut être distante.
Il affiche son résumé, recueille approbation/refus et produit un reçu lié à
l'action, avec échéance et nonce. Il écrit ses demandes, liens, reçus et
propositions d'enrôlement dans son état privé ; il ne modifie pas les actions
et ne consomme pas leur nonce. L'exécuteur récupère et vérifie le reçu avant
de l'utiliser sous la porte des actions.

Le code actuel n'a pas de filtre d'équipe dans `DbActionSource` : **une base
ou un schéma isolé par équipe et un accès DB limité à ce périmètre sont requis**.
Un simple filtre de projet côté interface ne constitue pas cette isolation.
Le canon de confiance et ses identités humaines doivent correspondre au même
périmètre ; une fédération partagée demande un contrat d'autorisation distinct.
Le rôle superviseur L25 n'est pas le rôle d'approve : il exclut les contenus
nécessaires pour afficher une action. Les droits DB propres à approve restent
à définir et à relire avant déploiement.

Chaque équipe reçoit un hôte canonique `H`, réservé durablement au service :

| Paramètre | Valeur requise |
|---|---|
| `rp_id` / `AMEESH_APPROVE_RP_ID` | `H`, sans schéma, port ni chemin |
| `origins` / `AMEESH_APPROVE_ORIGINS` | exactement `https://H` en exploitation |
| `public_url` | `https://H`, pour les liens humains |
| `approve_url` / `AMEESH_APPROVE_URL` | URL de l'API de cette instance : boucle locale ou HTTPS |
| `approve_token_file` / `AMEESH_APPROVE_TOKEN_FILE` côté client | fichier privé du jeton de demande de cette équipe |
| `token_file`, `state_dir`, `proposals_dir` côté service | fichiers/dossiers privés, utilisateur dédié |

`approve_url` est l'adresse machine de `POST /requests` et `GET /receipts/<id>` ;
`public_url` est l'adresse des pages ouvertes par les humains. Le jeton permet
de demander et récupérer, jamais de signer ; la passkey privée reste dans
l'authentificateur humain. Plusieurs PC d'une équipe peuvent utiliser le
service ; le code actuel utilise un jeton de service unique, pas des jetons
révocables individuellement par PC.

La politique doit être identique dans **le service et tous les vérificateurs** :
`actions.policy_from_env` lit RP ID/origines dans l'environnement, indépendamment
du JSON du service. Le provisionneur configure les deux, puis contrôle leur
concordance ; modifier seulement `approve_url` ne modifie pas la confiance.

Exemple de configuration existante pour une instance hébergée, avec `H`
illustratif à confirmer côté Nexlink :

```json
{
  "rp_id": "equipe-a.ameesh.nexlink.ph",
  "origins": ["https://equipe-a.ameesh.nexlink.ph"],
  "public_url": "https://equipe-a.ameesh.nexlink.ph",
  "bind": "127.0.0.1",
  "port": 8765,
  "api_via_public": true
}
```

Le service reste en boucle locale derrière un mandataire HTTPS, dans les trois
modes. `api_via_public` vaut `false` par défaut ; le mettre à `true` dans le
JSON autorise l'API sous l'hôte public, avec son authentification par jeton.
Ce n'est pas une option d'environnement ou de CLI existante. L'API doit être
joignable par les exécuteurs autorisés ; l'accès peut aussi rester privé.

# Domaines et isolation WebAuthn

RP ID propre à chaque équipe : ni `ameesh.nexlink.ph/<équipe>`, ni RP ID parent
commun `ameesh.nexlink.ph` ou `nexlink.ph`. Les RP ID des équipes sont également
**sans relation ancêtre/descendant**. L'hôte `H` et ses sous-domaines ne servent
aucun contenu ou code contrôlé par une autre équipe ; la page dédiée est le
service d'approbation, sans HTML de contenu Nexlink sur la même origine.

WebAuthn lie les credentials au RP ID, pas au chemin d'URL ; son RP ID reste
fixe pendant la vie du credential. Un suffixe parent valide peut être demandé
depuis un sous-domaine : choisir `H` exactement évite de partager cette portée
avec les équipes voisines. Voir la [définition W3C du RP ID](https://www.w3.org/TR/webauthn-3/#rp-id)
et le [modèle de l'authentificateur](https://www.w3.org/TR/webauthn-3/#sctn-authenticator-data).

La séparation des RP ID ne remplace pas les contrôles d'origine, de `Host` et
des demandes intersites. Le service vérifie déjà l'origine exacte et refuse
`Sec-Fetch-Site: same-site` pour les POST navigateur ; aucune permission
globale de sous-domaines ou de CORS ne doit être ajoutée. Les siblings Nexlink
peuvent rester dans le même site navigateur : l'arbitrage sous-domaines sous
`nexlink.ph` versus domaine d'approbation séparé reste côté Nexlink.
Pas de cookies partagés avec `Domain=nexlink.ph` ; aucune exception de domaine
parent ni configuration « related origins » commune aux équipes.
Le service actuel n'utilise pas de cookies. Si une façade en introduit, ils
doivent être préfixés `__Host-` (Secure, Path=/, sans Domain), avec contrôles
d'origine conservés ; cela ne remplace pas l'analyse same-site/PSL de Nexlink.

# Trois modes, mêmes données et protocole

| Mode | Service et exposition | Appel des exécuteurs |
|---|---|---|
| PC + relais Nexlink | utilisateur dédié sur le PC, boucle locale ; tunnel sortant vers le relais, HTTPS sur `H` ; abonnement Nexlink | PC hôte : `http://127.0.0.1:8765`, API publique fermée ; autres PC : accès privé ou API HTTPS explicitement ouverte et authentifiée |
| Page Nexlink dédiée | instance isolée sur le serveur de page, boucle locale derrière son mandataire, HTTPS sur le même `H` | URL API HTTPS et jeton ; accès du service à la base/registre de l'équipe |
| Serveur de l'équipe | équipe opératrice du service, état et HTTPS ; domaine de l'équipe choisi à l'adoption | config client URL HTTPS et fichier de jeton, sans dépendance obligatoire au relais Nexlink |

En mode serveur propre, « configurer l'URL » décrit le client ameesh ; le
serveur doit déjà disposer du service, de sa base/registre, des secrets et de
TLS. Héberger le service ailleurs ne copie pas automatiquement la base ameesh.

# Enrôlement et changement de mode

Fixer `H` avant l'enrôlement. Un administrateur autorisé lance
`ameesh-approve enroll-link --approver human:<id>` sous l'utilisateur du
service ; l'humain ouvre le lien sur son appareil et crée sa passkey.
L'enrôlement écrit une proposition, jamais une clé directement active en base :
PR du canon relue, puis synchronisation du registre. La première approbation
réelle contrôle équipe, origine, RP ID, empreinte de l'action et consommation
unique du nonce. Le harnais d'installation prépare cette configuration ; les
gestes d'autorité et les dépenses restent humains ([0011](decisions/0011-hebergement-configurable.md)).

Changer **l'emplacement** tout en gardant `H`, HTTPS, RP ID, origine et registre
permet de conserver les passkeys. Ce n'est pas un transfert de clé privée :
les credential ID et clés publiques de l'équipe restent au canon/registre,
les clés privées restent dans les appareils. Conserver seulement le nom d'hôte
sans restaurer le registre ne suffit pas.

Procédure de bascule à fournir par le provisionneur :

1. Préparer la cible isolée, accès à la même base et au même canon, configuration
   RP/origine identique ; vérifier le certificat de `H` avant la bascule.
2. Suspendre les nouvelles demandes et vider les actions en cours. Laisser
   expirer/terminer les liens d'approbation et d'enrôlement, puis arrêter l'ancien
   écrivain. Une équipe ne possède qu'une instance active dans ce profil.
3. Transférer l'état privé durable et les propositions non intégrées avec leurs
   permissions ; conserver la base des nonces consommés, les actions et le
   registre courant. Aucun retour à une sauvegarde antérieure des nonces ou des
   liens consommés, y compris lors d'un retour arrière.
4. Basculer routage/DNS de **`H`**, garder le contrôle du nom ; mettre à jour
   seulement `approve_url` si le point d'entrée machine change. Distribuer un
   nouveau jeton et révoquer l'ancien si nécessaire ; ne pas élargir RP/origines.
5. Vérifier une approbation avec une passkey déjà enrôlée et le refus du rejeu,
   reprendre les demandes ; retour arrière avec état courant et écrivain unique.

Décision du propriétaire rapportée à 06:30 : **le nom Nexlink reste le même
entre les modes 1 et 2 ; il n'est pas conservé en mode 3**. Nexlink ne route
pas vers le serveur propre et ne maintient pas le nom pour l'équipe après
l'abonnement. Une équipe visant le mode 3 utilise son domaine dès l'adoption.
Le changement d'un nom Nexlink vers ce domaine change le RP ID : réenrôlement
assumé. La recette de bascule sans réenrôlement ci-dessus ne s'applique qu'à
un déplacement conservant effectivement le nom, notamment mode 1 ↔ mode 2.
Le maintien d'un tombstone empêchant l'attribution à une autre équipe n'est
pas un droit de continuer à utiliser ou à faire router le nom.

Si `H` change (ex. `equipe-a.nexlink.ph` vers `approve.equipe-a.example`), **ce n'est pas
une migration sans réenrôlement** avec le code actuel : nouvelle cérémonie sur
le nouveau domaine, nouvelles propositions revues et politique mise à jour.
Ne pas remplacer le RP ID parent pour recycler les anciennes passkeys. Le cas
du chantier, s'il a déjà été enrôlé sous `ameesh.nexlink.ph`, suit cette procédure
pour passer à `manaty.ameesh.nexlink.ph` ; ne pas réattribuer l'apex ni y servir
du contenu. Aucun réenrôlement réel ni changement DNS n'est fait par ce document.

# À fournir côté Nexlink

- Identité d'équipe indépendante avec association optionnelle de page,
  provisionnement idempotent à la création d'une page (sur son serveur en
  mode 2), projection authentifiée des admins et de leurs retraits vers
  l'autorité ameesh ; aucune dépendance de ce parcours pour le mode autonome.
- Choix de namespace après analyse same-site ; attribution exclusive de `H`
  à une équipe, hôte conservé entre modes 1/2, domaine propre dès l'adoption
  du mode 3 et règles de non-réattribution ; apex réservé sans RP ID ni contenu.
- Mode PC : mapping `H` → tunnel/port/clé dédiés ; canal sortant, clé limitée
  à cette redirection, arrêt de l'abonnement et PC hors ligne sans contournement.
- Mode page : instance réellement isolée, secret/état/sauvegardes propres,
  accès sécurisé à la base ameesh de l'équipe, séparation des exécutants d'agents.
- HTTPS/certificat sur le même `H` entre modes 1/2 et domaine propre en mode 3, routage exact par hôte,
  conservation de `Host`, contrôle des en-têtes de mandataire, aucun cache ni
  journal des jetons/liens/corps ; exposition API authentifiée ou accès privé.
- Contrat de provisionnement retournant hôte/origine/URL API et moyen privé de
  remise du jeton ; abonnement et dépenses sous approbation humaine. Aucun prix
  ni endpoint de provisionnement nouveau n'est inventé dans cette spec.

Si Nexlink adopte les routes dynamiques de l'étude multi-équipe de Nexlink,
le snapshot doit porter une version, une preuve de complétude et une validité
bornée. Un HTTP200 avec liste syntaxiquement valide mais partielle doit être
refusé ; les associations équipe/hôte ne se réaffectent pas silencieusement.
Les retraits explicites et la rotation de clé doivent être représentables sans
contourner les limites de variation ; une configuration conservée après panne
ne prolonge pas indéfiniment une autorisation échue. Le protocole et ces sondes
sont à fixer avant R1–R3. Une quarantaine de 90 jours n'est pas une garantie de
non-réattribution d'un RP ID déjà utilisé : la réservation durable du nom et
la fin d'abonnement n'accorde aucun maintien du routage vers le serveur propre.

# Cohérence avec les agents de page natifs

L'[architecture Nexlink §3.2i](https://github.com/manaty/nexlink/blob/599863f/docs/architecture/decentralisation.md#32i-page-agents-owner-2026-10-02)
prévoit les agents de modération, écriture, filtrage, mise en valeur et
administration **sur la page**, avec endpoint LLM compatible OpenAI,
permissions bornées, actions expliquées et contestables. Le pont ameesh
reprend ces identités, règles et journaux : il ne crée pas une deuxième
équipe d'agents exécutant les mêmes jobs ni un deuxième budget facturé.
Les contenus et règles restent dans le périmètre de la page, aucune lecture
transverse aux équipes ni accès aux contenus E2E.

Nexlink conserve identité, API de page, droits, règles métier et audit ;
ameesh apporte exécution, sessions, baux, budget et vérification d'autorité
(0003). Le choix act/propose/off des agents natifs et les classes d'action
ameesh doivent être raccordés explicitement, sans élargir l'autonomie ni
imposer arbitrairement un reçu à chaque action déjà autorisée. Un seul
réclamant effectif par job et un identifiant corrélant job/action/tour
doivent empêcher la double exécution. Ce pont est à construire et relire ;
ce document ne remplace ni le worker natif actuel ni son mécanisme de claim.

# Recette et écarts à implémenter

Recette exigée avant offre publique : deux équipes A/B, plusieurs PC pour A ;
une passkey, origine, jeton ou action de B ne vaut jamais pour A. Refuser RP ID
parent, hôte partagé par chemin, contenu non fiable sur `H` ou sous `H`, et
POST navigateur venant d'un sibling. Vérifier les trois modes, DB distante
indisponible = refus, état privé isolé, migration à `H` constant sans enrôlement,
ancien lien consommé non réutilisable et changement d'hôte nécessitant enrôlement.
Ajouter : création de page rejouée sans doublon d'équipe/job, installation
autonome sans page, ajout/retrait d'admin avec révocation effective, rattachement
à la bonne page et job natif exécuté une seule fois avec budget/audit corrélés.

À réaliser : provisionnement et allocation d'équipe, contrôle du profil
RP ID égal à `H` et origine unique (la validation actuelle accepte aussi un
sous-domaine du RP ID), concordance de la politique des vérificateurs, droits DB
approve dédiés, bascule/sauvegarde/arrêt du nom Nexlink. Cette spec n'ajoute aucune
clé inconnue au JSON actuel ; le choix du mode et l'identité d'équipe seront
des paramètres du provisionneur, dont le format reste à spécifier.

Réalisé côté ameesh par le lot L27 (guide d'exploitation :
[héberger ameesh-approve](../profils/heberger-ameesh-approve.md)) : profil
strict par défaut (RP ID égal à l'hôte de l'unique origine `https://H` et de
`public_url`, zones et hôtes réservés refusés avec leurs parents) et profil
« compatible » explicite pour les essais ; `GET /health` et
`ameesh approve-check` pour la concordance de la politique ; `approve_url`
distincte de `public_url`, API publique seulement par `api_via_public` écrit
dans le JSON ; TLS local facultatif pour une passerelle en passthrough
(certificat de `H` déposé sur l'appareil, rechargé à chaud) ;
`deploy/sql/role-approve.sql` ; recette A/B dans
`tests/test_approve_equipes.py`. Restent côté Nexlink ou provisionneur :
allocation des hôtes, ACME, tunnels, bascule outillée. Ce lot ajoute au JSON
les clés `profile`, `reserved_zones`, `reserved_hosts`, `tls_cert`, `tls_key`.
