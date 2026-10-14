# #1107 — procédure de démarrage en relevé initial groupé (SLO tranche 2)

Source : lot #1107, n1 P3 de la relecture Claude2 sur la tête `5b0189769` (PR #817),
tracé par mesh-design le 13/10 22:25. Ne bloque pas la fusion #1035, n'élargit pas
la tête de #817.

## 1. Le problème mesuré

À l'activation de la tranche 2, la première passe d'`exploitation.alerts()` produit
une alerte `slo_breach` par `(lot, étape, niveau)`. Un lot P1 (`critical`) porte
`owner` dans ses cibles et part **urgent** dès le premier dépassement : sans
préparation, la première passe envoie donc une rafale de notifications P1
historiques à l'owner, de nuit si l'activation a lieu la nuit.

Relevé du 2026-10-13 22:32Z (lecture seule, code de la tête `5b0189769` appliqué à
la base vivante, `releve-p1.py`) :

- **88 alertes** `slo_breach` sur **81 lots** distincts ;
- niveaux : 3× → 74, 1× → 13, 2× → 1 ;
- **15 alertes P1** (`critical`, owner + urgent) sur **13 lots** :
  - niveau 3× (12) : #364, #414 (×2 : `build_quiet`, `verdict_merge`), #645, #705,
    #747, #780, #810, #843, #880, #925, #932, #947 ;
  - niveau 1× (3) : #724, #947 (`build_quiet`).
- Le reste (73 alertes) ne touche pas l'owner (cibles assigné/pilote/bureau+auditeur).

L'instantané complet est dans `.qa/qa-1107/releve-p1.json` (et `releve-p1.md`).
Il est **daté** : c'est la liste à rejouer au moment de l'activation, pas une
liste à réutiliser telle quelle.

## 2. Procédure de démarrage (mécanismes existants, sans code)

1. **Relevé à T0, avant la première poussée.** Une passe de lecture, sans
   `--follow` : `ameesh alerts --json`, extraire les alertes
   `type=slo_breach` et `critical=true`. C'est l'ensemble A (les P1 historiques).
   Rejouer le relevé local (`.qa/qa-1107/releve-p1.py`) si la commande n'est pas
   encore disponible sur l'hôte au moment voulu.
2. **Présentation groupée à l'owner.** Un seul message : nombre de P1, tableau
   par lot (étape, niveau, retard, cible, assigné), et la phrase qui situe :
   ces P1 sont **antérieurs à l'activation** ; leurs notifications individuelles
   vont suivre (une par `(lot, étape, niveau)`), puis les nouveaux P1 seront
   notifiés normalement dès leur premier dépassement. Aucun P1 n'est ignoré.
3. **Suivi conservé par les mécanismes existants.** `notify` dédoublonne par
   `key_of(alerte)` = (type, agent, hôte, lot, `since`, `reason`) ; pour un
   `slo_breach`, `reason` = `<étape>/level-<n>` : chaque `(lot, étape, niveau)`
   a donc son entrée dans `notify/state.json` (`active` → `resolved`), et une
   montée de niveau est une clé neuve, donc une escalade neuve. La rafale est
   en outre bornée par `notify.rate_per_minute` (défaut 10/min par humain) et
   le surplus part en résumé périodique (`render_summary`) : la présentation
   groupée précède ce que le limiteur aurait de toute façon regroupé.
   Vérifié dans le code de la tête `5b0189769` (00:1xZ) :
   `exploitation.ESCALATING = ("host_disk_low",)` seul, donc `alert_key` garde
   bien `reason` pour `slo_breach` (`exploitation.py:1437-1459`) ;
   `_slo_breaches` pose `reason = "<step>/level-<n>"` (`exploitation.py:399`) ;
   `since = started_ts` (`slo.py:180`) est l'horodatage de l'événement ou du
   jalon qui a ouvert l'étape, stable tant que l'étape court : même clé d'une
   passe à l'autre au même niveau, clé neuve à la montée.
4. **Vérification reproductible.**
   - T0 : ensemble A = P1 `slo_breach` du relevé ; A est consigné groupé
     (message owner + note du lot d'activation).
   - T1 : après la première passe de notification, `ameesh alerts --json` donne B.
     Tout P1 de B\A est une escalade neuve légitime (nouveau dépassement ou
     montée de niveau) ; tout P1 de A\B doit avoir une trace de résolution
     (lot fermé/fusionné, étape repassée). `ameesh notify --dry-run` montre ce
     qui serait poussé sans pousser.
   - Critère : aucun P1 de A sans suivi (entrée `notify` ou trace de lot).

## 3. Option : grouper le premier envoi lui-même (code)

Sans intérêt pour un premier démarrage (le point 2 suffit, et le limiteur
existant borne déjà). Si l'owner veut **un seul** message au premier passage au
lieu de N notifications, c'est un code séparé : marqueur de démarrage à froid
(aucune entrée `slo_breach` dans `notify/state.json`) dans
`src/ameesh/exploitation.py` / `src/ameesh/notify.py` — chemins sensibles prod,
donc **PR distincte**, deux avis de familles différentes, hors PR #817.

## 4. Limites

- Instantané : à rejouer à T0 (les lots bougent, #925 et #880 sont sur le
  chemin critique de leur propre fusion).
- Les étapes non mesurées du §9 (`green_verdict`, `merge_prod`,
  `prod_verified`, `ci_queue`, `agent_paused`) ne produisent aucune alerte
  aujourd'hui : pas de P1 historiques sur ces étapes tant que la tranche
  suivante ne les branche pas.
- Les lots en attente planifiée et la file d'amélioration sans preneur sont
  exclus du relevé (comme dans le moteur).
