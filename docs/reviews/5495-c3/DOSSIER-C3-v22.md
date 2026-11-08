# #5495 — dossier C3 v2.2 : delta de v2.1

Bureau (BG), 2026-11-08, vers 12:55Z. Branche `review/bureau-5495-c3`. Les versions v1, v2 et
v2.1 sont conservées. **AUCUN GESTE.** Seuls des relevés en lecture et des essais à blanc ont
tourné ; tous les témoins sont locaux (faux `ssh` et faux `systemctl` dans le PATH).

## 0. Décisions et avis reçus depuis v2.1

- **Q4 (pilote, #200777 (2)) : P6 doit précéder C3. Il n'y a plus de repli sur 5c1.** Le
  §1 de v2.1 (« repli 5c1 avec qualification ciblée ») est **retiré**.
  - Si l'un de ces deux éléments n'est pas rendu le 09/11 à 08:00Z, il n'y a **pas de C3 à 10:00Z** :
    - la requalification par l'auditeur (b) ;
    - les avis sur l'installateur (c).
  - Dans ce cas, BG porte la coupure et le C3 glisse au créneau suivant.
- **P6 (b) CONFORME** (auditeur #200778), sur l'archive `4143455e…` déposée sur AG1. Il a
  recalculé :
  - l'inventaire (3076 fichiers = git à M) ;
  - SHA256SUMS ;
  - roues = épingles ;
  - 57 scripts = blobs de M ;
  - l'arbre `2dca0d97`.

  Un écart de forme est corrigé ici : le `SHA256SUMS` du kit compte **75** lignes, pas 74. Le
  74 de v2.1 venait d'un comptage fait avant l'ajout de `preparation-receipt.json`.
- **Installateur v3 (c)** : avis **OK** d'impl-11 (#200781, codex) et OK statique avec N d'impl-03
  (#200802, deepseek). Limite de preuve d'impl-11
  explicite : l'essai fourni exerce `bascule preparer/activer/retour`, **pas le contrôleur
  complet** (autorité, extraction, inventaire, réécriture des configurations sous root).

## 1. Avis B d'impl-11 sur v2.1 (#200781), corrigés

| Avis | Défaut | v2.2 | Témoin |
|---|---|---|---|
| **B1** | `lecteur-c3.py` jetait l'horodatage des attestations ; `evaluer-c3.py` acceptait n'importe quelle attestation du même SHA | Le lecteur garde l'horodatage de chaque événement. En base, c'est la colonne `ts` d'`ameesh_events`, vérifiée sur données réelles : l'attestation `97285a07` est à 11:52:32Z. **Correctif lié** : les enregistrements de demande lisaient `created_at`, absent de ces lignes, et auraient été vides ; ils lisent désormais `ts`. Le juge exige une attestation du SHA horodatée **dans la fenêtre du run** (après son démarrage, au plus tard la marge après son état terminal) **et du même run** si le champ `run` existe. Aujourd'hui l'attestation ne porte pas ce champ, donc le rattachement est temporel | `temoin-apres.sh` : « ancienne attestation même SHA » (horodatage 1, run ancien) → **KO** ; attestation après la fenêtre → KO ; d'un autre run → KO ; du même run → OK. Plus les 14 témoins de v2.1, inchangés |
| **B2** | `if systemctl … \| grep` et `list-jobs …; true` changeaient un échec de lecture en « sans » ou « 0 » | Plus aucune commande distante ne masque son code de retour. `execstart()` et `jobs_release()` capturent la sortie avec `lire` (rc contrôlé), puis l'examinent sur BG ; une erreur de lecture devient un KO. Même principe dans `pose-c3-v22.sh` (`list-jobs` lu séparément ; `UnitFileState` remplace `is-enabled \|\| true`) | `temoin-lectures.sh` : avec un faux `systemctl` en rc 1, **v2.1 rend bien « OK … (sans) »** (défaut reproduit), alors que **v2.2 rend KO** pour ExecStart et ERREUR-LECTURE pour list-jobs |
| **B3** | Le retour exigeait les reçus des deux hôtes avant de restaurer le premier | (a) `activer` crée les **reçus des deux hôtes** avant toute mutation (boucle 0 : état de la minuterie release et empreinte du drop-in, sans aucun geste d'unité). (b) `retour-c3-v22.sh activation` traite **chaque hôte réellement modifié**, c'est-à-dire celui qui a des reçus. Un hôte sans reçus n'est jamais touché : on vérifie seulement qu'il est intact (drop-in présent, minuterie L2 disabled, pas de soumission). Sinon c'est un **incident, rc 4**, après la restauration complète de l'autre | `temoin-lectures.sh` : activation partielle (reçus sur AG1 seulement) → gestes de retour sur AG1 seul, AG2 « intact », rc 0. AG2 sans reçus mais modifié → INCIDENT, rc 4, AG1 traité, aucun geste sur AG2 |

## 1 bis. Avis B d'impl-03 sur v2.1 (#200802), intégrés dans v2.2

| Avis | Défaut | v2.2 |
|---|---|---|
| **B1** fenêtre de release | `…; systemctl stop` masquait un échec de la sauvegarde d'état ; `if start; fi; show` masquait un échec du démarrage ; le retour ne redémarrait pas sans fichier d'état ; `go`/`actif` ne vérifiaient pas la minuterie release | Sauvegarde d'état en `&&`, avec 2 lignes vérifiées dans la boucle 0, **avant** tout arrêt ; arrêt en geste séparé (échec = STOP). Restauration : `start && ActiveState=active` exigé (pose et retour). Retour sans fichier d'état lisible : la minuterie release est redémarrée (état nominal sur les candidats), le fait est signalé et un état par défaut écrit dans les reçus. Case « minuterie release active » ajoutée à `go` et `actif` |
| **B2** canon absent | Si le premier `mv` avait réussi et le second échoué, le retour refusait au lieu de remettre le canon conservé | `retour prealables` : canon absent **et** `$RET` présent avec HEAD = AVANT → `$RET` remis, puis HEAD vérifié. Sinon : STOP, rien touché. Témoin `temoin-lectures.sh` « canon absent → canon conservé remis » |
| N | `vexp` prenait `sorted(...)[0]` si plusieurs venv étaient nommés | Exactement un venv est exigé dans `expected_sha256`, sinon KO (`PLUSIEURS-VENV`) |
| N | Attestation non bornée par `depuis` ; drains non liés à l'hôte du run | La première est couverte par B1 d'impl-11 (fenêtre du run). Les drains portent sur les agents des **cibles** (AG1, AG2, AG3), pas sur l'exécuteur : le lien se fait par la fenêtre du run et l'acteur `ameesh:staging` (limite déclarée) |
| N | Retour : arrêt de la release avant vérification du service | En v2.2, l'ordre est : minuterie L2 désactivée → service L2 vérifié inactif (sinon rc 3) → seulement ensuite, fenêtre release fermée |
| N | Dossier « 74 lignes » | Corrigé : 75 |

**Installateur P6, v3.1** (`p6/update-l2-runtime-v31.py`, sha `f838ee6b…`, diff
`p6/update-l2-runtime-v3-v31.diff`, 50 lignes). Il porte les N d'impl-03 sur v3, dont l'avis est
**OK (statique) avec N** :
- `--retour` vérifie l'arbre 5c1 (`0d38f1e7`) après `bascule retour` ;
- le venv M et le kit restent sur place (documenté) ;
- le temporaire de `staging-submit.json` est créé en `O_EXCL` ;
- toute exception donne un refus constant (nom du type seulement), pas seulement `AssertionError`.

Les N « autorité ne contrôle que les clés et identifiants des avis » et « extraction en chmod
644 » sont gardés tels quels : même comportement que la phase 1 relue. **Les deux avis OK (impl-11
sur v3, impl-03 sur v3) portent sur `1007f986…`.** v3.1 est un delta de 50 lignes à confirmer par
les deux relecteurs ; sans confirmation à 08:00Z, c'est v3 (`1007f986…`), relu OK, qui sera posé.

## 2. Séquence (remplace v2.1 §3, points 1-2)

1. **Avant le 09/11 06:00Z** : re-gel de v2.2, puis avis.
2. **Avant 08:00Z** : l'installateur v3 a déjà les deux avis OK (impl-11 #200781, impl-03 #200802) ;
   la requalification de l'auditeur est CONFORME. Confirmation du delta v3.1 demandée ; sinon v3 est posé.
3. **T-60** :
   - P6 (d) sur AG1 puis AG2, **obligatoire** ;
   - `preparer-canon-c3.sh` ;
   - `pose-c3-v22.sh prealables AG1|AG2 … --config-avant <config_after_sha256 du reçu runtime-aligned.json>`.
   - La sonde attend `venv-97285a0` et accepte les migrations jusqu'à 0555, l'un et l'autre lus dans le runtime.
4. Les étapes T-15 → retour sont celles de v2.1, avec les scripts `*-v22.sh`.

## 3. Pièces v2.2

| Fichier | Rôle |
|---|---|
| `DOSSIER-C3-v22.md` | Ce delta |
| `sonde-c3-v22.sh`, `pose-c3-v22.sh`, `retour-c3-v22.sh` | Corrigés (B2, B3) |
| `lecteur-c3.py`, `evaluer-c3.py` | Corrigés (B1, horodatage `ts`) ; même nom, l'historique git garde v2.1 |
| `temoin-apres.sh` | Témoins B1 en plus, 18 au total |
| `temoin-lectures.sh` | Nouveaux témoins B2, B3 et canon absent (impl-03 B2) |
| `p6/update-l2-runtime-v31.py`, `p6/update-l2-runtime-v3-v31.diff` | Installateur v3.1 (N d'impl-03) et son diff |
| `releve-v22-*.txt` | Sonde `go` réelle et essais à blanc. Les cases propres à `go` sont OK : release v1.44.1 attestée, détenteur AG2, tick de release inactif sans job. Restent KO, comme attendu, les préalables non posés |
| `MANIFEST.json` | sha256 de toutes les pièces |

## 4. Points ouverts

- **O1.** Confirmation du delta v3.1 de l'installateur (`f838ee6b…`) par impl-11 et impl-03. À défaut, v3 `1007f986…` (deux OK) est posé.
- **O2.** Re-gel de v2.2 et avis sur B1-B3.
- **O3.** Si l'attestation gagne un jour un champ `run`, le juge l'exige déjà ; aujourd'hui le rattachement est temporel.
