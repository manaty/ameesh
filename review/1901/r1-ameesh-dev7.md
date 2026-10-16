# #1901 — R1 Claude

16/10/2026, ameesh-dev7 (Claude). Mission : orchestrateur, courrier #88178 du 16/10 à 21:08 (avis Claude, classe sensible prod). Tête relue [a6b478a025f0e7105d192bba376865567bd5e0df](https://github.com/manaty/ameesh/commit/a6b478a025f0e7105d192bba376865567bd5e0df) : le lot de deepseek4 (d24489ae7) **et** la correction de codex1 (R1 Codex, [rapport](https://github.com/manaty/ameesh/blob/dae78fc37/review/1901/r1-codex1.md)). Six fichiers, base dffe30ea.

**Verdict : 0 B / 0 N bloquant ; favorable à la fusion sur a6b478a0.** Aucune correction poussée.

## Ce qui a été vérifié

- **Chaîne complète.** `FileRegistry._load` → `error` (absent exclu par le `stat` préalable) → `availability.local_alerts` nomme `GATE_FAMILY` → `exploitation._gate_alerts` (l'`except (OSError, ValueError)` nomme aussi la famille, sans doublon) → `Notifier.run_pass` passe la même liste à `alert_ack.reconcile` et à la boucle de résolution. Les autres appelants de `exploitation.alerts` (`alert_ack.current`, vues) ne passent pas de liste : comportement inchangé.
- **Garde de `reconcile` sur `observation_host is not None`.** Seule `_gate_alerts` pose `observation_host` (exploitation.py:1945), et elle le pose depuis l'introduction même de la colonne (a258a463, 15/10) : aucune ligne d'acquittement de la porte n'a d'hôte nul. La garde ne touche donc que la porte, et l'hôte observé est déjà filtré la ligne d'avant.
- **Correction codex1 (1) — état notify antérieur.** `notify.key_of` ne dépend pas d'`observation_host`, la clé d'état reste stable ; la garde bornée à `ALERT_TYPES` et `observer in (None, cfg.host)` couvre l'état antérieur sans élargir à d'autres familles (l'état de notify est local à l'hôte). `observation_host` ajouté à `_KEPT` : correct.
- **Correction codex1 (2) — `stat`.** Seul `FileNotFoundError` vaut absence ; `PermissionError` remonte à `_gate_alerts`, qui nomme la famille. Correct.
- **Direction de l'erreur.** Tous les nouveaux chemins ratent du côté « pas de résolution » (une course `stat` → `open` sur un registre supprimé coûte au pire une passe sans résolution) : sens sûr.
- **Témoins.** `tests.test_1901_gate_unreadable` couvre absent / illisible / schéma inconnu / lisible / répertoire inaccessible / `except` de `_gate_alerts`, la garde de `reconcile` (incomplet, complet, autre famille, autre hôte) et le bout en bout avec état antérieur, y compris la résolution effective quand le registre se relit.

Tests ciblés (unittest, banc PG local) : `tests.test_1901_gate_unreadable tests.test_l87c_alert_ack tests.test_l112_host_gate` → **66 verts** (201 s). Pas de suite complète, aucune CI déclenchée.

## Non bloquants (backlog, hors périmètre du lot)

- **N1 — un registre illisible ne se signale pas.** Avant #1901, l'alerte se résolvait à tort ; désormais elle reste active, mais silencieusement : rien ne dit à l'opérateur que la porte n'est plus lue. Une alerte dédiée (p. ex. `host_gate_unreadable`, observée par l'hôte) fermerait la boucle.
- **N2 — `FileRegistry.put` sur un registre illisible l'écrase** (préexistant) : `_load` rend `{}`, et l'écriture suivante ne garde que l'hôte écrit. `error` est maintenant disponible pour refuser l'écriture ou mettre le fichier de côté.
- **N3 — une ligne d'hôte malformée** (`body` non dict) lève `AttributeError` dans `availability.alerts`, hors de l'`except` de `_gate_alerts` : toute la passe échoue (préexistant ; sens sûr, mais bruyant).
