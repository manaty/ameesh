# #5495 phase 2B — clé L2 d'AG1 : dossier de pose (AUCUNE POSE) — v2

Auteur : bureau (BG, famille claude), 08/11. Références : décision #184823 a ; modèle = pose AG2 du 04/11 16:40Z (#190288, reçu /root/ameesh-operator-L2-AG2-key-20261104T1640 sur AG2) ; plan #190310 corrigé #190531 ; phase 2A V6 AG1 CONFORME (auditeur 06/11 17:02Z) ; périmètre mesh-design #200023.

## Périmètre
- Clé Ed25519 neuve générée **sur AG1** par l'uid 980 `ameesh-staging`, `/home/ameesh-staging/.ssh/id_ed25519` 0600, jamais lue ni exportée.
- Partie publique ajoutée à `/root/.ssh/authorized_keys` de **AG1, AG2, AG3** : `restrict,from="78.232.56.22,10.77.0.3" <pub> ameesh-staging-L2-AG1-5495-<date>`. Lignes existantes conservées, sauvegarde par cible.
- **manaty-mesh-1 reporté** (canon §14, aucun root depuis les VM) — consigné, ne retient ni 2B ni le staging.
- Aucun timer/service L2 activé, aucune demande SHA, aucun restart (sshd, agents, PG).

## État relevé 08/11 04:1xZ (lecture seule)
- AG1 : compte `ameesh-staging` 980:980, nologin ; home et `.ssh` 0700 980 ; `.ssh/config` (IdentitiesOnly, BatchMode, StrictHostKeyChecking yes, known_hosts dédié) ; `known_hosts` sha256 `60d47dce…` = approuvé (map 2A) ; **aucune clé** ; pas de compte `runner` sur AG1.
- Cibles AG1/AG2/AG3 : `authorized_keys` 600 root, 3 lignes chacune (dont la ligne L2 d'AG2), aucune ligne `ameesh-staging-L2-AG1-`, fin de fichier `\n`.
- `pose-2B-AG1.sh` sans `--apply` (contrôles seuls) : **préalables OK**, rc 0, aucun effet.

## Acte (après deux avis hors claude + feu exact mesh-design)
`pose-2B-AG1.sh --apply` depuis bureau-goal : 0 préalables (STOP au premier écart) → 1 génération sur AG1 → 2 ajout par cible (Python : sauvegarde, `enrollment.json`, ajout, relecture exacte avant/après, 600 root) → 3 preuves : `ssh root@<cible> id -u` = 0 depuis AG1 sous 980 pour les trois cibles ; refus de lecture de la privée pour `ameesh` et `nobody`. Reçus : `/root/ameesh-operator-L2-AG1-key-<TS>/` sur chaque cible.

## Retour arrière
`retour-2B-AG1.sh` : retrait de la seule ligne `ameesh-staging-L2-AG1-*` sur chaque cible (copie avant retour), puis `shred` de la clé sur AG1.

## Provenance
Reçu natif `ameesh provenance record` indisponible hors tour géré (« no managed turn ») : l'auteur est déclaré ici et dans MANIFEST.json (bureau, claude). Relecteurs désignés hors claude : ameesh-cx1 (codex) + mty-amh-impl-06 (deepseek).

## v2 (08/11 04:2xZ) — avis B/P2 ameesh-cx1
- Préalable droits home/.ssh : échec SSH/stat = STOP ; valeur exacte attendue `700 ameesh-staging` ×2.
- Retour **lié à la pose** : `retour-2B-AG1.sh <TS>` relit le reçu `enrollment.json` de chaque cible et retire exactement sa `new_line` (égalité de ligne complète, une seule occurrence, les autres lignes intactes, écriture atomique 600 root, copie avant retour dans le dossier du reçu). Pose partielle : cible sans reçu ou ligne absente → rien à retirer. Ligne en double ou fichier qui deviendrait vide → refus. Clé AG1 détruite seulement si sa partie publique est celle du reçu ; sinon STOP (pose partielle avant tout reçu : clé laissée, retrait manuel tracé).
- Test local du retour, 5 cas : normal (retirée, autres intactes), absente, sans reçu, double (refus), seule ligne (refus).
