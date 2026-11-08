# #5495 — dossier C3 v2.1

Bureau (BG), 2026-11-08 ~12:35Z. Branche `review/bureau-5495-c3`. Les versions v1 et v2 sont conservées.

v2.1 intègre :
- les avis **B** de mty-amh-impl-11 (#200663 sur v1, #200726 sur v2) ;
- les avis de mty-amh-impl-03 (#200665 sur v1, #200716 sur v2) ;
- les réponses du pilote #200708.

**AUCUN GESTE** sur les VM. Toutes les commandes vers les VM sont des lectures. Les scripts sont
à blanc par défaut ; aucun n'a été lancé avec `--apply`.

Le kit P6 et l'installateur v3 ont été **préparés et éprouvés sur BG seulement** (conteneurs
jetables, réseau coupé). Rien n'a été déposé sur une VM.

## 0. Avis traités

| Avis | Point | v2.1 |
|---|---|---|
| impl-11 B1 (#200663, #200726) | `apres` ne prouvait rien : simple impression ; un témoin simulé passait OK | `sonde-c3-v21.sh apres --sha --depuis --canon` lit la base (`lecteur-c3.py`, lecture seule) et la juge avec `evaluer-c3.py`. Critères : **une** exécution de la demande pour ce SHA depuis l'activation, état `completed`, porteur ∈ {AG1, AG2}, démarrage après l'activation, `canon_ref` = canon des agents relevé à l'activation, empreintes = `expected_sha256`, attestation `staging.attested` du SHA. Dans la fenêtre du run : ≥ 1 drain `paused=true` par `ameesh:staging`, principal = `serve.owner` (lu, `human:smichea` aujourd'hui), motif « drain de déploiement », chaque drain **levé ensuite** par `ameesh:staging`, 0 drain de déploiement humain. **Le témoin d'impl-11 est reproduit** dans `temoin-apres.sh`, avec 13 variantes : il est KO (rc 1), le cas réussi est OK, chaque altération est KO |
| impl-11 B2 | N7 non rejoué après C3 | `n7()` est extrait de l'état attendu et rejoué dans `actif` et `apres`, sur les deux hôtes |
| impl-11 B3 | Fenêtre de release : `!= active` acceptait `activating` et les lectures illisibles | `activer` exige `ActiveState=inactive` et l'absence de job `ameesh-release` en file, sur les **deux** candidats, **avant tout geste**. Il arrête ensuite la minuterie de release (état gardé dans le reçu) et revérifie `inactive`. Il pose soumission, minuterie L2 et retrait du drop-in, contrôle `ExecStart` à part, puis restaure la minuterie de release. Un échec après le premier geste donne un STOP qui nomme le retour. `retour activation` ferme la même fenêtre |
| impl-11 N, impl-03 B1 | `|| true` en fin de `prealables` (l. 141) et sur le retrait du drop-in (l. 155) | Supprimés. La sonde de fin de pose est une **garde dure**. `geste()` arrête au premier échec. L'absence de `--no-staging` est contrôlée à part |
| impl-03 B2 | Le retour pouvait laisser 980 sans canon | `retour prealables` ne touche au canon que si le canon conservé `$RET` **existe** et porte l'ancien HEAD, **et** si le canon courant est celui de la pose. Si le canon courant est déjà l'ancien (pose interrompue), il n'y touche pas. Sinon : STOP, canon non touché. La pose exige `test ! -e "$RET"` avant le renommage |
| impl-03 B3, pilote O4 | La dernière release taguée pouvait ne pas être attestée | Case `go` : `release_sha` du rapport ameesh le plus récent (`state/release/reports/` d'AG1 et AG2), puis attestation `staging.attested` lue **en base, sans effet de bord** (`read_attestation` n'est pas appelé : il réécrit le fichier local), et `actif-<sha12>.json` sur AG1. Sans attestation : NO-GO. Plus `staging_watch.in_flight` vide et un historique non tronqué |
| impl-03 N | Égalité des configs avec un seul fichier | Les deux fichiers sont exigés (`-f`) et leurs deux empreintes comparées |
| impl-03 N | Clé privée : un échec de `runuser` passait pour un refus | Témoin positif `runuser -u ameesh -- id -u` = 1001 avant le test de refus |
| impl-03 N | `git status \| wc -l` | rc de git vérifié (`s=$(git status) && [ -z "$s" ]`) |
| impl-03 N | Contrôle BG faux OK sans bus | La lecture du bus utilisateur est vérifiée (sinon KO), et `in_flight` est lu en base |
| impl-03 N | Minuterie disabled/inactive à T-15 | Cases `is-enabled=disabled`, `ActiveState=inactive` de la minuterie et du service. `activer` les revérifie sur les deux hôtes |
| impl-03 N | Principal = `serve.owner` | Lu en base (`serve.owner(cfg)`), exigé dans `evaluer-c3.py` |
| impl-03 N | Arbres de `expected_sha256` ignorés | Fichiers **et** arbres vérifiés avec `_sha256` et `_tree_sha256` du runtime 980 lui-même (« conformes 3 ») |
| impl-03 N | `MAX_MIGRATION_L2` figé à 544 | La borne est la plus haute migration du **venv courant de 980** sur chaque hôte : 0544 sur 5c1, 0555 après P6 |
| impl-03 N | Le venv attendu était figé | Déduit des chemins de `expected_sha256` (5c1 ou P6) |
| impl-03 N | Retour : dossiers AG2 absents ; pas de contrôle après restauration | Absence tolérée. Contrôle après restauration : octets = reçus, canon = ancien, DropInPaths (vide sur AG2, unique sur AG1) |
| impl-03 N | TS à la minute ; chemins BG en dur | TS = `%Y%m%dT%H%M%SZ-<pid>` ; `AMEESH_SRC` paramétré et vérifié |
| pilote O3 | Drop-in | Retiré sur les DEUX candidats, seulement à `activer` ; reposé sur les deux au retour |

**Q3 (N7).** Les deux relecteurs acceptent la détection seule, dans la limite déclarée (980 peut
changer ses unités). Ils exigent qu'elle soit une garde dure avant et après C3 : c'est fait (`n7()`
dans `prealables`, `go`, `actif` et `apres`).

## 1. Q4 et P6 : chemin principal, divergence à trancher

**Les relecteurs divergent.**
- impl-03 (#200716) accepte 5c1 avec la condition « 0 migration non additive au-delà de la
  migration max du runtime » ; il n'exige pas P6.
- impl-11 (#200726) recommande **P6 avant C3**. Il **refuse le repli automatique sur 5c1** à
  l'heure limite : le contrôle AST des noms et l'absence de migration non additive ne qualifient
  pas la sémantique.

**Proposition v2.1.**
- **P6 est le chemin principal.** Le kit et l'installateur sont prêts (§2), et le test sur BG
  passe de bout en bout.
- Le repli sur 5c1 n'est proposé **que** avec une qualification ciblée du kit ancien : exécuter,
  dans un conteneur jetable avec le runtime 5c1 et les outils de séquence de la flotte M,
  `sante-courrier.py` et `actif.py` en mode à blanc ou `--help`, plus les imports.
  Cette qualification n'est **pas** faite dans ce dossier.
- **mesh-design tranche** : P6 obligatoire avant C3 (impl-11), ou repli 5c1 qualifié admis (impl-03, plus la qualification ciblée).

## 2. P6 : préparation (feu #200708), rien de posé

**(a) Kit scellé sur M = `97285a076dee`.**
- Construit sur BG le 08/11 de 12:24 à 12:27Z, dans `ameesh-l2-builder:p6-97285a0`.
  - Ce conteneur vient du `Dockerfile.builder` du kit 5c1, inchangé (`a53443ae`) : ubuntu `@sha256:fe81…`, `python3.12=3.12.3-1ubuntu0.17`.
  - La python du conteneur vaut `e50d468e` = la cible.
  - Réseau coupé, `--cap-drop ALL`, uid 1000.
- **Enveloppe** : `build-sealed-package.py` et `verify-sealed-package.py` du kit 5c1. Seul le SHA
  vient de `KIT_SHA` (40 hex vérifiés) ; la roue setuptools et le nom du venv sont dérivés. Diff
  dans la branche.
- **Lock** : `pins-lock.py` (runtime-pins + runtime-build-pins, une ligne par épingle, tri stable).
  La règle **reproduit le lock de 5c1 à l'octet**. Celui de M ajoute `coverage==7.17.2` et `pyyaml==6.0.3` (#6291).
- **Roues** : `pip download --require-hashes --only-binary=:all:`. Les 5 roues communes sont
  identiques à l'octet à celles du kit 5c1 d'AG2.
- **Résultats** :
  - deux manifestes `produire --scelle` identiques ;
  - venv scellé vérifié ; `pip install` refusé ;
  - vérificateur séparé dans un conteneur neuf : rc 0.

| Pièce | sha256 |
|---|---|
| Archive `L2-review-97285a07-20261108.tar.gz` (13,5 Mo, sur BG : `scratchpad/p6/`) | `4143455e87bb07f32398efd5df49df3ca274c3a35e78e51b504f49fcf6784a57` |
| `SHA256SUMS` du kit (74 lignes) | `68deab6eea3e7c83be4444128d5ed0db703493884829a1ea0a4e6207d2e2dea6` |
| `manifest.json` | `1ec6e407733f55e9bf03071779481b7cca1ec3237fdf579700928580ef2f07ed` |
| `requirements-staging.lock` | `185d3bef81efd5c35b385b8705ca2b8885064ad3a3781e6b3c63bf12185cf039` |
| Roue `ameesh-1.44.1-py3-none-any.whl` | `d3e5d21a2dd43ce6a97e916099f4630e5c6efafdc99e60b8cfa168e8d933e773` |
| Arbre du paquet installé (`_tree_sha256`) | `2dca0d97ba27576a6c6c624c711e8f3588c7f0cc87b2f20386dbf45636e8a633` |
| Inventaire source | 3076 fichiers = git à M (mode, blob, sha256) |

**Source.** M = fusion de la PR #4869 (paquet-h-20261108-12). Arbre `c48d833b` = arbre de sa tête
`d6a518f7` : 59 checks SUCCESS, 5 SKIPPED. Staging attesté : `actif-97285a076dee.json`, 3 VM.

**Écart nommé.** L'image du constructeur a été reconstruite sur BG (id `900a31fc…` ≠ `50c8464f`
du kit 5c1 ; couches horodatées). Base et paquets sont épinglés de même, et la python est identique.

**(b) Requalification par l'auditeur.** Paquet à lui soumettre :
- l'archive ci-dessus et son `.sha256` ;
- la branche (`docs/reviews/5495-c3/p6/` : reçus, enveloppe, lock, `pins-lock.py`).

Ce qu'il recalcule :
- l'inventaire = git à M ;
- `SHA256SUMS` ;
- roues = épingles de M ;
- scripts du kit = blobs de M (hors enveloppe) ;
- absence de pin ;
- arbre `2dca0d97`.

**(c) Installateur v3 : `update-l2-runtime-v3.py`.** C'est une **mise à jour**, pas une première
installation. Il dérive de la phase 1 V2 (`465180850fb1d24d`) et en garde les gardes :
- autorité exacte, fraîche (moins de 600 s), avec l'empreinte du contrôleur ;
- avis codex et deepseek par identifiants de courrier, plus l'identifiant de la requalification de l'auditeur ;
- extraction sûre ;
- commandes sous 980 en environnement vide ;
- refus constant sans trace.

Étapes :
1. L2 doit être disabled/inactive avant, pendant et après.
2. `venv-courant` doit valoir `venv-5c1e679`.
3. Fetch public et checkout de M dans le clone dédié, puis inventaire à l'octet.
4. `bascule.py preparer` verrouillé, puis `activer --version 97285a0 --precedent 5c1e679`.
5. Vérificateur, puis arbre `2dca0d97`.
6. `expected_sha256` réécrit dans la copie 980 **et** `/etc/ameesh/staging-submit.json` (octets
   identiques). Seuls les chemins du venv changent ; python `e50d468e` et `quiescence` `f988ee7a`
   sont inchangés.

`--retour` : `bascule.py retour` (`venv-courant` revient à `venv-5c1e679`, sans pip), puis les deux
configs d'avant sont remises depuis les reçus.

**Essai complet sur BG**, conteneur du constructeur, réseau coupé : `preparer` et `activer` 5c1,
puis `preparer` et `activer` M (`--precedent 5c1e679`), vérificateur M OK, arbre `2dca0d97`, puis
`retour` → `venv-5c1e679`, arbre `0d38f1e7`.

**Avis requis** sur l'octet `update-l2-runtime-v3.py` (`1007f986541e24ec…`). Deux familles hors
claude : codex et deepseek.

**(d) Pose à T-60 le 09/11, sur le feu C3.** Pour chaque hôte :
1. Dépôt de l'archive (root 0600) et de l'autorité.
2. `python3.12 -I update-l2-runtime-v3.py --apply`.
3. Puis `pose-c3-v21.sh prealables <h> … --config-avant <config_after_sha256 du reçu runtime-aligned.json>`.

La sonde déduit le venv attendu, la borne de migrations et les arbres : aucune valeur n'est à
changer dans les scripts C3.

## 3. Séquence (09/11, coupure de 10:00Z)

1. Avant 06:00Z : re-gel de v2.1 et avis. Avant 08:00Z : requalification (b) et avis sur (c).
2. **T-60 (09:00Z)** :
   - P6 (d) sur AG1 puis AG2, si le pilote le retient ;
   - `preparer-canon-c3.sh <HEAD des agents>` ;
   - `pose-c3-v21.sh prealables AG1|AG2` (à blanc, puis `--apply`) ; chaque pose se termine par la sonde en garde dure.
3. **T-15** : `sonde-c3-v21.sh go`. S'il est KO : NO-GO, BG porte, rien n'est activé.
4. **GO** : `pose-c3-v21.sh --apply activer`, puis sonde `actif`. Le fichier `c3-activation-<TS>.env` garde DEPUIS et CANON.
5. **Après la séquence** : `sonde-c3-v21.sh apres --sha <release_sha de 10:00Z> --depuis <DEPUIS> --canon <CANON>`. Attestation de l'auditeur.
6. Les minuteries restent actives (Q5). Le staging manuel du bureau ne cesse qu'après **deux** C3 attestés de suite.
7. **Retour** : `retour-c3-v21.sh --apply activation <TS>`. Si une séquence tourne, rc 3 et l'unité n'est pas tuée. Puis BG relance la coupure.

## 4. O5 : copie `.pgpass` d'AG2 et rotation du rôle `ameesh`

**Fait.** `~ameesh-staging/.pgpass` d'AG2 (980 0600, 62 o, cible `10.77.0.1:5432:ameesh:ameesh`)
est une **copie** du secret du rôle `ameesh`, posée le 04/11 (pose passive relue, #190531).

AG1 n'a pas de copie : son mot de passe est résolu à chaque démarrage depuis le `.pgpass` de
1001 (phase 2A V6). Une rotation du rôle casserait donc **L2 sur AG2 seul**, en silence, jusqu'au
prochain tick.

**Procédure de rotation du rôle `ameesh`** (ajout proposé) :
1. Avant la rotation : `systemctl --user -M ameesh-staging@ disable --now ameesh-staging.timer`
   sur AG2, si aucune séquence ne tourne.
2. Après la rotation : réécrire `~ameesh-staging/.pgpass` d'AG2 (980 0600, `O_EXCL` puis
   renommage), **sans afficher le secret**, depuis la même source que les `.pgpass` des agents.
   Puis vérifier `sonde-c3-v21.sh prealables --hotes AG2`, ainsi qu'une connexion en lecture
   sous 980.
3. Réactiver la minuterie si elle l'était.
4. Le lot de suite promis par le pilote (parité du résolveur sur AG2, sans copie) supprimera cette étape.

**Texte proposé pour `hotes/ameesh-agents-2.md`** (canon : à écrire par un agent runner, fusion
par le bouton, cf. règle du bureau) :

> ### Exécuteur élu L2 (compte `ameesh-staging`, uid 980) — secret de base copié
> Depuis la pose passive du 04/11 (#5495, #190531), `~ameesh-staging/.pgpass` (980:0600) contient
> une **copie** du mot de passe du rôle PostgreSQL `ameesh` (cible `10.77.0.1:5432`). AG1 n'a pas
> de copie (résolveur phase 2A V6). **Toute rotation du rôle `ameesh` doit mettre à jour cette
> copie** : désactiver d'abord `ameesh-staging.timer` d'AG2 (sans séquence en cours), réécrire le
> fichier sans afficher le secret, vérifier la sonde C3 `prealables --hotes AG2` et une connexion
> en lecture sous 980, puis réactiver la minuterie. Un lot de parité du résolveur (ouvert par
> mesh-design après le premier C3 attesté) supprimera cette copie.

## 5. Pièces v2.1 (docs/reviews/5495-c3/)

| Fichier | Rôle |
|---|---|
| `DOSSIER-C3-v21.md` | Ce dossier |
| `sonde-c3-v21.sh` | Sonde en garde dure : `prealables`, `go`, `actif`, `apres` |
| `lecteur-c3.py` | Lecteur de la base, lecture seule (porté sur AG2 par la sonde, sur l'entrée standard) |
| `evaluer-c3.py` | Jugement après C3 (sans réseau) |
| `temoin-apres.sh` | Témoin d'impl-11 et 13 variantes (local) |
| `pose-c3-v21.sh`, `retour-c3-v21.sh` | Pose et retour v2.1 |
| `releve-v21-*.txt` | Relevés et blancs datés de v2.1 |
| `p6/` | Enveloppe paramétrée, `pins-lock.py`, `update-l2-runtime-v3.py`, reçus du kit (`SHA256SUMS`, `build-receipt.json`, `manifest.json`, locks, provenance, portée, vérificateur séparé, préparation), essai bascule |
| `MANIFEST.json` | sha256 de chaque pièce, v1, v2 et v2.1 |

## 6. Points ouverts

- **O1 (pilote).** Q4 : P6 avant C3 (impl-11), ou repli 5c1 avec qualification ciblée (impl-03, plus §1). À trancher.
- **O2 (auditeur).** Requalification de l'archive `4143455e…` du kit P6. Paquet décrit au §2(b).
- **O3 (relecteurs codex et deepseek).** Avis sur l'installateur v3 (`1007f986…`), à rendre avant 08:00Z.
- **O4 (canon).** Paragraphe de rotation pour `hotes/ameesh-agents-2.md`, à faire écrire par un runner.
- **O5.** `lecteur-c3.py` s'exécute sur AG2 sous ameesh, avec le venv des agents. C'est une lecture
  pure (`writer=False`, aucun `read_attestation`). À confirmer par les relecteurs.
