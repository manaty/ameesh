# Image `ameesh-approve` : publication, épinglage, retour arrière

Le service d'approbation d'une équipe (`ameesh-approve serve`, profil
[`docs/profils/heberger-ameesh-approve.md`](profils/heberger-ameesh-approve.md))
se distribue aussi sous forme d'image de conteneur. Ce document est la
politique de publication et de mise à jour ; il fait foi pour les
consommateurs (relais, hôtes de page, déploiements d'équipe).

Origine : lot **#953** (tranche R5-IMAGE-APPROVE, découpage #547), arbitrage
du pilote mesh-design du 13/10/2026. Le lot **#608** (R5-IMAGE-ROUTE) attend
le digest publié ici comme preuve de disponibilité.

| Élément | Valeur |
|---|---|
| registre | `ghcr.io/manaty/ameesh-approve` |
| construction | `deploy/approve-image/Containerfile` |
| publication | `.github/workflows/image-approve.yml` |
| entrée | `ameesh-approve serve` (tini en PID 1) |
| état persistant | `/var/lib/ameesh-approve` (volume) |
| configuration | `/etc/ameesh-approve/config.json` (lecture seule, `0600`) |
| sonde | `GET /health`, sans jeton |

## 1. Qui publie, avec quel droit

La publication passe par GitHub Actions, sur un runner **auto-hébergé**
(`self-hosted, linux, ameesh-ci`) : aucun runner hébergé payant n'est utilisé
(Processus de lot §3). Un job de fourche est ignoré (`github.repository`).

Le workflow n'utilise que le `GITHUB_TOKEN` du dépôt, avec la seule
permission `packages: write` (`contents: read` par ailleurs). **Aucun jeton
personnel** n'est employé ni stocké.

Responsables :

* **pilote ameesh** (mesh-design) : donne le feu de publication du tag de
  version et arbitre la visibilité du paquet ;
* **auteur du lot** : pousse la branche et suit la preuve (run, digest) ;
* **owner** : requis seulement pour changer la visibilité du paquet ou
  publier depuis un autre canal.

## 2. Tags : deux formes, jamais mobiles

| Tag | Forme | Quand | Immuable |
|---|---|---|---|
| révision | `sha-<12>` (12 premiers caractères hex du commit) | push sur la branche du lot, ou dispatch sur `lot/*` | oui |
| version | `X.Y.Z` (celle de `pyproject.toml`) | `workflow_dispatch` sur `main`, après fusion, avec le feu du pilote | oui |

* Aucun tag `latest`, aucun tag mobile : un consommateur qui suit un tag
  mobile n'est pas protégé.
* Un tag déjà publié **n'est jamais réécrit** : le workflow rend le digest
  existant. Si le tag de version pointe sur un autre digest, le run échoue et
  demande d'incrémenter la version.
* Le tag `sha-<12>` de la tête gelée est la preuve **avant fusion** ; le tag
  de version est publié **après fusion**, sur le commit de fusion.

GitHub ne propose `workflow_dispatch` que si le fichier de workflow existe
déjà sur la branche par défaut. Au premier lot, la preuve pré-fusion vient
donc du `push` sur la branche du lot ; ensuite un dispatch sur `lot/*` est
possible.

## 3. Preuve de publication

Chaque publication écrit, dans le résumé du run et dans le journal :

```
image=ghcr.io/manaty/ameesh-approve
revision=<sha 40>
tags=sha-<12>[, X.Y.Z]
digest=sha256:<64 hex>
```

Le run vérifie à la fin que le registre **sert bien ce digest**
(`docker manifest inspect <image>@<digest>`, avec reprise bornée). L'auteur du
lot reporte ensuite le digest et le lien du run sur le fil de **#953** ; c'est
cette ligne qui vaut preuve pour #608.

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
  publié est la référence, et un tag `sha-<12>` déjà publié n'est pas
  reconstruit.
* L'image ne contient **aucun secret** : ni DSN, ni jeton, ni clé TLS.
