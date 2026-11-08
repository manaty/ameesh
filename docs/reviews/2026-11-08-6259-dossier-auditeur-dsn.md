# #6259 — Mise en service de la lecture seule de l'auditeur : dossier préparé (bureau, aucun geste fait) — v3

Préparé le 2026-11-08T02:58Z par BG (bureau, famille claude). Source : ameesh v1.44.0 `e256558523b3c354554ed69790db61a17607b65b`.

## 1. État relevé (lecture seule, 08/11 ~03:00Z, connexion `ameesh` sur 10.77.0.1/ameesh)

- PostgreSQL 17.11 ; schéma `public` (propriétaire pg_database_owner) ; rôle `ameesh` : LOGIN, ni SUPERUSER ni CREATEROLE.
- **Préalables ABSENTS** : ni `ameesh_superviseur` ni `ameesh_superviseur_contenus` n'existent ; `auditeur_lecture` absent ; aucune appartenance `ameesh_superviseur%`.
- Aucun accès propriétaire depuis les VM : SSH root/ameesh vers 10.77.0.1 refusé (clé), aucun DSN CREATEROLE sur BG ni AG2. Le geste revient donc à l'**owner** (superuser de la base centrale) ; la délégation du 23/10 ne fournit pas l'accès.
- Exécuteur de l'auditeur : `ameesh-runner-agent@auditeur.service` (utilisateur systemd `ameesh`, AG2), drop-ins existants dans `~ameesh/.config/systemd/user/ameesh-runner-agent@auditeur.service.d/` (AUDITEUR_CI_*, AMEESH_CONFIG critiques) ; **aucun AUDITEUR_DSN**.

## 2. Fichiers à exécuter (v1.44.0, empreintes SHA-256)

| Fichier | SHA-256 |
|---|---|
| deploy/sql/_contrat-superviseur.sql (inclus) | `16c7efd399f3e05a59a9c3cdf9bd31a5e5e88d4677aaeedd63524a42e1e94faa` |
| deploy/sql/role-superviseur.sql | `a5101e02103c891175ef88f9d1510f823c9d6bea95aa05228bd73e6fd695e865` |
| deploy/sql/role-superviseur-contenus.sql | `751cb4c3761b6bcde9cd277bc8b8cc1bbbe85af210acee9ab717e0b27c16739b` |
| deploy/sql/role-auditeur.sql | `3d15cf5513e7ebdd887cd6e859f0d933fa43ffff032b97a5d5dc2622dc29fd14` |

Tous transactionnels et idempotents ; ils échouent et annulent tout si le contrat n'est pas tenu.

## 3. Geste owner (sur l'hôte central, rôle superuser), dans cet ordre — STOP au premier échec

```sh
(
  set -eu
  cd <checkout ameesh @ e256558523b3c354554ed69790db61a17607b65b>
  export PGOPTIONS='-c search_path=public'
  DSN='<DSN superuser local de la base ameesh>'
  psql "$DSN" -X -v ON_ERROR_STOP=1 -f deploy/sql/role-superviseur.sql
  psql "$DSN" -X -v ON_ERROR_STOP=1 -f deploy/sql/role-superviseur-contenus.sql
  psql "$DSN" -X -v ON_ERROR_STOP=1 -f deploy/sql/role-auditeur.sql
  psql "$DSN" -X -c '\password auditeur_lecture'     # mot de passe généré, jamais en courrier
)
```

`set -e` dans le sous-shell arrête la séquence au premier psql en échec (ON_ERROR_STOP n'arrête que le psql courant). **Transaction par script** : chacun ouvre et valide la sienne ; il n'y a pas de transaction globale des trois. Un échec au 2e ou 3e laisse les précédents appliqués (idempotents, relançables) ; rien à défaire avant de corriger et relancer.

Contrôle après (lecture) : `SELECT rolname, rolcanlogin, rolconfig FROM pg_roles WHERE rolname='auditeur_lecture';` → LOGIN, `{default_transaction_read_only=on,jit=off}`.

## 4. Secret et drop-in (AG2, compte `ameesh`) — par BG après le geste owner

1. L'owner dépose le mot de passe dans un fichier 0600 sur BG (jamais en courrier) ; BG crée `/home/ameesh/.config/ameesh/auditeur.pgpass` (0600, ameesh), **une ligne** `10.77.0.1:5432:ameesh:auditeur_lecture:<mdp>`, distincte du `.pgpass` commun, puis `shred` du fichier de transit.
2. Drop-in `~ameesh/.config/systemd/user/ameesh-runner-agent@auditeur.service.d/auditeur-dsn.conf` :
   `[Service]` / `Environment="AUDITEUR_DSN=host=10.77.0.1 port=5432 dbname=ameesh user=auditeur_lecture sslmode=require"` et `Environment=PGPASSFILE=%h/.config/ameesh/auditeur.pgpass` (aucun mot de passe dans le drop-in).
3. `systemctl --user daemon-reload` puis redémarrage de l'exécuteur **entre deux tours** (arrêt prouvé, comme staging.sh) — l'auditeur est critique : pas de pause, redémarrage seul.
4. Vérification par l'auditeur : `psql "$AUDITEUR_DSN" -X -c 'select 1'` → 1 ; `create temp table t(x int)` → échec (read-only).

## 5. Point à trancher par les relecteurs (N)

Le `.pgpass` de `ameesh` sur AG2 est lisible par **tous** les agents qui tournent sous ce compte unix, pas seulement l'auditeur. Le rôle est en lecture seule (état + contenus, jamais signatures/nonces) ; la lecture des contenus par d'autres agents serait un élargissement. Alternatives : (a) accepter (les agents lisent déjà les contenus via leur client) ; (b) fichier pgpass dédié via `PGPASSFILE` dans le seul drop-in de l'auditeur, 0600 ameesh (même compte unix : protection nominale seulement) ; (c) compte unix dédié (hors périmètre). Recommandation BG : (b). **Révision v2 (03:0xZ, mesh-design #199919) : (b) retenue, sslmode=require (SSL actif sur 10.77.0.1, relevé `show ssl`=on).**

## 6. Retour arrière

`DROP ROLE auditeur_lecture` (après `REVOKE` des deux appartenances) par l'owner ; retrait du drop-in et de la ligne .pgpass ; les rôles superviseur NOLOGIN peuvent rester (aucune connexion possible sous leur nom).


v3 (03:1xZ) : §3 en sous-shell `set -eu`, transaction par script explicitée (avis B ameesh-cx3 #199936).
