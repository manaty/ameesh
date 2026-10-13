# Image `ameesh-approve` : publication, épinglage, retour arrière

Le service d'approbation d'une équipe (`ameesh-approve serve`, profil
[`docs/profils/heberger-ameesh-approve.md`](profils/heberger-ameesh-approve.md))
se distribue aussi sous forme d'image de conteneur. Ce document est la
politique de publication et de mise à jour ; il fait foi pour les
consommateurs (relais, hôtes de page, déploiements d'équipe).

Origine : lot **#953** (tranche R5-IMAGE-APPROVE, découpage #547), arbitrage
du pilote mesh-design du 13/10/2026, corrigé le même jour (consigne du
bureau) : **aucune publication avant la fusion**. Le lot **#608**
(R5-IMAGE-ROUTE) attend le digest publié ici comme preuve de disponibilité.

| Élément | Valeur |
|---|---|
| registre | `ghcr.io/manaty/ameesh-approve` |
| construction | `deploy/approve-image/Containerfile` |
| publication | `deploy/approve-image/publish.sh`, appelé par `.github/workflows/image-approve.yml` |
| entrée | `ameesh-approve serve` (tini en PID 1) |
| état persistant | `/var/lib/ameesh-approve` (volume) |
| configuration | `/etc/ameesh-approve/config.json` (lecture seule, `0600`) |
| sonde | `GET /health`, sans jeton |

## 1. Qui publie, avec quel droit

La publication passe par GitHub Actions, sur un runner **auto-hébergé**
(`self-hosted, linux, ameesh-ci`) : aucun runner hébergé payant n'est utilisé
(Processus de lot §3). Un job de fourche est ignoré (`github.repository`).

Le workflow n'a **aucun déclencheur de branche ni de PR** : il ne s'exécute que
par `workflow_dispatch`, et sa première étape refuse toute ref autre que
`main`. GitHub n'expose `workflow_dispatch` qu'une fois le fichier sur la
branche par défaut : la publication ne peut donc commencer qu'**après la
fusion**, avec le feu du pilote. La preuve **avant** fusion est le build local
du `Containerfile`, les tests et la CI ; aucun tag n'est publié depuis une
branche de lot ou une PR.

Le workflow n'utilise que le `GITHUB_TOKEN` du dépôt, avec la seule
permission `packages: write` (`contents: read` par ailleurs). **Aucun jeton
personnel** n'est employé ni stocké.

Responsables :

* **pilote ameesh** (mesh-design) : donne le feu de publication après la
  fusion et arbitre la visibilité du paquet ;
* **auteur du lot** : suit la preuve (run, digest) et la reporte au lot ;
* **owner** : requis seulement pour changer la visibilité du paquet ou
  publier depuis un autre canal.

## 2. Tags : deux formes, jamais mobiles

| Tag | Forme | Quand | Immuable |
|---|---|---|---|
| révision | `sha-<12>` (12 premiers caractères hex du commit) | `workflow_dispatch` sur `main`, après fusion, avec le feu du pilote | oui |
| version | `X.Y.Z` (celle de `pyproject.toml`) | même dispatch, promu depuis le manifeste du tag `sha-<12>` | oui |

* Aucun tag `latest`, aucun tag mobile : un consommateur qui suit un tag
  mobile n'est pas protégé.
* Un tag déjà publié **n'est jamais réécrit** : le workflow rend le digest
  existant. Si le tag de version pointe sur un autre digest, le run échoue
  **sans toucher à la version** et demande d'incrémenter celle-ci.
* **Une absence ne se conclut que sur un 404 du registre.** Toute autre
  réponse (réseau, 401/403, 5xx) arrête le run avant la moindre écriture : un
  lookup transitoirement en échec ne peut pas faire écraser un tag existant.
  Le tag `sha-<12>` n'est poussé que si son absence est prouvée.
* **Les publications sont sérialisées** par un groupe de concurrence unique à
  l'image (`image-approve-publish`), et non par `github.ref` : deux exécutions
  ne peuvent pas se croiser sur les mêmes tags.
* **Le tag de version n'est jamais poussé depuis l'image locale.** Il est créé
  en recopiant le manifeste distant du tag `sha-<12>` (mêmes octets, donc même
  digest), après lecture et vérification de ce digest. Si le tag `sha-<12>`
  existe déjà avec le digest D1 et qu'une reconstruction locale donnerait D2,
  c'est D1 qui devient la version : la base d'image mouvante (§7) ne peut pas
  déplacer une version.

## 3. Preuve de publication

Chaque publication écrit, dans le résumé du run et dans le journal :

```
image=ghcr.io/manaty/ameesh-approve
revision=<sha 40>
tags=sha-<12>[, X.Y.Z]
digest=sha256:<64 hex>
```

Le run vérifie à la fin que le registre **sert bien ce digest** (lecture du
manifeste par digest, avec reprise bornée). L'auteur du lot reporte ensuite le
digest et le lien du run sur le fil de **#953** ; c'est cette ligne qui vaut
preuve pour #608 — après la fusion et le feu du pilote, puisqu'aucune
publication n'a lieu avant.

## 4. Consommer l'image

Un déploiement épingle le **digest**, jamais un tag :

```yaml
image: ghcr.io/manaty/ameesh-approve@sha256:<64 hex>
```

Le conteneur attend, comme le service installé depuis les sources :

* `AMEESH_DSN` (base de l'équipe, rôle `deploy/sql/role-approve.sql`) ;
* un fichier de configuration monté en lecture seule
  (`AMEESH_APPROVE_CONFIG`, `0600`, propriétaire du service) ;
* le jeton de service (`AMEESH_APPROVE_TOKEN_FILE`) et, si TLS local, le
  certificat et la clé (`AMEESH_APPROVE_TLS_CERT`/`_KEY`), privés ;
* un volume persistant sur `/var/lib/ameesh-approve`.

Le service écoute sur la boucle locale (`127.0.0.1:8765`) : le mandataire
inverse doit partager l'espace de noms réseau du conteneur (même pod, ou
réseau hôte). La sonde est `GET /health`.

## 5. Retour arrière

Le retour arrière est un **ré-épinglage** sur le digest précédent, puis un
redémarrage du conteneur. Il ne touche ni la base ni
`/var/lib/ameesh-approve` : passkeys, demandes, reçus et propositions
survivent. Un digest publié reste disponible sur le registre ; on ne
supprime pas un digest tant qu'un déploiement peut y revenir.

## 6. Visibilité du paquet

Le paquet est **privé** par défaut sur ghcr. Si les hôtes de page relais ne
peuvent pas s'authentifier au registre, le passage en public (ou
l'installation d'un identifiant de tirage) se décide au branchement de #608 :
c'est un suivi, pas un blocage de #953.

## 7. Limites connues

* **Une seule architecture** : l'image est construite pour l'architecture du
  runner (`linux/amd64`) ; une image multi-architecture ouvrira un lot dédié
  si un hôte d'une autre architecture doit la tirer.
* **Image de base mouvante** : `python:3.12-slim-bookworm` n'est pas épinglée
  par digest ; deux constructions du même commit peuvent différer. Le digest
  publié est la référence, et un tag déjà publié n'est ni reconstruit ni
  déplacé : le tag de version est une promotion du manifeste distant (§2), pas
  une reconstruction locale.
* L'image ne contient **aucun secret** : ni DSN, ni jeton, ni clé TLS.
