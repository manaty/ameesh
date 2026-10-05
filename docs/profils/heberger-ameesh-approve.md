# Héberger ameesh-approve pour une équipe

Ce guide d'exploitation décrit l'installation d'**une** instance
d'ameesh-approve pour **une** équipe. Il applique la décision
[0026](../design/decisions/0026-hebergement-d-ameesh-approve-par-equipe.md)
et le [contrat d'hébergement](../design/ameesh-approve-hebergement-equipes.md)
(lot L27). Les noms d'hôtes ci-dessous sont des exemples (`.example`) : chaque
équipe a les siens.

Règle de base : **une équipe = un hôte `H` = un RP ID WebAuthn égal à `H`**.
Il n'y a jamais de page commune à plusieurs équipes, jamais de RP ID parent
commun, jamais de partage d'hôte par chemin.

---

## 1. Ce qu'il faut avant d'installer

| Élément | Exigence |
|---|---|
| hôte `H` | réservé durablement à l'équipe, choisi **avant** tout enrôlement (le changer impose de réenrôler les passkeys) |
| base | une base ou un **schéma par équipe** (`AMEESH_DSN`, `AMEESH_SCHEMA`) ; le service n'a pas de filtre d'équipe |
| utilisateur Unix | dédié au service, distinct de celui des agents |
| rôle Postgres | `deploy/sql/role-approve.sql` (§6) : lecture seule de ce que lit le service |
| HTTPS sur `H` | par un mandataire inverse, ou par le TLS local du service (§4) |

## 2. Configuration du service (profil strict)

`~/.config/ameesh-approve/config.json` de l'utilisateur du service :

```json
{
  "rp_id": "equipe-a.pages.example",
  "origins": ["https://equipe-a.pages.example"],
  "public_url": "https://equipe-a.pages.example",
  "reserved_zones": ["pages.example"],
  "reserved_hosts": ["approve.pages.example"],
  "bind": "127.0.0.1",
  "port": 8765
}
```

Le **profil strict** est le défaut (`"profile": "strict"`). Au démarrage, le
service refuse, avec un message qui dit pourquoi :

* un RP ID différent de l'hôte de l'origine (par exemple un parent) ;
* plus d'une origine, une origine avec un port, en `http`, ou sur `localhost`
  ou une adresse IP ;
* une `public_url` sur un autre hôte ou avec un préfixe de chemin ;
* un RP ID égal à une **zone réservée** (`reserved_zones` : la zone sous
  laquelle des hôtes d'équipe sont attribués) ou à un **hôte réservé**
  (`reserved_hosts`), ou parent de l'un d'eux. Les noms réservés par la
  décision 0026 le sont toujours, même absents du fichier.

Le profil **compatible** (`"profile": "compatible"` ou
`--profile compatible`) n'existe que pour les essais locaux
(`http://localhost`, plusieurs origines, sous-domaines du RP ID). Il doit être
écrit explicitement ; les noms réservés y restent refusés.

Les mêmes clés existent en variables `AMEESH_APPROVE_*` (`RP_ID`, `ORIGINS`,
`PUBLIC_URL`, `PROFILE`, `RESERVED_ZONES`, `RESERVED_HOSTS`, `TLS_CERT`,
`TLS_KEY`…) et, pour les principales, en options de `ameesh-approve serve`.

## 3. Deux adresses : `public_url` et `approve_url`

* `public_url` (service) : l'adresse des **pages humaines** (`/a/…`,
  `/enroll/…`), toujours `https://H`.
* `approve_url` (client ameesh, `AMEESH_APPROVE_URL`) : l'adresse **machine**
  de l'API (`POST /requests`, `GET /receipts/<id>`, `GET /health`), avec le
  jeton de service lu dans `AMEESH_APPROVE_TOKEN_FILE` (fichier `0600`).

Aucune des deux ne se déduit de l'autre. Par défaut, l'API n'est servie que
sous l'hôte local (`127.0.0.1:PORT`) : un appel sous l'hôte public reçoit
`421`, et le client le dit. Pour que des exécuteurs d'autres machines
appellent l'API par `https://H`, il faut l'**ouvrir explicitement** dans le
JSON du service :

```json
{ "api_via_public": true }
```

C'est un booléen JSON (`true`/`false`, jamais une chaîne), sans équivalent en
variable d'environnement ni en option : ouvrir l'API est un choix écrit. L'API
reste authentifiée par le jeton et refuse tout navigateur (`Origin`). Un accès
privé (réseau privé, tunnel) est une alternative.

Modifier `approve_url` ne change **pas** la confiance : seuls RP ID et origines
la règlent (§5).

## 4. Exposer `H` en HTTPS

### 4.1 Derrière un mandataire inverse (défaut)

Le service parle HTTP sur la boucle locale ; le mandataire termine TLS pour
`H`, transmet `Host` tel quel, ne met en cache ni ne journalise jetons, liens
ou corps. Sans option, c'est le comportement historique.

### 4.2 Passthrough TLS : le certificat vit sur l'appareil

Quand la passerelle route par SNI **sans déchiffrer** vers un tunnel sortant
de l'appareil, c'est le service qui termine TLS sur la boucle locale :

```
ameesh-approve serve --tls-cert ~/.config/ameesh-approve/tls/H.crt \
                     --tls-key  ~/.config/ameesh-approve/tls/H.key
```

(ou `"tls_cert"`/`"tls_key"` dans le JSON). Le service **ne parle à aucune
autorité de certification** : l'obtention et le renouvellement du certificat
de `H` (par exemple ACME DNS-01 piloté par la plateforme d'hébergement) se
font ailleurs ; ce processus **dépose** les fichiers :

* dans un dossier privé de l'utilisateur du service (proposé :
  `~/.config/ameesh-approve/tls/`, `0700`) ;
* deux fichiers PEM **réguliers** (pas de lien symbolique), appartenant à
  l'utilisateur du service, en `0600` (ou `0400`) — le service refuse tout
  fichier lisible par le groupe ou les autres ;
* par **renommage atomique** (`écrire H.key.tmp`, `rename` ; puis le
  certificat) : à chaque nouvelle connexion, le service compare l'identité des
  fichiers à celle du certificat chargé et le relit s'ils ont changé, sans
  redémarrage. `kill -HUP` force une relecture. Un dépôt refusé (permissions,
  clé et certificat qui ne correspondent pas) laisse l'ancien certificat en
  service et le journal l'indique.
* chaque fichier est ouvert **une seule fois**, sans suivre de lien, contrôlé
  sur son descripteur puis lu ; ce sont ces octets qui sont chargés (copie
  éphémère `0600` dans un sous-dossier `0700` de l'état privé, effacée
  aussitôt) : remplacer un fichier pendant la relecture ne peut pas faire
  servir une autre paire.

Le certificat est **celui de `H` seulement** : jamais un certificat
générique (wildcard) de la zone sur un appareil, jamais de RP ID égal à la
zone.

Avec le TLS local, l'exécuteur installé sur le même appareil continue de passer
par la boucle locale, sans ouvrir l'API publique :

```
AMEESH_APPROVE_URL=https://127.0.0.1:8765
AMEESH_APPROVE_TLS_NAME=H        # le certificat est vérifié pour ce nom
```

## 5. Politique des vérificateurs et contrôle de concordance

ameesh vérifie et consomme les reçus avec **sa** politique, lue dans
`AMEESH_APPROVE_RP_ID` et `AMEESH_APPROVE_ORIGINS` (indépendamment du JSON du
service). Elle doit être identique sur le service et sur **tous** les postes
qui vérifient :

```
AMEESH_APPROVE_RP_ID=equipe-a.pages.example
AMEESH_APPROVE_ORIGINS=https://equipe-a.pages.example
```

Sur chaque poste vérificateur :

```
ameesh approve-check            # 0 concordance, 1 écart (listé), 2 injoignable
ameesh approve-check --json
```

La commande lit `GET /health` du service (sans jeton : le document publie RP
ID, origines, URL publique, profil, niveau, ouverture de l'API, TLS — aucun
secret) à `approve_url` (ou `--url`), et la compare à la politique locale. À
lancer après l'installation, après toute modification de configuration et
après chaque bascule. `/health` suit les règles de `Host` de l'API.

## 6. Droits en base

Le service lit, dans le schéma de l'équipe, les actions (pour recalculer
l'empreinte et rendre le résumé), le registre des authentificateurs et les
nonces déjà consommés ; il n'écrit jamais en base (ses demandes, liens, reçus
et propositions vivent dans son état privé).

