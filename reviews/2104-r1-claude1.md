# #2104 — R1 Claude1 (harnais DeepSeek)

Gel : `68df8f01324812a202f7914e69152fb9da28cc87` — branche
`lot/2104-radar-conflict-attribution`, cible `main`, PR #1512.
**GO : 0 B / 0 N.**

## Périmètre relu

7 fichiers, +465/−20 : `src/ameesh/radar.py` (attribution des paires, états,
rendu, courrier), `src/ameesh/radar_attribution.py` (nouveau : lecture de
`merge-tree --messages`, classification), `src/ameesh/resolutions.py`
(sélection du responsable, replay), `src/ameesh/merge_queue.py`
(`UNPREPARED`), `docs/RADAR-ATTRIBUTION.md`, `docs/EXPLOITATION.md`,
`tests/test_2104_radar_attribution.py`.

Auteur `ameesh-cx3` (Codex) : avis d'une autre famille. Classe « sensible hors
SQL/secrets/paiement/prod réelle » notée au gel : un seul avis requis.

## Vérifications

- **Preuve d'héritage** : `classify` exige le même groupe de chemins *et* les
  mêmes entrées d'index (stages, modes, objets) dans une fusion témoin, plus au
  moins un chemin stagé ; un simple nom commun ne prouve rien (témoin
  `same_filename` → `unclassified`, `inherited_files=[]`). Un renommage garde
  son groupe entier (`rename_rename`, `inherited_rename`) ; l'union des groupes
  est transitive (relue).
- **Propre / hérité / incertain** : `native` seulement si *aucune* fusion témoin
  ne conflit sur le groupe ; toute lecture illisible ou tout contenu qui change
  → `uncertain`, jamais « propre ». Bases identiques → la fusion brute est
  l'interaction propre (`radar.py` l.831-834) ; `target_head` différents →
  incertain.
- **Conséquences** : `target-conflict` et `superseded` ne reçoivent ni courrier
  de paire ni responsable ; `unclassified` reste visible et signalé sans
  responsable ; une paire mixte garde `files` = groupes propres, affiche
  hérités/incertains et `attribution_pending`, sans responsable — la commande de
  préparation fusionnerait les têtes brutes, conformément au corps du lot.
  Conflits avec la cible : toujours éjectés par `merge_queue.py` l.848-870, avec
  identité tip/gel/cible vérifiée.
- **File** : `UNPREPARED` inclut `unclassified` → classée non préparée, aucun
  responsable ; `superseded`/`target-conflict` n'attendent pas (le conflit avec
  la cible est suivi à part) ; préparations et replays existants conservés.
- **Cache** : clé (tête cible, têtes, bases) ; un mouvement de la cible
  re-sonde ; `trim` couvre la nouvelle table. Classification indépendante de
  l'ordre des lots (témoin dédié).
- **Robustesse de lecture** : `read_merge` borne ses indices, attrape
  `ValueError`/`IndexError`, rend `None` si `rc ∉ {0,1}`, si la sortie est
  incohérente ou si `rc=1` sans groupe → incertitude, jamais « propre ».
- **Aucune écriture** : `show-ref` et `count-objects` inchangés, pas de
  `rr-cache` (témoin) ; aucun rebase, aucune branche réécrite.

## Preuves

- `PYTHONPATH=src python -m unittest tests.test_2104_radar_attribution` : **13/13**.
- Modules touchés : `test_l160b_radar`, `test_l160e_merge_queue`,
  `test_l160d_resolutions` : **98 OK** (1 saut) ; `test_l160c_radar_mail`,
  `test_l160e2_land`, `test_1333_radar_cible` : **94 OK** — 205 verts, 1 saut.
- `git diff --check` propre ; code relu = arbre exact du gel.
- Aucune suite complète, aucune CI de lot (§3). Le cas réel #2022 n'est pas
  rejoué ; ses motifs (hérité hors delta, propre, mixte, renommage) sont couverts
  par les témoins du lot.

## Limites

Aucun bloquant ni non bloquant relevé. Le choix « paire mixte sans responsable »
et la re-sonde subordonnée à un mouvement (tête ou cible) sont documentés dans
`docs/RADAR-ATTRIBUTION.md` et conformes au corps du lot.

Désignation : `claude1` ne peut pas se désigner (#705, autorité pilote) ;
l'orchestrateur est saisi pour poser le champ `reviewer`.
