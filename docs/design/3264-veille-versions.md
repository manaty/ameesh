# Veille des versions de harnais et de modèles (manaty/ameesh#3264)

Spécification co-écrite avec l'owner le 2026-10-28 (session du bureau). Les
décisions marquées **[owner]** sont les siennes ; le reste en découle.

Lots : **A** détection, **B** rejeu comparatif, **C** adoption. Premier cas
réel : #4278, le passage de dsh 0.2.0-rc.2 à 0.2.1-rc.4.

## 0. Le problème

DeepSeek a publié DeepSeek-V4.1-Flash-1017 le 2026-10-17 : tâches plus
courtes, 77 % sur DeepSWE v1.1 contre 74,2 % auparavant. Il recommande pour ce
modèle le harnais « DeepSeek Harness 0.2.1-rc.1 or later ». Le 28/10, les trois
VM d'agents de Manaty tournaient encore avec dsh 0.2.0-rc.2, la version du
29/09. Personne ne l'avait vu avant que l'owner pose la question.

Une comparaison modèle et effort a déjà été faite à la main au début
d'octobre, avec `nexlink-rd` : l'effort LOW de DeepSeek a été rejeté, plus
lent et moins bon d'un point de qualité. Elle était ponctuelle, et rien ne la
déclenche.

## 1. Principe

- **Générique** (canon §7). La liste des harnais et des modèles surveillés, et
  leurs sources de versions, viennent de la configuration de chaque mesh.
  ameesh ne connaît aucun mesh client.
- **Le mécanique est du code** (canon §7). Détecter une version, choisir le
  banc, lancer les rejeux et mesurer est déterministe. Un agent juge les
  écarts de qualité et rédige le rapport.

## 2. Lot A : détection

### 2.1 Ce qui est surveillé

Pour chaque harnais déclaré par une fiche d'agent du canon du mesh (`claude`,
`codex`, `deepseek`…), et pour chaque modèle utilisé :

- la version du harnais installée sur chaque hôte (`<cli> --version`, ou le
  `package.json` du paquet npm) ;
- les versions publiées : registre npm (`dist-tags` et `time`) ou releases du
  dépôt ;
- les modèles servis par le fournisseur : liste de modèles de l'API, et
  changelog s'il est déclaré ;
- l'identifiant réellement facturé dans `turn_costs.model`.

Les sources sont déclarées dans la configuration du mesh
(`veille.sources`), une entrée par harnais. Exemple neutre :

```json
{"veille": {"sources": [
  {"harness": "<harnais>", "npm": "<@org/paquet>", "changelog": "<url>"}
]}}
```

### 2.2 Quelles versions **[owner]**

Les versions **stables et rc** sont surveillées. Les alpha, beta et
pre-releases équivalentes sont ignorées. Une version n'est prise en compte que
si elle est plus récente que la version installée.

Une recommandation explicite du fournisseur (« version X or later » pour un
modèle servi) est relevée. Elle donne la priorité 1 au lot d'évaluation.

### 2.3 Geste

Une fois par jour, ameesh crée **un** lot « Évaluer <harnais ou modèle>
<version> », de priorité 2, ou 1 en cas de recommandation du fournisseur.
- Il est assigné à la file du mesh.
- Un seul lot d'évaluation est ouvert par harnais : une version plus récente
  remplace l'objet du lot ouvert.
- Le corps du lot donne la version installée par hôte, la version publiée, sa
  date, et le lien du changelog.

### 2.4 Acceptation de A

- Une rc plus récente que la version installée ouvre un lot. Une alpha n'en
  ouvre pas.
- Une deuxième version publiée pendant qu'un lot est ouvert met ce lot à jour
  sans en créer un second.
- Un écart de version entre hôtes est signalé dans le lot.
- Témoin : la situation de dsh au 28/10 (0.2.0-rc.2 installée, 0.2.1-rc.4
  publiée, 0.2.2-alpha.2 ignorée).

## 3. Lot B : rejeu comparatif

### 3.1 Le banc **[owner]**

Le banc est formé des **3 derniers lots livrés** du mesh, de classes
différentes : léger, normal, sensible. Si l'une des classes manque, on prend
le lot livré suivant.

Chaque lot est rejoué :
- sur sa base gelée (le SHA de base de son gel), avec son brief et son corps
  d'origine ;