```
PGOPTIONS='-c search_path=<schéma de l équipe>' \
psql "<DSN d'administration>" -v ON_ERROR_STOP=1 -v role=approve_equipe_a \
     -f deploy/sql/role-approve.sql

CREATE ROLE approve_equipe_a_service LOGIN IN ROLE approve_equipe_a;
ALTER ROLE approve_equipe_a_service SET default_transaction_read_only = on;
ALTER ROLE approve_equipe_a_service SET search_path = <schéma de l équipe>;
```

Le script est transactionnel, idempotent et audité comme les rôles
superviseur : il refuse (et n'applique rien) si le rôle obtiendrait d'ailleurs
plus que son contrat (droits donnés à `PUBLIC`, rôle parent, ACL par défaut,
appartenance), y compris dans un **autre schéma** (celui d'une autre équipe),
sur une base (CREATE, ou droit accordé au rôle lui-même : la connexion est
donnée au rôle de connexion) ou sur un grand objet. Un rôle par équipe, chacun
sur son schéma. Le DSN du service
(`AMEESH_DSN`) utilise le rôle de connexion.

## 7. Enrôlement

`H` fixé, configuration vérifiée (`approve-check`), puis, sous l'utilisateur
du service :

```
ameesh-approve enroll-link --approver human:<id>
```

L'humain ouvre le lien **sur son appareil** et crée sa passkey ; le service
écrit une **proposition** de canon (jamais une clé active en base), relue par
PR puis synchronisée (`ameesh canon sync`).

## 8. Changer d'emplacement, changer d'hôte

**Même `H`** (même RP ID, même origine, même registre) : les passkeys sont
conservées.

1. Préparer la cible : même base/schéma et même canon, configuration RP/origine
   identique, certificat de `H` présent et valide.
2. Suspendre les nouvelles demandes ; laisser expirer ou aboutir les liens
   ouverts ; arrêter l'ancienne instance (une seule instance active).
3. Copier l'état privé (`state_dir`, propositions non intégrées) en gardant ses
   permissions (`0700`/`0600`) ; ne jamais restaurer une sauvegarde plus
   ancienne de l'état ni des nonces.
4. Basculer le routage de `H` ; changer seulement `approve_url` si le point
   d'entrée machine change ; distribuer un nouveau jeton et révoquer l'ancien
   si besoin.
5. `ameesh approve-check` sur chaque vérificateur ; une approbation avec une
   passkey déjà enrôlée ; vérifier qu'un ancien lien consommé reste refusé.

**Nouvel hôte** (`H` change) : ce n'est pas une migration. Nouvelle
configuration (RP ID et origine du nouvel hôte), **nouvel enrôlement** de
chaque humain, nouvelles propositions revues, politique des vérificateurs mise
à jour, puis `approve-check`. Ne jamais élargir le RP ID à un parent pour
recycler les anciennes passkeys.

## 9. Liste de contrôle

- [ ] `ameesh-approve serve` démarre en profil strict, sans avertissement de
      configuration ;
- [ ] `ameesh approve-check` rend 0 sur chaque poste vérificateur ;
- [ ] l'API n'est ouverte sous `H` que si `api_via_public` est écrit, et le
      jeton est un fichier `0600` de l'utilisateur concerné ;
- [ ] en passthrough : certificat de `H` seul, fichiers `0600` de
      l'utilisateur du service, renouvellement par renommage atomique ;
- [ ] rôle Postgres de l'équipe appliqué sans écart ; DSN du service sur son
      rôle de connexion ;
- [ ] aucune autre application ne sert de contenu sur `H` ni sous `H`.
