# #1110 / PR845 — R1 Codex

Auditeur, harnais réel Codex, indépendant de l'auteur ameesh-dev5 (DeepSeek). Tête exacte `b1e0739fb07f8894fbbaa0a23f90055ccbb607b9`, base `b85777c1e3e757b9ba0caa8cef49b0db1558b251`. Dix fichiers, +449/−62, migration 0433, contrat du superviseur, collecteur/stockage CI et configuration des workflows.

**NO-GO — deux P2 bloquants dans la classe sensible ameesh, un N/P3.** Aucune suite rejouée, aucune CI ou configuration modifiée. Trois témoins isolés ont exécuté les fonctions de cette tête ; le témoin SQL utilise une CTE VALUES en lecture seule, qui masque la table réelle, sans aucune écriture ni donnée produit lue.

## P2-1 — une exécution terminée avant le premier passage reste invisible

`ci.py:397–428` n'énumère que `in_progress` et `queued` (`LIVE_STATUSES`, ligne74), puis ne cherche un résultat terminal que pour les lignes déjà `running` en base. Un workflow Nexlink court qui commence et finit entre deux passages n'est donc jamais enregistré : ni verdict ni réveil. La cadence GitHub est de 300s par défaut, 60s minimum (`plan_github.py`). Le correctif découvre les bons noms de workflows, mais ne résout pas ce cas de réveil perdu.

Témoin sur `Watcher.tick` réel : dépôt découvrant `sql-invariant.yml`, un run42 de la branche d'un lot déjà terminal côté API, aucune ligne de suivi préalable. Les seuls appels sont la découverte et les listes `in_progress`/`queued` ; zéro `open_run`, zéro événement. Le résultat terminal disponible n'est jamais demandé. Le problème existait dans le collecteur à un seul workflow ; il reste directement sur le chemin de fin CI que #1110 promet de restaurer, sans lot de correction distinct trouvé au relevé.

Inclure un rattrapage borné des exécutions récemment terminées ou une collecte équivalente permettant le premier enregistrement terminal, avec contrôle tête/attempt et déduplication. Couvrir : run court terminé avant le premier passage → un verdict/réveil, passage suivant → aucun doublon, ancienne tête → aucun verdict périmé. Coordonner avec #901 sans attribuer à ce lot une garantie d'attempt qu'il n'implémente pas encore.

## P2-2 — un workflow vert masque celui qui a échoué sur main

`storage/postgres/ci.py:153–161`, `latest_concluded`, conserve **une ligne par `(repo,branch)`**, sans distinguer workflow ni état de l'ensemble d'une tête. `ci.main_red` (`ci.py:571–581`) ne voit que cette ligne. Avec le nouveau suivi multi-workflow, un succès d'une suite secondaire conclu après l'échec backend fait disparaître l'alerte `ci_main_red`, bien que la tête soit toujours rouge. Cette alerte est documentée comme bloquant les déploiements ; nommer le workflow dans le message ne corrige pas sa sélection.

Témoin : deux runs de **la même tête et de main**, backend-suite failure à T−1min puis scripts-suite success à T. La requête **réelle** de `CiResults.latest_concluded` exécutée sur les deux lignes d'une CTE VALUES renvoie seulement scripts-suite success ; appel réel `ci.main_red` → `[]`. L'échec backend est masqué. Adapter la sélection/agrégation pour qu'un succès d'un autre workflow ne fasse pas disparaître un échec applicable à la tête surveillée. Le rouge hérité `base_failures` utilise lui aussi la dernière exécution globale du dépôt/branche : requalifier sa référence par workflow dans le même traitement pour éviter une comparaison entre suites étrangères.

## N/P3 — autres workflows d'une autre tête annoncés comme appartenant à cette tête

`ci.py:426–428` construit `pending` avec toutes les exécutions du dépôt encore en cours, sans filtre de tête ou branche. Témoin sur `verdict_text` : verdict backend de `agent/A`, tête aaaa, et website en cours sur `agent/B`, tête bbbb → texte « Other workflow(s) of this head are still running: website-suite.yml ». Filtrer les lignes comparables (dépôt, tête et contexte de branche/lot) et couvrir deux lots indépendants. Cela n'affaiblit pas un check GitHub ; c'est une erreur d'information, non bloquante P3.

## Points examinés

- Migration 0433 additive : `workflow text NOT NULL DEFAULT ''`, clé `(repo,run_id)` intacte, aucun SECURITY DEFINER nouveau. Contrat des colonnes superviseur mis à jour ; seule métadonnée workflow ajoutée aux colonnes admises.
- Liste déclarée de workflows validée et dédupliquée ; forme courte préservée. Découverte des workflows actifs en cache, erreur API journalisée et délai de reprise ; défaut ameesh `tests.yml` conservé. Stockage du nom et transport dans les verdicts cohérents.
- Les 31 témoins deux pilotes et 15 témoins de contrat sont annoncés par l'auteur/pilote, pas réexécutés ici. `git diff --check` vert.
- La gestion complète des relances d'un même run reste celle de #901, encore en qa : le nouveau témoin d'affichage attempt2 n'est pas une preuve de réouverture d'un run déjà conclu. Aucun nouveau blocage autonome sur ce défaut déjà suivi.

L'auteur corrige les deux P2 avant la fusion (exception ameesh sensible du canon §2), puis une relecture du delta final ; SQL/contrat requiert aussi le R2 d'une famille réelle autre que Codex et DeepSeek. La livraison inclut toujours activation du service et vérification par deepseek1. Aucun feu de production par ce rapport.
