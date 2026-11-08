# #5495 — dossier C3 v2 : exécuteur élu L2 sur AG1 et AG2

Bureau (BG), 2026-11-08, sur les réponses de mesh-design #200655 au dossier v1
(`DOSSIER-C3-v1.md`, conservé). Branche `review/bureau-5495-c3`.

**Aucun geste.** Relevés en lecture seule, datés. Les scripts sont à blanc par
défaut et n'ont **pas** été lancés avec `--apply`. Un staging hors créneau de
`97285a07` (unité `ameesh-staging-hc-20261108T1130` sur BG) tournait pendant la
rédaction : les relevés d'état des VM sont refaits après sa fin, voir §8.

Cible : la coupure du **09/11 à 10:00Z**. Relecture : deux familles hors claude.
Q3 et Q4 appellent d'abord un avis bref.

## 0. Ce que v2 change par rapport à v1

| Réponse | v1 | v2 |
|---|---|---|
| Q1 : tick de release `--no-staging` (#4215, #4220) | Non vu ; P4 posait seulement la config de soumission | P4 se fait **en deux temps** : la config de soumission est posée **et** le drop-in `10-sans-staging.conf` est retiré chez les deux candidats, à l'activation. Le retour le repose. La sonde vérifie que le tick tourne sans `--no-staging` |
| Q2 : parité d'AG2 **avant** C3 | AG2 hors C3 | Les P1 à P3 valent pour AG1 **et** AG2. Le nouveau **P5** amène AG2 au niveau 2A V6 (garde, drop-in, `bascule.env` = PUBLIC_ENV avec acteur machine). La clé L2 d'AG2 a été vérifiée sur les 3 cibles. Le porteur est le détenteur de `mesh-design` à T-15 |
| Q5 : minuteries actives après C3 | Question ouverte | Elles restent actives sur AG1 et AG2 après un C3 réussi et attesté. BG reste le repli. Le staging manuel du bureau ne cesse qu'après **deux** C3 attestés de suite |
| Q4 : coût de l'alignement | Écart sans effet, réalignement en lot suivant | Coût évalué au §5. Le **P6** est proposé : kit sur la version de flotte au moment de la pose, avec une porte de repli datée |

## 1. Relevés nouveaux (08/11, heures UTC sur chaque ligne)

**Drop-in de release** (11:26:50Z AG1, 11:26:51Z AG2, 11:26:52Z AG3) :

- `~ameesh/.config/systemd/user/ameesh-release.service.d/10-sans-staging.conf` est présent sur
  AG1 (04/11 22:06) et AG2 (28/10 11:21), avec les mêmes octets (sha256 `3c213e4349d334a0…`).
  Son contenu : `ExecStart=` vide, puis `ExecStart=%h/.local/share/ameesh/venv/bin/ameesh release tick --no-staging`.
  Ses commentaires citent #4215, #4220 et #4219.
- `systemctl --user -M ameesh@ show ameesh-release.service -p ExecStart` : `release tick --no-staging`
  sur AG1 et AG2. Minuterie `ameesh-release.timer` enabled et active sur les deux.
- AG3 : ni drop-in ni unité de release (`not-found`).
- Unité de base `deploy/systemd/ameesh-release.service` (1.44.1, posée sur AG2 sous l'empreinte
  `3f113e1f…`) : `ExecStart=%h/.local/share/ameesh/venv/bin/ameesh release tick`.

**Clé L2 d'AG2 du 04/11** (11:27:10-11Z) :

- Empreinte `SHA256:U+207G71EWpLbzAgIikIoaABfit0Jd3MkT7L4ZwNG30`, commentaire `ameesh-staging-L2-AG2-5495-20261104`.
- Ligne `restrict,from="78.232.60.1,10.77.0.4" ssh-ed25519 … ameesh-staging-L2-AG2-5495-20261104`
  présente sur AG1, AG2, AG3, et aussi sur manaty-mesh-1. Le `from=` est conforme : IP publique
  et WireGuard d'AG2, comme le modèle 2B d'AG1.
- Depuis AG2 sous l'uid 980 (`ssh -F ~/.ssh/config root@… id -u`), le résultat vaut 0 vers
  78.232.56.22, 78.232.60.1, 51.15.137.245 et 10.77.0.1.
- Home et `.ssh` en 700, privée en 600 ameesh-staging. `test -r` de la privée par ameesh : refus.

**Écart AG2 / 2A V6** (11:2xZ) :

- `bascule.env` d'AG2 : 204 o, sha `15887afb…`, mêmes clés que celui d'AG1.
- Remplacer seulement `AMEESH_OPERATEUR=human:smichea` par `ameesh:staging` donne exactement
  `PUBLIC_ENV` de la garde (205 o, `1223707f…` = AG1). Comparaison faite en mémoire, rien n'est écrit.
- La garde `staging-startup-guard.py` (`4bde802e`) ne dépend pas de l'hôte. Elle n'admet que
  l'uid 980 et les nodenames `ameesh-agents-1` et `ameesh-agents-2`, et pinne `PUBLIC_ENV`.
  Sa source relue est sur AG2 : `/home/ameesh/preparation-5495-AG1-20261105/phase2A-review-v6/AG1-L2-phase2A-v6/staging-startup-guard.py`, sha `4bde802e`.
- AG2 n'a ni `~/.local/libexec` ni `ameesh-staging.service.d`. `/usr/bin/python3.12` est présent.
- Base de données d'AG2 : `env` porte `AMEESH_DSN=postgresql://ameesh@10.77.0.1:5432/ameesh?sslmode=require`,
  et le mot de passe est dans `~ameesh-staging/.pgpass` (980 0600, une ligne `10.77.0.1:5432:ameesh:ameesh:…`).
  - Cela satisfait déjà la borne 2 de #195967 : 980 se connecte directement à la base en TLS
    et ne lit aucun fichier de 1001.
  - La résolution de la phase 2A n'est donc **pas** à refaire sur AG2. Elle n'était due qu'à
    AG1, où 1001 passe par PgBouncer.
- Runtime d'AG2 :
  - `bin/python` → `/usr/bin/python3.12` (`e50d468e`) ;
  - `quiescence-staging.sh` `f988ee7a` ;
  - clone dédié à `5c1e679b`, Linger yes ;
  - FragmentPath du service `~/.config/systemd/user/ameesh-staging.service` (`f8add4eb`), aucun drop-in.

**Code, de 5c1 à la flotte** (git, sans hôte) :

- Identiques entre `5c1e679b` et `97285a07` : `staging_executor.py`, `role_run.py`,
  `staging_watch.py`, `staging_pending.py`, `storage/postgres/events.py`, `bascule.py`,
  `deploy/systemd/ameesh-staging.service` et `quiescence-staging.sh`.
- La minuterie de `97285a07` est le blob `c710a0d2` (= 1.44.1). `pyproject` : 1.44.1.
  `5f5bc7f2a..97285a07` = 15 commits.

## 2. Points 1 à 5 et préalables (mise à jour)

Les constats de v1 (§1-§2) restent valables pour AG1. Seuls les écarts sont repris ici.

- **Point 1 : canon d'AG2.** P1 vaut pour AG2 aussi : `c336c53c` → canon des agents, `canon-c336c53c-retained-before-<new>` conservé.
- **Point 2 : canon d'AG1.** Inchangé (P1).
- **Point 3 : cgroup.** Inchangé. La preuve s'obtient sur le porteur réel : `sonde-c3-v2.sh apres`
  lit les drains `ameesh:staging` sans filtrer l'hôte, et le journal des deux unités.
- **Point 4 : minuterie.** P2 vaut pour AG1 et AG2.
- **Point 5 : N7.** La sonde couvre les deux hôtes. L'avis bref des relecteurs (Q3) est attendu.

**P0 : porteur.**

- Le porteur est celui des deux qui détient `mesh-design` à T-15 (`sonde-c3-v2.sh go`). Aucun épinglage.
- Une bascule entre T-15 et la coupure est acceptée (#200655 Q1). Le nouveau détenteur, au même
  niveau, porte alors la demande ; sinon BG relance après constat.

**P3 : arguments.** Mêmes arguments que BG, sur les deux hôtes. `/etc/ameesh/staging-submit.json`
garde les mêmes octets que la copie 980 de chaque hôte, donc les mêmes octets sur AG1 et AG2.

**P4 : soumission et release.** À l'activation, sur AG1 **et** AG2 :

- poser `~ameesh/.config/ameesh/staging-executor.json` (copie de `/etc/ameesh/staging-submit.json`, ameesh 0600) ;
- activer la minuterie L2 ;
- enfin, retirer `10-sans-staging.conf` (déplacé dans les reçus root) puis `daemon-reload` du compte ameesh.

Ordre et raison :

- Soumission d'abord : `release_cutoff.py` l. 1186-1190 exige la clé sous rôle élu, et
  `settings()` échouerait après le tag si le drop-in tombait sans ce fichier.
- Retrait sur les **deux** candidats, et pas seulement chez le porteur : le tick de release
  n'agit que chez le détenteur (`release.role = mesh-design`). Après une bascule de détenteur,
  le nouveau soumet donc à son propre exécuteur, déjà actif (Q5). Cela évite un retour à BG
  dû seulement au drop-in.
- Le retrait se fait quand `ameesh-release.service` est inactif ; sinon le script s'arrête et on réessaie.

**P5 : parité 2A V6 d'AG2** (nouveau). Dans `pose-c3-v2.sh prealables AG2`, après P1 à P3 :

- `bascule.env` : seule la ligne d'acteur change, avec écriture atomique 980:0600. L'empreinte
  du résultat doit commencer par `1223707f` **avant** le renommage.
- La garde est copiée depuis la source relue de la V6 (sha `4bde802e` vérifié avant la copie),
  dans `~/.local/libexec/` en 980 0755, comme sur AG1.
- Le drop-in `10-native-actor.conf` reçoit les octets d'AG1 (`ce3ed09c`, vérifié en local),
  en 980 0644, puis `daemon-reload` du gestionnaire 980.
- Contrôles :
  - empreintes de `bascule.env`, de la garde et du drop-in d'AG2 égales à celles d'AG1 ;
  - la garde lancée sous 980 (`env -i AMEESH_OPERATEUR=ameesh:staging python3.12 -I -B`) rend 0.
- Rien d'autre ne change sur AG2 : la base reste en accès direct, et le résolveur de la phase 2A n'est pas posé.
- Restes du 04/11 laissés en place : `quiescence-qualification.env` (non lu par l'unité) et `venv-6add86b`.

## 3. Séquence C3 proposée : coupure du 09/11 à 10:00Z

STOP au premier refus nommé.

1. **Avant le 09/11 06:00Z** : avis des deux familles sur v2 (avis bref sur Q3 et Q4), puis feu du pilote.
2. **P6, s'il est retenu** (§5) : kit, requalification par l'auditeur, avis sur l'installateur
   v3. Il faut avoir fini avant 08:00Z, sinon on garde 5c1 avec la condition de v1 §4.
3. **T-60 (09:00Z)** :
   - `preparer-canon-c3.sh <HEAD des agents>` ;
   - `pose-c3-v2.sh prealables AG1 …` à blanc puis `--apply` ;
   - même chose pour AG2.
   - Chaque pose finit par `sonde-c3-v2.sh prealables --hotes <h>`.
   - Hors des créneaux de coupure, et jamais pendant une séquence : le script contrôle l'unité 980 et les séquences BG.
4. **T-15 (09:45Z)** : `sonde-c3-v2.sh go`. Il couvre :
   - les préalables des deux hôtes ;
   - le détenteur `mesh-design` ∈ {AG1, AG2} ;
   - aucune migration non additive inconnue du runtime L2 ;
   - aucune demande `mesh-staging` en cours ;
   - aucune séquence BG.
   S'il est KO : NO-GO, BG porte 10:00Z, et les préalables restent posés (inertes).
5. **GO** : `pose-c3-v2.sh --apply activer`, puis `sonde-c3-v2.sh actif` :
   - minuteries enabled et armées sur les deux hôtes ;
   - `ExecStart` sans `--no-staging` ;
   - soumission présente.
   BG ne lance pas de coupure.
6. **10:00Z** : la release du détenteur soumet la vraie demande, et son exécuteur la prend dans les 60 s.
7. **Après** : `sonde-c3-v2.sh apres --depuis <TS d'activation>`, avec :
   - l'enregistrement : `host` = porteur, `canon_ref` = canon des agents, empreintes ;
   - les drains `ameesh:staging`, et 0 drain humain ;
   - le journal des deux unités ;
   - l'attestation `actif.py`.
   Ensuite, attestation de l'auditeur. Les minuteries **restent actives** (Q5).
8. Le staging manuel du bureau ne cesse qu'après **deux** C3 attestés de suite. Le bureau
   garde le repli jusque-là, et vérifie à chaque coupure que la demande est prise.

**Retour (repli #190310 §5).**

- `retour-c3-v2.sh --apply activation <TS>`, sur AG1 et AG2 :
  - minuterie désactivée ;
  - si une unité est encore en séquence : arrêt du script (rc 3), **sans la tuer**, car son piège TERM fait le retour d'hôte ;
  - drop-in `--no-staging` reposé à partir des reçus (sha contrôlé) ;
  - soumission retirée.
- Puis BG relance la coupure dès que `staging_watch in_flight` est vide.
- `retour-c3-v2.sh --apply prealables <h> <TS>` remet les octets d'avant. Sur AG2, cela inclut
  `bascule.env` du 04/11, et la garde et le drop-in sont retirés vers les reçus.

## 4. Questions Q3 et Q4 : ce que les relecteurs tranchent d'abord (avis bref)

- **Q3 (N7).** v2 garde la proposition de v1 :
  - la sonde de détection, élargie à AG2 ;
  - un lot doctor, que le bureau n'écrit pas ;
  - pas de durcissement des droits. 980 possède ses dossiers ; seule une unité système ou
    `chattr +i` sur toute la chaîne fermerait N7.
  Avis attendu : « détection suffit » ou « exiger X avant C3 ».
- **Q4 (version).** Voir §5. Avis attendu : « P6 avant C3 », « 5c1 avec condition », ou « P6 après le premier C3 ».

## 5. Coût de l'alignement des deux kits sur la version de flotte (P6, proposé)

**Définition.** M est le commit de flotte **au moment de la pose** : le SHA de la dernière
attestation « staging actif » des trois VM (`actif-<sha>.json`), relu à T-60. Ce n'est pas une
valeur figée : après le staging hors créneau en cours, ce sera `97285a07`, et une coupure suivante l'avancera.

**Ce qui change entre 5c1 et M, pour le kit.**

- Le paquet `ameesh` (1.37.0 → la version de M) et son arbre (`expected_package_tree_sha256` du reçu de construction).
- `runtime-pins.txt` gagne `coverage==7.17.2` et `pyyaml==6.0.3` (#6291, `0e81b26db`).
- `runtime-build-pins.txt` est inchangé, de même que `bascule.py`.
- `bin/python` reste `/usr/bin/python3.12` (`e50d468e`), et `quiescence-staging.sh` reste `f988ee7a`.

**Étapes et coût estimé**, d'après le précédent du 04/11 (#190452 à 18:34Z, kit `5c1e679b`
construit vers 18:45Z, auditeur CONFORME à 18:54Z, pose passive sur AG2 à 19:26Z) :

| # | Étape | Qui | Durée estimée |
|---|---|---|---|
| a | Kit scellé depuis M : enveloppe du bureau `build-sealed-package.py` / `Dockerfile.builder` (réseau coupé à la construction), même format `ameesh-venv-staging/3`, wheelhouse = pins de M. Inventaire et SHA256SUMS | bureau, sur BG | 20-30 min |
| b | Requalification indépendante : inventaire = git à M, roues = pins à M, absence de pin | auditeur | 20-30 min (précédent : < 20 min) |
| c | Installateur v3 : `install-passive-runtime.py` fige aujourd'hui `SHA='5c1e679b…'` et le dossier de kit (l. 25, 158, 192-201). Diff borné : SHA et version en paramètres vérifiés contre le reçu du kit, `preparer` verrouillé (`--lock --wheelhouse --manifeste --verificateur`), puis `activer --precedent 5c1e679` | bureau, puis 2 avis | 30 min + avis (le chemin critique) |
| d | Pose sur AG1 puis AG2 : `preparer` et `activer` sous 980, et `expected_sha256` mis à jour dans les deux copies de config (arbre du paquet seulement). Le clone dédié n'a pas à bouger : le tick fait `git fetch` et prend l'arbre du commit demandé | bureau | environ 5 min par hôte |
| e | Retour : `bascule.py retour` (repointe `venv-courant` sur `venv-5c1e679`, sans pip) et `expected_sha256` d'avant | — | 1 min |

**Total.** Environ 2 h 30 de travail. Le chemin critique est formé par les avis (c) et la
requalification (b). Le coût est modéré : faisable avant le 09/11 08:00Z si les étapes (a)
et (c) partent ce soir.

**Proposition.**

- Faire P6 **avant** C3 si (b) et les avis sur (c) sont rendus le 09/11 avant 08:00Z.
- Sinon C3 tourne sur 5c1 avec la condition de v1 §4 (0 migration non additive inconnue
  du runtime, case `go`), et P6 passe après le premier C3 attesté.
- Dans les deux cas, l'écart se reformera à chaque release. Un lot de suite devra fixer la
  règle « runtime L2 = version de flotte à N-1 au plus », avec l'étape de mise à jour
  propre au compte dédié (`docs/DEPLOIEMENT-HOTE-AGENTS.md` l. 970-990).

## 6. Pièces (v2)

| Fichier | Rôle |
|---|---|
| `DOSSIER-C3-v2.md` | Ce dossier |
| `sonde-c3-v2.sh` | Lecture seule, AG1 et AG2 : `prealables`, `go`, `actif`, `apres` |
| `pose-c3-v2.sh` | À blanc par défaut : `prealables <AG1\|AG2>` (P1-P3, plus P5 sur AG2), `activer` (P4 sur les deux) |
| `retour-c3-v2.sh` | À blanc par défaut : `activation <TS>`, `prealables <h> <TS>` |
| `preparer-canon-c3.sh` | Inchangé depuis v1 (BG seulement) |
| `releve-v2-*.txt` | Relevés datés de v2 (§8) |
| `DOSSIER-C3-v1.md`, `sonde-c3.sh`, `pose-c3-AG1.sh`, `retour-c3-AG1.sh`, `releve-prealables-20261108T1100Z.txt` | v1 conservée, remplacée par v2 |
| `MANIFEST.json` | sha256 de chaque pièce, v1 et v2 |

## 7. Points ouverts

- **O1 (relecteurs, avis bref).** Q3 (N7) et Q4 (P6 avant ou après le premier C3).
- **O2 (pilote).** P6 : feu pour lancer (a) et (c) ce soir ? Sans feu, C3 se fait sur 5c1.
- **O3 (pilote).** Le drop-in `--no-staging` est retiré sur **les deux** candidats, et pas
  seulement chez le porteur (raison au §2, P4). Conforme à l'intention de Q1 ?
- **O4 (relecteurs).** Après le retrait du drop-in, chaque tick du détenteur passe par
  `maybe_staging`. Pour une release déjà attestée, la réponse est « already attested » ; il ne
  faut donc pas activer entre un tag et son attestation BG. La case `go` vérifie qu'aucune
  demande n'est en cours, mais pas qu'il n'existe aucune release non attestée. Faut-il ajouter
  cette case, et par quelle lecture ?
- **O5.** Le mot de passe de la base d'AG2 L2 est une copie (`.pgpass` 980). Une rotation du
  rôle `ameesh` casserait AG2 sans casser AG1 (résolveur). C'est à noter dans la procédure de rotation.

## 8. Relevés datés de v2

Le staging hors créneau de `97285a07` s'est terminé à 11:52:34Z
(`terminal.public.json` : rc 0, `no_L2_C3_pose: true`, attestation `actif-97285a076dee.json`
sur 3 hôtes). Les relevés suivants ont été pris **après** cette fin.

- `releve-v2-sonde-prealables-20261108T1152Z.txt` : `sonde-c3-v2.sh prealables --timer-attendu f6460989…` (minuterie 5c1 attendue), à
  11:52:45Z. Verdict : KO (11), et ce sont exactement les écarts que P1, P3 et P5 doivent fermer :
  - AG1 : arguments et canon ;
  - AG2 : garde, DropInPaths, drop-in, dossier drop-in, nombre d'unités, `bascule.env`
    (`15887afb`), garde sous 980, arguments et canon.
  - Tout le reste est OK sur les deux hôtes :
    - service, minuterie, venv `venv-5c1e679` ;
    - empreintes attendues des fichiers ;
    - ssh root sous 980 vers AG1, AG2 et AG3 ;
    - clé privée illisible par ameesh ;
    - minuterie L2 disabled ;
    - drop-in `--no-staging` présent, tick avec `--no-staging` ;
    - pas de config de soumission ;
    - aucune séquence BG.
- `releve-v2-blancs-20261108T1153Z.txt` (11:53-11:54Z), sans aucun `--apply` :
  - `pose-c3-v2.sh prealables AG1` et `… AG2` à blanc : contrôles OK, rc 0, gestes listés. AG2
    passe ses contrôles propres : `bascule.env` = `15887afb`, garde et drop-in absents, source
    de la garde = `4bde802e`.
  - `pose-c3-v2.sh activer` : STOP, comme attendu, car la sonde `go` est KO (préalables non posés).
    Cases propres à `go` :
    - détenteur `mesh-design` = `ameesh-agents-2` (OK, porteur possible) ;
    - 0 migration non additive au-delà de 0544 ;
    - aucune demande `mesh-staging` en cours.
  - `retour-c3-v2.sh activation` et `prealables` : STOP sur l'absence des reçus, comme attendu.
  - Une archive d'essai du canon a été préparée sur BG seulement (`b8c4ea12`, arbre `219caba4`) ;
    elle n'a été envoyée à aucun hôte.
