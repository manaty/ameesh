# #6002 — pose AG3, v4 : delta de v3 (demande mesh-design #200690)

Bureau (BG), 2026-11-08 ~12:10Z. Branche `review/bureau-6002-tunnel`. La v3 (`d7d8e573`,
fichiers `DOSSIER-POSE-AG3.md` et scripts sans suffixe) est conservée.

**AUCUN GESTE.** Relevés en lecture seule (AG3 et base centrale), datés. Scripts à blanc par
défaut. Pose hors coupure, sur feu de mesh-design.

Relecteurs prévus :
- mty-amh-impl-11 (codex) ;
- mty-amh-impl-03 (deepseek).

## 0. Ce qui change par rapport à v3

| Sujet | v3 | v4 |
|---|---|---|
| Code de référence | `41262fcde6bf` | **M = `97285a076dee`** (actif en staging depuis 11:52Z, attestation `actif-97285a076dee.json`). M contient #6296 et #6298 |
| Rôle DB (étape 2) | Non posé (attendait #6298) | `pose-DB-vm_probe.sh` : `deploy/sql/role-vm-probe.sql` à M, en postgres sur la base centrale, par la route AG2 → `runuser ameesh-staging` → `ssh root@10.77.0.1` (go owner 08/11). Puis audit `_contrat-vm-probe.sql`. Aucune copie du mot de passe du rôle `ameesh` |
| Secret vm_probe | — | Généré **sur AG3**, jamais affiché ni transféré. Seul le vérificateur SCRAM quitte AG3 (§2) |
| #6296 | Non posé | `checks/health.py`, `checks/venv-processus.py`, `/var/lib/ameesh-vm-health` (ameesh:vm-operator **2750**), cartes producteur et lecteur, `ameesh-vm-health.{service,timer}` en unités **utilisateur d'ameesh** activées. Aucun helper sudo ; `vm-remote.py` tel que fusionné |
| remote.json | Non posé (tout refusé) | Posé (root:vm-operator 0640), avec la connexion `remote-database.json` (root:vm-operator 0640) et le passfile vm-operator 0600 |
| Fragment sshd | Sans `PermitListen` | Modèle de M : `PermitListen none` ajouté. `sshd -T` de vm-operator est vérifié sur les 7 valeurs du README, étape 4 |
| Tests | A et B, sans base | B : `version-mode` et `doctor` réels, lectures natives sous vm_probe, refus de toute autre table (§4) |
| Retour | Compte et sshd | Miroir complet : minuteur, unités, ACL, cartes, passfile détruit, puis `retour-DB-vm_probe.sh` (rôles et fonction retirés) |

### Écart signalé : PR #4863

- La v3 citait la tête `38fdfa30`. Ce commit n'est **pas** ancêtre de M
  (`git merge-base --is-ancestor 38fdfa30 97285a07` → faux).
- La PR a été fusionnée par la composition `1599ab0c2` (« composition : #6298 12a8b9efafa4 »),
  sur la tête `12a8b9efa`. Après 38fdfa30, celle-ci ajoute :
  - `780701b12` (« ignore inert extension SELECT behind closed schema ») ;
  - `12a8b9efa` (« combine VM contracts after health registry integration »).
- La v4 référence uniquement les octets de M :
  - `role-vm-probe.sql` sha256 `2a87ee04d0c4af4a…` ;
  - `_contrat-vm-probe.sql` `4c9eecb0532b0704…`.
  Les deux sont vérifiés par le script avant l'envoi.
- `vm-remote.py` a changé de 41262fcd à M (+45/-… lignes) : `remote.json` exige désormais `checks`.
- `sshd-match.example` de M corrige le défaut signalé en v3 (`PermitUserEnvironment` retiré du Match).

## 1. Relevés (lecture seule)

**AG3, 12:05:27Z.**
- Absents : `vm-operator`, `/opt/ameesh-vm-adapter`, `/etc/ameesh-vm-adapter`, `/var/lib/ameesh-vm-health`, `/var/lib/ameesh-vm-remote`.
- `ameesh` : uid 1001, groupes 1001 et **112 (docker)**. Hors périmètre, à rapprocher de #5781.
- **`/home/ameesh` en 750 ameesh:ameesh.** `.local`, `.local/share` et `.local/share/ameesh` sont en 775. `venv-courant` → `venv-97285a0` (755).
- 8 unités runners actives : `ameesh-cx3`, `ameesh-dev1`, `ameesh-dev2`, `ameesh-dev4`, `ameesh-dev7`, `claude1`, `claude2`, `codex2`. Aucune n'est « enabled ».
- Linger d'ameesh : yes ; `~ameesh/.config/ameesh/env` présent (0600).
- `/usr/bin/python3` → `python3.12` ; `systemd-run` présent ; psql 17.11.
- `setfacl`/`getfacl` **absents** (paquet `acl` non installé).
- WireGuard `ameesh` actif, `10.77.0.1:5432` joignable.

**Base centrale, 12:04-12:11Z**, via AG2 → mesh-1, en postgres, lecture seule :
- PostgreSQL 17.11, `current_schema` = `public`, schéma `diagnostics` présent.
- Rôles `vm_probe` et `vm_probe_writer` absents ; `vm_probe_send` absente.
- `pg_hba` : `hostssl ameesh all 10.77.0.0/24 scram-sha-256`, donc AG3 (10.77.0.5) est admis en TLS sans changement.
- `host_lifecycle` : 0 ligne pour `ameesh-agents-3`. `quiesce` et `runners` rendront donc 75 (pas prêt), pas 1.
- `log_statement=none`, `log_min_duration_statement=-1`, `password_encryption=scram-sha-256`.

## 2. Étape 2 : rôle DB et secret

**Ordre.**
1. `pose-AG3-vm-operator-v4.sh --apply` (étapes 1-7). Son étape 6 génère le secret sur AG3.
2. `pose-DB-vm_probe.sh --apply <TS>`.
3. `tests-AG3-vm-operator-v4.sh B`.

**Secret** (`secret-vm-probe.py`, root sur AG3, bibliothèque standard) :
- `secrets.token_urlsafe(32)`, alphabet `[A-Za-z0-9_-]` : ni `:` ni `\` pour libpq, SASLprep neutre.
- Écrit dans `/var/lib/ameesh-vm-remote/vm_probe.pgpass` : vm-operator 0600, `O_EXCL|O_NOFOLLOW`,
  dans le répertoire privé 0700 du compte (README étape 2). Ligne `10.77.0.1:5432:ameesh:vm_probe:<secret>`.
- Calcul du **vérificateur** SCRAM-SHA-256 (sel aléatoire, 4096 itérations), écrit en root 0600 dans
  le dossier de reçus de la pose.
- Rien n'est affiché.
- Essai sur BG avec `postgres:17` en conteneur jetable : `ALTER ROLE … PASSWORD :'v'` avec ce
  vérificateur, puis connexion de `vm_probe` par le passfile → « connexion vm_probe OK ». Conteneur
  et fichiers détruits ensuite.

**Rôle** (`pose-DB-vm_probe.sh`) :
- Préalables : rôles et fonction absents, PG 17, schéma public.
- Les deux fichiers SQL de M vont dans `/var/tmp/ameesh-6002-vm-probe-<TS>` (root:postgres 0750/0640), pour que `\ir` fonctionne.
- `runuser -u postgres -- env PGOPTIONS='-c search_path=public' psql -d ameesh -X -v ON_ERROR_STOP=1 -f role-vm-probe.sql`.
  Le script est transactionnel et inclut le contrat. On exige « vm_probe contract held ».
- Le vérificateur passe d'AG3 à BG puis à mesh-1, par tube, vers un fichier root:postgres 0640.
  Ensuite :
  - `\set v` lu par `cat` ;
  - `SET password_encryption='scram-sha-256'` ;
  - `ALTER ROLE vm_probe PASSWORD :'v'` ;
  - `shred` du fichier sur mesh-1, puis du vérificateur sur AG3.
- Audit seul `_contrat-vm-probe.sql` après le mot de passe : il vérifie aussi « SCRAM dédié », puis on fait le ménage.

**Écart nommé avec le commentaire de `role-vm-probe.sql`** (« le bureau fixe le secret avec
`\password vm_probe` ») :
- `\password` exige le secret en clair dans la session psql de mesh-1, donc son transfert.
- Le vérificateur obtient le même résultat en base (`rolpassword` SCRAM) sans que le secret quitte AG3.
- Le vérificateur n'est pas le secret. Il permettrait une attaque hors ligne, que 32 octets aléatoires rendent impraticable.
- `log_statement=none` : l'`ALTER ROLE` n'est pas journalisé.
- À valider par les relecteurs (O3).

## 3. #6296 : contrôles de santé (README deploy/vm-adapter, étapes 2, 3 et 6)

- **Code, à M.**
  - `/opt/ameesh-vm-adapter/checks/health.py` et `checks/venv-processus.py` (root 0644, parents root non inscriptibles) ;
  - `remote/vm-remote.py`, tel que fusionné.
- **`/var/lib/ameesh-vm-health`** : `install -d -m 2750 -o ameesh -g vm-operator`. Les reçus héritent
  du groupe lecteur ; ameesh n'est pas ajouté au groupe ; vm-operator n'a aucun droit d'écriture.
- **Cartes** (`/etc/ameesh-vm-adapter/`, root 0755) :

| Fichier | Droits | Contenu |
|---|---|---|
| `remote.json` | root:vm-operator 0640 | `enrollment.example.json` rendu : `host=ameesh-agents-3`, `probe_agent=mty-amh-impl-15` (cible v2), `ameesh_config=…/remote-database.json`, `state_dir=/var/lib/ameesh-vm-remote`, 60 s, 900 s, argv fixes `checks.version-mode` et `checks.doctor` du modèle |
| `remote-database.json` | root:vm-operator 0640 | `host=10.77.0.1 port=5432 dbname=ameesh user=vm_probe sslmode=require passfile=/var/lib/ameesh-vm-remote/vm_probe.pgpass`, schéma `public`. Aucun secret |
| `health-reader.json` | root:vm-operator 0640 | Reçu partagé, `producer_uid` = uid réel d'ameesh lu à la pose (1001), période 60, venv stable |
| `health-producer.json` | root:root 0644 | Venv stable, vérificateur `venv-processus.py`, **liste exhaustive des unités runners actives lue à la pose** (8 aujourd'hui), EnvironmentFile d'ameesh (non copié), reçu |

- **Unités.** `ameesh-vm-health.service` et `.timer` de M sont installés dans
  `~ameesh/.config/systemd/user/` (ameesh 0644, non inscriptibles par vm-operator). Puis
  `systemctl --user -M ameesh@ daemon-reload` et `enable --now`.
  Aucune unité système, aucun sudo ajouté. La pose attend 70 s et relève le reçu.

**Blocage relevé : accès du lecteur au venv.**
- Le README (étape 6) exige que vm-operator puisse observer la cible de `venv-courant` et ses parents.
- Or `/home/ameesh` est en 750 : sans changement, `health.py version-mode` et `doctor` refusent
  (fermé), et les tests B échouent.
- Le script propose `--acces-venv acl` : installer le paquet `acl` (absent), puis
  `setfacl -m u:vm-operator:x /home/ameesh`. C'est une traversée seule, sans lecture de la liste ;
  les sous-dossiers concernés sont déjà en 775.
- Défaut : `aucun`. Le choix revient au pilote (O1).
- Alternative : `chmod o+x /home/ameesh`. Elle est plus large (tous les comptes) ; non proposée.

## 4. Tests (`tests-AG3-vm-operator-v4.sh`)

- **Phase A** (avant le rôle) : `true` et `journalctl` sont admis ; `remote/quiesce` rend 1, car la
  base est refusée.
- **Phase B** (après `pose-DB-vm_probe.sh`) :
  - **Santé #6296 réelle**, en vm-operator, par la clé forcée : `remote/version-mode` rc 0 et
    `remote/doctor` rc 0. Reçu `health.json` en 640 ameesh:vm-operator, âge inférieur à 180 s,
    répertoire en 2750.
  - **Lectures natives réelles sous vm_probe** : `remote/quiesce`, `remote/runners` et
    `remote/probe-receipt <nonce inconnu>` rendent 0 ou 75. 1 signifierait une erreur, de
    connexion ou de droit.
  - **Lecture directe** depuis AG3, en vm-operator, avec son passfile, en transaction lecture seule :
    - `current_user=vm_probe` ;
    - colonnes contractuelles de `host_lifecycle` et `agent_registry` lues ;
    - boucle sur **toutes** les relations hors catalogues : `SELECT 1 FROM t` n'est admis que pour
      les 7 relations du contrat, et `SELECT *` est refusé partout ;
    - le schéma `diagnostics` est refusé.
  - Aucune écriture n'est tentée.
  - Refus inchangés : `id`, `sh -c`, `sudo … status`, argument en trop, PTY. `root@AG3` reste joignable.
- **Remarque** sur la demande « version-mode et doctor lus par vm_probe » : à M, ces deux contrôles
  ne lisent **pas** la base. Le README dit : « Les lecteurs ne consultent ni base, ni bus utilisateur ».
  Ils lisent le reçu du producteur. La lecture par vm_probe est donc prouvée séparément (lignes
  ci-dessus). À confirmer par le pilote (O2).

## 5. Retour en miroir

- `retour-AG3-vm-operator-v4.sh --apply <TS>`, sur AG3 :
  - minuteur désactivé, unités retirées, `daemon-reload` ;
  - ACL retirée, et paquet `acl` retiré s'il a été posé par la pose ;
  - passfile et vérificateur résiduel détruits (`shred`) ;
  - cartes et reçu partagé supprimés ;
  - sudoers, drop-in sshd et clé retirés (`sshd -t`, reload) ;
  - `userdel` ; `/var/lib/ameesh-vm-remote` et `/opt/ameesh-vm-adapter` supprimés.
- `retour-DB-vm_probe.sh --apply`, sur la base centrale, en une transaction :
  - sessions vm_probe terminées ;
  - `DROP OWNED BY vm_probe, vm_probe_writer` (fonction et droits) ;
  - `DROP ROLE` des deux ;
  - contrôle : 0 rôle, 0 fonction.

## 6. Pièces (docs/reviews/6002-pose-ag3/, v4)

| Fichier | Rôle |
|---|---|
| `pose-AG3-vm-operator-v4.sh` | Étapes 0-7 sur AG3, à blanc par défaut, `--acces-venv acl\|aucun` |
| `secret-vm-probe.py` | Secret, passfile et vérificateur, sur AG3 |
| `pose-DB-vm_probe.sh` | Rôle et contrat sur la base centrale, mot de passe par vérificateur |
| `tests-AG3-vm-operator-v4.sh` | Phases A et B |
| `retour-AG3-vm-operator-v4.sh`, `retour-DB-vm_probe.sh` | Miroir |
| `releve-v4-blancs-*.txt` | Blancs du 08/11 12:10Z : préalables AG3 OK (les deux modes), préalables base OK, retours à blanc |
| `SHA256SUMS-v4` | Empreintes v4 |

## 7. Points ouverts

- **O1 (pilote).** Accès de vm-operator à la cible de `venv-courant` : `--acces-venv acl`
  (paquet `acl` plus traversée seule) ou une autre voie. Sans accès, les contrôles #6296 refusent
  (conforme au README) et le test B échoue.
- **O2 (pilote).** Les tests proposés prouvent séparément la santé #6296 (lecteurs du reçu) et les
  lectures sous vm_probe, puisque `version-mode` et `doctor` ne lisent pas la base. Est-ce bien
  l'intention de #200690 ?
- **O3 (relecteurs).** Mot de passe posé par vérificateur SCRAM au lieu de `\password` (§2).
- **O4.** La carte producteur fige la liste des runners actifs à la pose. Tout changement de
  placement sur AG3 rend le reçu rouge tant que la carte n'est pas mise à jour (geste root).
  Faut-il une procédure ?
- **O5.** `host_lifecycle` n'a pas de ligne pour AG3. `quiesce` et `runners` restent donc à 75
  jusqu'à la première inscription de retour ; la preuve d'un 0 viendra de la première vague.
- **O6 (hors périmètre).** `ameesh` est dans le groupe `docker` sur AG3 : même constat que #5781 sur AG1.
