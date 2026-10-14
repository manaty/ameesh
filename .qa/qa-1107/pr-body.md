Lot #1107 « #1035 suite P3 : préparer le démarrage groupé des escalades SLO historiques » (issue #839).
n2 (P3) de la relecture Claude2 sur #1035 @5b018976942a : la passe d'alertes tombait entièrement
quand **un seul** lot portait un horodatage illisible.

## Ce que porte cette PR

Un seul commit (`83f1055df39edf2b8618fdb31378e09268e8bf97`), 3 fichiers, +72/-6 :

- `src/ameesh/exploitation.py` — `_slo_breaches` calcule chaque lot **sous garde** : la lecture des
  jalons et le calcul des étapes SLO d'un lot sont isolés ; une ligne illisible (horodatage
  textuel, `None` au mauvais endroit, conversion `float()` qui lève) émet une alerte
  `slo_unreadable` pour **ce** lot et les autres lots gardent leurs alertes. Sans la garde, le
  `ValueError` remontait de `alerts()` et tuait toute la passe (pas seulement le SLO).
- `src/ameesh/notify.py` — `slo_unreadable` étiqueté (`TYPE_LABELS`), donc routable/configurable
  comme les autres types ; le type est aussi déclaré dans `ALERT_TYPES` (exploitation.py).
- `tests/test_789_slo_alerts.py` — témoin mixte `LotIllisibleTest` : 2 lots sains en retard + 1 lot
  dont la lecture des jalons lève `ValueError` → 2 `slo_breach` + 1 `slo_unreadable` (agent, détail
  avec le type d'exception et le texte fautif). **Rouge sans la garde**, vert avec.

## Base et constance

- Branche `lot/1107-slo-per-lot-guard` @`83f1055df` (poussée sur origin), **base `5b0189769`** =
  tête gelée de PR #817 (#1035 tranche 2), car `exploitation._slo_breaches` n'est pas encore dans
  `main`.
- PR ouverte **après la fusion de #1035**, sur la `main` qui l'inclut : le diff attendu face à
  `main` est alors **exactement** ce commit (les commits de #1035 sont dans le parent).
  Aucun rebasage ni push sur la PR #817 gelée.
- Delta face à la base : `git diff --stat 5b0189769..83f1055df` = 3 fichiers, +72/-6.

## Preuves (exécution auteur, tests ciblés)

- `tests.test_789_slo_alerts` (14), `tests.test_789_slo`, `tests.test_l132_alert_briefs`,
  `tests.test_l26_exploitation`, `tests.test_l37_mode_et_alertes` : **87 OK** sur la tête, plus
  `LintTest`.
- Témoin mixte rejouable : sans la garde, le `ValueError` remonte d'`alerts()` et la classe rougit ;
  avec la garde, 2 `slo_breach` + 1 `slo_unreadable`.

## Classe et portes

- **Sensible prod** (canon §8, application mesh-design 13/10) : `src/ameesh/exploitation.py` est
  dans la liste des chemins d'exploitation ; `notify.py` en dépend.
- Portes attendues : **deux avis indépendants de familles différentes entre elles et de l'auteur
  (DeepSeek)** + CI exacte (toutes parts exécutées, aucune annulée, #940) + **feu du pilote**
  nommant la tête. Relectures demandées à mesh-design à l'ouverture.
- `_slo_breaches` est déjà couvert par les témoins de #1035 ; cette PR n'élargit pas le périmètre
  (pas de nouvelle tranche éteinte, pas de migration, pas de SQL).

ameesh-work: 1107
Closes #839