- dans un arbre isolé, sans push, sans courrier et sans effet de bord ;
- deux fois, en ne faisant varier que le **paramètre évalué** : le harnais
  (version installée contre version évaluée) ou le modèle (modèle utilisé
  contre modèle évalué). Les paramètres non évalués restent constants d'un
  rejeu à l'autre : pour un harnais, le modèle, l'effort et le plafond de
  tour ; pour un modèle, le harnais, l'effort et le plafond de tour.

### 3.2 Les mesures

- **Coût** : jetons d'entrée, de cache et de sortie, et coût en dollars au
  barème du fournisseur (`cost.py`), ou part de forfait consommée.
- **Durée** : du début à la fin du tour, et nombre de tours.
- **Qualité** **[owner]** :
  1. **les tests du vrai lot livré** passent sur le rejeu. Ce sont les tests
     ajoutés ou modifiés par le lot, plus ses modules voisins (carte
     d'impact) ;
  2. **une relecture croisée à l'aveugle** par un relecteur d'une autre famille
     que le harnais qui exécute les rejeux : une note de 0 à 10 et la liste
     des défauts. Le relecteur ne sait pas quelle version a produit quel
     rejeu.

### 3.3 Budget

Les rejeux s'imputent dans les plafonds existants : marge de forfait d'abord,
puis le plafond DeepSeek. Une évaluation fait au plus 3 lots × 2 versions,
plus 6 relectures. Elle s'arrête et le signale si elle dépasse ce plafond.

### 3.4 Rapport

L'agent d'évaluation écrit une note du lot, avec un tableau par lot du banc
(ancienne et nouvelle version : coût, durée, tests, note, défauts) et une
conclusion :
- **meilleure** : aucune mesure moins bonne, et au moins une meilleure ;
- **équivalente** : écarts dans le bruit, soit ±10 % sur le coût et la durée
  et ±1 point sur la note ;
- **régression**.

Le rapport nomme le **paramètre évalué** — harnais ou modèle — et les
paramètres maintenus constants (le paramètre non évalué, l'effort et le
plafond de tour), pour qu'on ne lise jamais « même modèle » quand c'est le
modèle qui change.

### 3.5 Acceptation de B

- Le rejeu d'un lot livré sur sa base gelée ne pousse rien et n'écrit dans
  aucune file.
- Les deux rejeux ne diffèrent que par le paramètre évalué — le harnais ou le
  modèle ; le paramètre non évalué (modèle ou harnais), l'effort et le plafond
  de tour restent constants.
- Le relecteur ne voit pas la version.
- Le rapport donne les trois mesures pour chacun des trois lots, et nomme le
  paramètre évalué ainsi que les paramètres maintenus constants.

## 4. Lot C : adoption **[owner]**

- **Automatique** quand la conclusion est « meilleure » ou « équivalente »,
  sans régression de qualité, **à prix égal** : il s'agit d'une mise à jour de
  harnais ou d'une nouvelle version d'un modèle au même tarif.
  - Canari : un seul agent du harnais passe sur la nouvelle version pendant 24 h
    ou 10 tours, et ses mesures restent dans les bornes du rejeu.
  - Puis ameesh passe le mesh entier, hôte par hôte, entre deux tours, jamais
    dans les 20 min avant une coupure.
  - Inverse : remettre la version précédente, consignée dans le lot.
- **Décision owner** (`ameesh decide`) pour un modèle plus cher, un nouveau
  fournisseur ou un changement de forfait. La décision joint le rapport et le
  coût projeté sur 7 jours.
- **Régression** : aucune adoption. Le lot est clos avec son rapport, et la
  version suivante sera réévaluée.

### 4.1 Acceptation de C

- Une mise à jour à prix égal, sans régression, passe en canari puis sur tout
  le mesh, sans geste humain.
- Un modèle plus cher ouvre une décision owner, sans aucune adoption.
- Une régression ne change rien sur les hôtes.

## 5. Hors périmètre

- Changer le modèle ou l'effort d'un rôle par jugement, en dehors de toute
  nouvelle version : l'auto-réglage actuel, ou une décision owner.
- Les mises à jour de sécurité urgentes d'un harnais : elles suivent le circuit
  des bugs de priorité 1.
