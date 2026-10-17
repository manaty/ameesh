# #2180 — R1 Claude1 (harnais DeepSeek)

Gel : `731e25fc9b6f20f80508fe34d9d78a80ea9225f0` — branche
`lot/2180-housekeeping-discovery`, cible `main`, PR #1570.
**GO : 0 B / 0 N.**

## Périmètre relu

5 fichiers, +411/−7 : `src/ameesh/housekeeping.py` (dérivation des racines,
garde de réutilisation, branchement de la découverte), `src/ameesh/
housekeeping_discovery.py` (nouveau, 167 l.), `src/ameesh/storage/interface.py`
(docstring `register_worktree`), `docs/EXPLOITATION.md`,
`tests/test_2180_housekeeping_discovery.py` (189 l.).

Auteur `ameesh-cx3` (Codex) : avis d'une autre famille (DeepSeek), indépendant.
Lot hors SQL/secrets/paiement/prod réelle.

## Vérifications

- **Périmètre** : seuls les worktrees liés des dépôts des agents **de cet
  hôte** sont adoptés (`agents` filtré par `host`), et seulement si le chemin
  porte un marqueur `.git` **fichier** régulier, non lien, canonique
  (`_marker`) ; worktree principal (index 0), clones, dossiers sans `.git` et
  liens ne sont jamais adoptés — seulement signalés (`garde`), sans
  suppression.
- **Bornes** : `DISCOVERY_LIMIT=2000` et `BUDGET_S=30` ; `skipped` dit
  l'inventaire incomplet ; `list_worktrees` n'est appelé qu'une fois par dépôt
  (deux agents de même `cwd` partagent la liste, témoin dédié).
- **Fraîcheur / réutilisation** : un chemin `gone`/`removed` reçoit une
  nouvelle inscription (lot et branche actuels, `turn_id=None`,
  `ended_ts=None`, âge neuf), sans reprise de l'ancien lot (témoin sur les deux
  terminaux) ; le passage suivant le garde `attend` (âge <
  `ORPHAN_WORKTREE_IDLE_S`).
- **Attribution** : lot par la branche (`cited_lots`), puis par le nom du
  dossier (`named_lot`), puis repli « non attribué » avec témoin du dépôt ; le
  lot n'est retenu que si son assigné est un agent local. `HEAD` et branche
  sont relus **après** la liste (un checkout survenu entre-temps est attribué
  sur les références courantes, témoin dédié).
- **Sûreté de la suppression** : la découverte n'écrit que des inscriptions et
  des signaux ; le retrait éventuel repasse par les gardes existantes
  (`worktree_state` : propre, poussé **et intégré**, non verrouillé, aucun
  processus dans le dossier, âge orphelin 24 h). Témoin : un worktree découvert
  avec un commit non poussé est `kept` ; un worktree propre est retiré et sa
  branche conservée.
- **Garde #2180 dans `_worktree_pass`** : un chemin suivi qui n'est plus un
  worktree lié du dépôt enregistré (`_marker != linked` ou `common_dir` ≠
  `repo`) est `garde` — jamais retiré. Placée **après** le contrôle
  d'existence (`os.path.isdir` → `disparu`/`gone`) et le contrôle d'âge, donc
  un dossier disparu garde son cycle ; un clone ou un lien réutilisé au même
  chemin est conservé (témoin dédié).
- **Lecture seule de la découverte** : `dry_run` n'écrit ni inscription ni log
  (témoin : base et `recent()` vides) ; aucune suppression, aucun parcours
  récursif, aucun suivi de lien.
- **Docs** : `docs/EXPLOITATION.md` décrit la découverte, le cycle neuf, les
  bornes, les signaux et la garde de réutilisation — conforme au code.

## Preuves

- `PYTHONPATH=src python -m unittest tests.test_2180_housekeeping_discovery`
  (Postgres de test `127.0.0.1:55432`) : **8/8**.
- Voisins du module : `tests.test_l73_housekeeping` : **41/41** (un
  `ResourceWarning` préexistant sur une fermeture de fichier, hors delta).
- `git diff --check` propre ; code relu = arbre exact du gel ; PR #1570 (le
  lien 1566 de la note de gel est corrigé au journal).
- Aucune suite complète, aucune CI de lot (§3).

## Limites

Aucun bloquant ni non bloquant relevé. Choix documentés que je ne compte pas
comme défauts : les racines d'inventaire se déduisent du préfixe avant
`{agent}` de `work_root`/`work_roots` (un suffixe après `{agent}` élargit la
racine au parent — l'inventaire ne fait que signaler) ; la découverte ne tourne
qu'aux passages avec relevé d'artefacts (`artifact_scan`, au plus toutes les
`SCAN_EVERY_S`).

Désignation : `claude1` ne peut pas se désigner (#705, autorité pilote) ;
l'orchestrateur est saisi pour poser le champ `reviewer` et le jalon verdict.
