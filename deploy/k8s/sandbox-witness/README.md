# Témoin kind du chemin sandbox — #8246 (TPK #2202 S6a)

Cluster **jetable**. Ce dossier ne contient ni image de production, ni
kubeconfig réel, ni secret : il qualifie le chemin sandbox de bout en bout sur
un cluster `kind` local ou de CI, puis le cluster est supprimé.

## Ce que la preuve exécute

`preuve-sandbox-kind.sh` crée (ou réutilise) un cluster kind, puis lance
`tests/test_8246_sandbox_kind.py` avec `AMEESH_SANDBOX_KIND=1`. Le test :

1. construit un canon git de test (Member, Agent `kind-reviewer`, Host
   `tpk-stg-reviewers`, Placement) avec les blocs `sandbox` ;
2. passe l'admission S2 (`placement.sandbox_admission`) et les réservations S3
   (tranche + enveloppe du compte), puis écrit l'intention `sandbox_turns` ;
3. démarre le superviseur S4 réel dans un processus enfant et le fournisseur
   Kubernetes réel ; un Job réel est créé, avec un jeton de relais lié au tour
   (S4d-3) ;
4. selon le mode, le Job termine immédiatement, attend, ou reste lent ; quand
   il termine, il écrit un résultat `ameesh-sandbox-result/1` sur stdout et son
   digest dans `/dev/termination-log` ;
5. S1 accepte ce résultat **une seule fois** (`ameesh_sandbox_complete`) ; le
   reçu `ameesh-sandbox-receipt/1` est relu par `ameesh sandbox show --json`,
   `GET /api/v1/sandbox/turns/{turn}` et la liste filtrée.

Cas d'acceptation couverts : annulation avant résultat (`revoked` puis
`cancelled`, Job supprimé, jeton invalidé), redémarrage du superviseur avec
adoption du **même** Job (même UID, un seul Job), bail perdu (`stale`, reprise
`lost`/`expired` sans toucher le bail de remplacement), résultat en double
(`duplicate`/`replayed`) et Job supprimé sans observation (`orphaned`, événement
`sandbox.orphan`, alerte `sandbox_orphan` après expiration du bail).

Trois tests sans cluster complètent la preuve : le témoin produit exactement
le canon JCS attendu, ne lit que les deux fichiers du Secret du tour (aucun
import ameesh, aucun DSN, aucune clé de fournisseur) et appelle le relais avec
le jeton du tour sur la bonne route ; la resynchronisation du canon distingue
`revoked`, `changed` (limites comprises) et `same`.

## Lancement local

Prérequis : `kind`, `kubectl`, `docker`, un Python avec les dépendances du
paquet (`psycopg`, `PyYAML`) et une base de test migrée. Sans
`AMEESH_TEST_DSN`, le banc de `tests/support.py` utilise
`postgresql://agent_mesh@127.0.0.1:55432/agent_mesh`.

```sh
# Docker rootful : le Job joint le relais de l'hôte par la passerelle kind.
deploy/k8s/sandbox-witness/preuve-sandbox-kind.sh

# Docker rootless (slirp4netns --disable-host-loopback) : pas de route
# pod -> hôte ; le Job ne fait aucun appel et S3 garde la réservation `kept`.
deploy/k8s/sandbox-witness/preuve-sandbox-kind.sh --no-host-relay
```

Options : `--reuse` (réutilise le cluster nommé), `--keep` (garde cluster et
kubeconfig pour inspection), `--cluster`, `--namespace`, `--image`, `--python`.
Le script sort `77` si un outil manque : la preuve est alors **non concluante**,
pas verte.

En CI, `.github/workflows/sandbox-kind.yml` (déclenchement manuel uniquement)
lance la preuve sur un runner GitHub rootful, avec PostgreSQL 17 en service,
kind v0.27.0 et kubectl 1.32.2 épinglés par empreinte. Le Job y appelle
réellement le relais de l'hôte et S3 règle la réservation (`settled`,
`turn_costs`), ce que le banc local rootless ne peut pas exercer.

## Isolation et secrets

- Le kubeconfig est privé (`0600`) et créé hors des racines partagées
  (`/tmp`, `/var/tmp`, `/dev/shm`) : `RUNNER_TEMP` en CI, `~/.cache` sinon.
- Le Job ne reçoit que le Secret `prompt` + `relay_token`, monté en lecture
  seule sous `/run/ameesh/turn` ; ni DSN, ni clé de fournisseur, ni kubeconfig.
  Le jeton n'apparaît jamais dans l'argv du Job.
- L'image du Job est publique et **épinglée par digest**. Le défaut est
  `gcr.io/distroless/python3-debian12@sha256:7d1042…`, qui tourne non-root :
  c'est requis par le `runAsNonRoot` du fournisseur Kubernetes. Un autre image
  doit être remplacée par `--image` (ou `SANDBOX_WITNESS_IMAGE`) avec la même
  forme `…@sha256:…`, et tourner non-root.
- Le témoin `turn-witness.py` n'est pas du code de production et n'est jamais
  importé par ameesh. Il n'utilise que la bibliothèque standard.
- Aucun canon réel, aucun staging, aucune production n'est contacté. Le
  cluster et la base de test sont jetables.

## Limites de la preuve

- Le banc local rootless ne qualifie pas la route réseau pod → hôte ; il
  qualifie le reste et vérifie explicitement le coût inconnu (`kept`). La CI
  rootful qualifie cette route et le règlement réel.
- Le Job ne exécute pas un vrai relecteur : le résultat est un témoin sans
  contenu de revue. La preuve porte sur le contrat, les transitions d'état, la
  liaison du jeton et l'exactly-once, pas sur la qualité d'une revue.
- Ce n'est ni un déploiement, ni une release : aucun geste de production.
