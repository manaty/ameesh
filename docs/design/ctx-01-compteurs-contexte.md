# Contrat des compteurs de contexte — `ameesh-context/1`

| Champ | Valeur |
|---|---|
| **Version** | `ameesh-context/1`, révision 1 |
| **Statut** | proposition soumise à relecture ; stable à la fusion après verdict |
| **Date** | 2026-10-29 |
| **Source** | plan `e18` (CTX-01), `conversation:smichea:2026-10-17:roadmap-gestion-contexte` ; découpage #4510 |
| **Base de code** | `main` `a02b97c9b` (release 1.29.0) ; canon appliqué `5a488730` |
| **Classe** | légère (documentation/contrat), avis d'une autre famille, pas de CI lourde |
| **Portée** | spécification seulement : aucun seuil, aucune configuration, aucun comportement d'exécution n'est modifié par ce lot |

Ce document fixe le **sens** des mesures de contexte avant toute modification de
seuil. Il ne remplace ni les décisions 0018 (rotation), 0025 (session par lot)
ni L81 (entretien résident), et ne rouvre aucun de leurs mécanismes. Les lots
CTX-02 à CTX-06 l'implémentent, le mesurent et le qualifient ; ce lot-ci les
borne.

Mots normatifs : **DOIT**, **NE DOIT PAS**, **PEUT**. Les valeurs datées et les
fenêtres observées sont des **faits de runtime**, pas un catalogue universel.

---

## 1. Décision en une page

1. **Sept grandeurs principales** : entrée du dernier appel (`C_last`),
   occupation projetée du prochain (`C_next`, décomposée en entrée projetée
   `C_next_input` et occupation avec réserve de sortie), entrées cumulées du
   tour (`R_turn`), entrées cumulées de la session (`R_session`), sortie (`O`),
   durée (`D`) et coût (`Cost`). `O_last`, sortie du dernier appel, alimente la
   projection et ne se confond pas avec `O`. Aucune grandeur ne peut servir à
   la place d'une autre.
2. **`context_max_tokens` n'est pas un budget de relecture** : depuis #2336, il
   plafonne `C_last`, la taille du contexte du dernier appel du fil principal
   (fenêtre), cache lu **et** écrit compris. Un budget cumulé de relecture est
   une autre grandeur, et une autre décision (CTX-04).
3. **`session_max_tokens` est une garde de taille de session**, pas une
   fenêtre : elle compare la somme `S_hist` des `input_tokens + output_tokens`
   des tours de la session à un seuil par harnais (#4448). Sa convention de
   cache est hétérogène selon le harnais ; elle n'est pas comparable d'un
   harnais à l'autre.
4. **Une fenêtre inconnue reste inconnue** : elle ne se reprend jamais du
   modèle précédent, d'un modèle auxiliaire, du catalogue ou d'une valeur par
   défaut. Sans fenêtre, aucune décision en pourcentage ni compaction
   automatique : seul le plafond absolu de `C_last` et les gardes legacy
   s'appliquent.
5. **Une compaction n'est réussie que si le harnais publie l'après** ; une
   commande `/compact` n'est pas une preuve. Sans `post_tokens`, la mesure
   reste l'avant, marquée incomplète, et la rotation reste prioritaire.
6. **Le budget cumulé clôture, la fenêtre entretient** : budget cumulé →
   clôture sûre au point sûr, même session ; occupation de fenêtre →
   compactage/renouvellement qualifié ; frontière de lot ou changement
   d'identité → politique dédiée ; garde de sécurité → priorité absolue.
7. **Compatibilité stricte** : les noms, valeurs, défauts et surcharges legacy
   (`context_max_tokens`, `session_max_tokens`, `session_too_big`,
   `resident_reread_max`) gardent leur comportement. Aucun seuil n'est changé
   ici. Les noms cibles sont des alias de télémétrie/documentation, pas une
   migration.

---

## 2. Grandeurs du contrat

### 2.1 Table normative

Chaque grandeur a une **unité**, un **périmètre**, une **source**, une
**convention de cache**, une **remise à zéro**, une **règle de déduplication**
et un **comportement d'absence**. Une valeur absente est `null`/`unknown`, avec
une raison et une provenance ; elle n'est jamais remplacée par `0` ni par une
valeur supposée.

#### Mesures de contexte

| Id | Grandeur | Unité | Périmètre | Source | Cache | Remise à zéro | Déduplication | Absence |
|---|---|---|---|---|---|---|---|---|
| `C_last` | entrée du dernier appel au modèle | jetons | un appel du **fil principal** | flux du harnais (usage de l'appel) ; Codex : journal de session du fil | Claude : entrée + cache lu + cache écrit ; Codex : entrée (cache inclus) ; DeepSeek/ACP : entrée + cache lu | à chaque appel ; dernière valeur du tour | un appel = un `call_id` quand il existe ; blocs répétés d'un même message comptés une fois ; Codex : un `last_token_usage` par tour | `null` + `context_unknown` ; jamais `0` ; jamais la valeur du modèle précédent |
| `O_last` | sortie du dernier appel | jetons | dernier appel du fil principal | usage de l'appel (`output_tokens` / `outputTokens` ; Codex : `last_token_usage.output_tokens`) | sans objet (sortie) | à chaque appel ; dernière valeur du tour | un appel compté une fois ; jamais `O` du tour | `null` + `output_unknown` ; ne jamais remplacer par `O` du tour ni par `0` |
| `C_next` | occupation projetée du prochain appel | jetons | prochain appel du fil principal | **dérivée** : `C_next_input` + réserve de sortie du prochain appel (§2.3) | idem `C_last` pour la part déjà mesurée | à chaque appel | ne pas ajouter ce qui est déjà dans `C_last` ; `O_last` une seule fois ; `O` du tour jamais | `null` si `O_last` ou une composante est inconnue ; `C_next_input` peut rester connu ; intervalle possible, jamais un point supposé |
| `R_turn` | entrées cumulées du tour | jetons | un tour, fil principal | somme des entrées des appels du tour | Claude/DeepSeek/ACP : entrée + cache lu, **hors cache écrit** ; Codex : entrée (cache inclus) | au début du tour | un appel une fois ; cumuls Codex en delta | `null` si le tour n'a aucune mesure ; somme partielle marquée `partial` |
| `R_session` | entrées cumulées de la session | jetons | session courante | cumul des `R_turn` de la session | même convention que `R_turn` ; l'alerte résidente L81 mesure `R_session_L81` (cache écrit inclus), cas distinct (§5.3) | nouvelle session / rotation | jamais la somme de relevés cumulés | `null` sans session ou sans tour mesuré ; pas de reprise d'une valeur supposée |
| `W` | fenêtre du modèle effectif | jetons | profil `(modèle, harnais, version, mode, rôle)` | métadonnée runtime du harnais (ex. `modelUsage.contextWindow`, `modelContextWindow`, `usage_update.size`) | sans objet | changement de profil ; redémarrage si la métadonnée n'est pas re-annoncée | une entrée = un modèle ; ne pas sommer plusieurs modèles | `null` + `window_unknown` ; jamais une fenêtre auxiliaire ou précédente |
| `S_hist` | taille legacy de session (`session_max_tokens`) | jetons | session courante | grand livre `turn_costs` (`sum(input_tokens + output_tokens)`) ; compteur vivant de même somme | **hétérogène** : Claude/DeepSeek entrée non cachée ; Codex entrée cache inclus ; cache écrit exclu | nouvelle session / rotation ; relue du grand livre à la reprise du worker | Codex : côté compteur vivant, dernier relevé cumulé du fil (max), jamais la somme des relevés ; côté grand livre, somme des deltas par tour, qui donne le même total ; autres : somme des tours | `null` sans tour inscrit ; seuil : canon par harnais, sinon valeur commune |

#### Temps et coût

| Id | Grandeur | Unité | Périmètre | Source | Remise à zéro | Déduplication | Absence |
|---|---|---|---|---|---|---|---|
| `O` | jetons de sortie du tour | jetons | un tour | usage du harnais | début du tour | une fois par appel | `null` si aucun usage de sortie |
| `D` | durée du tour | secondes | un tour | horloge de l'exécuteur | début du tour | une mesure par tour | `null` si le tour n'a pas été chronométré |
| `Cost_turn` | coût imputé du tour | USD | un tour | harnais (`total_cost_usd` en delta) ou barème local | début du tour | delta d'un cumul, jamais le cumul | `null`/`void` si aucun prix ou mesure ; jamais `0` par défaut |
| `Cost_session` | coût imputé de la session | USD | session | cumul des tours / `cost_total` du harnais | nouvelle session / changement de compte | delta du cumul | `null` si le cumul n'est pas lisible |

`Cost_turn` est une **imputation au barème**, pas une facture : pour un compte
au forfait, ce n'est pas un montant d'API réellement facturé. Un coût de
forfait, un cache et une estimation ne sont jamais présentés comme des dollars
d'API. Le champ de provenance (`cost_basis`) vaut `api-list`,
`subscription-imputed` ou `unknown` ; il est obligatoire dès qu'un coût est
publié.

### 2.2 Définitions par harnais

Les clés de flux sont normalisées par `adapters.usage_tokens` ; les formules
ci-dessous sont celles de `adapters.reread_tokens` et
`adapters.context_tokens`, étendues par le contrat.

| Harnais | Entrée | Cache lu | Cache écrit | `C_last` | `R_turn` |
|---|---|---|---|---|---|
| Claude (stream-json) | `input_tokens` | `cache_read_input_tokens` | `cache_creation_input_tokens` | entrée + cache lu + cache écrit | entrée + cache lu (**hors** écrit) |
| Codex (exec / app-server) | `input_tokens` (cache **inclus**) | `cached_input_tokens` (sous-ensemble) | — | `input_tokens` (ou `max(input, cached)` si l'entrée manque) | `input_tokens` (cache inclus) |
| DeepSeek (dsh) | `inputTokens` | `cacheReadTokens` | — | entrée + cache lu | entrée + cache lu |
| ACP (générique) | `inputTokens` / `used` | `cacheReadTokens` (0 dans le pont actuel) | — | entrée du dernier `usage_update` (`used`) | entrée + cache lu |

Règles :

* **Claude** : `cache_creation_input_tokens` est une **écriture** de cache. Elle
  appartient à `C_last` (le prompt du dernier appel l'a payée) mais pas à
  `R_turn` (ce n'est pas une relecture) ; le grand livre la porte séparément
  (`cache_write_tokens`).
* **Codex** : `input_tokens` inclut déjà `cached_input_tokens`. Ne jamais
  additionner les deux : `C_last = input_tokens`, pas `input + cached`.
* **DeepSeek/ACP** : pas de champ d'écriture de cache dans le contrat actuel ;
  un harnais qui en publierait un devrait être qualifié avant d'être agrégé.
* **`O_last`** : `output_tokens` (Claude, Codex `last_token_usage`) ou
  `outputTokens` (DeepSeek). Le pont ACP actuel publie `output_tokens: 0`
  faute de champ de sortie : ce `0` n'est **pas** une mesure d'`O_last` ;
  `O_last` reste `unknown` tant que le harnais ne fournit pas une sortie
  explicite, et `C_next` n'est alors pas calculable.
* **Sous-agents** : leurs appels ne comptent ni dans `C_last`, ni dans
  `R_turn`, ni dans `R_session` du fil principal. Ils sont mesurés à part, pour
  information et coût, et ne sont jamais un point sûr du tour principal.
* **Session** : un relevé n'est valable que s'il porte la session courante.
  Un `C_last` ou un `R_turn` d'une autre session est `unknown` pour la
  décision : il n'est ni reporté ni additionné à la nouvelle session.
* **Comparaison** : deux jetons de deux harnais ne sont pas la même grandeur.
  Aucune addition ni aucun pourcentage ne mélange les conventions ci-dessus.
* **Chemin résident L81** : `resident_runner.turn_reread` additionne aussi le
  cache écrit ; c'est l'agrégat `R_session_L81` de l'alerte `resident_reread_max`, pas
  `R_turn`/`R_session` au sens du contrat. Tant que CTX-02 ne l'a pas normalisé, il est publié sous
  son nom legacy et ne doit pas être comparé à `R_turn`.

### 2.3 Occupation projetée du prochain appel (`C_next`)

`C_next` est une **estimation nommée**, pas une mesure. Elle sert à décider
d'un compactage ou d'un renouvellement *qualifié* (CTX-04/CTX-05), jamais à
présenter un budget cumulé comme un pourcentage de fenêtre.

La projection distingue deux choses :

```
C_next_input = C_last          (entrée du dernier appel, déjà dans l'historique)
             + O_last          (sortie du dernier appel, ajoutée à l'historique)
             + G_consigne      (nouvelle consigne et résultats d'outils attendus)
             + G_protocole_outils (croissance non déjà comptée dans C_last)
             + M_harnais       (marge déclarée par le harnais)

C_next       = C_next_input
             + O_reserve_next  (sortie maximale du prochain appel)
```

* `C_next_input` est l'**entrée projetée** du prochain appel : ce que le
  harnais va relire avant de générer. `C_next` est l'**occupation** de la
  fenêtre pendant le prochain appel, réserve de sortie comprise. Les deux sont
  publiées séparément : une décision « reste-t-il de la place pour la sortie ? »
  compare `C_next` à `W`, ou de façon équivalente `C_next_input` à
  `W - O_reserve_next`.
* **`O_last` n'est pas `O`.** `O` est la sortie cumulée du tour ; dans un tour
  à plusieurs appels, elle ne dit pas ce que le dernier appel a ajouté à
  l'historique. Seul `O_last` entre dans `C_next_input`. Exemple : `C_last`
  50 000, `O_last` 30 000, `G_consigne` 1 000, `O_reserve_next` 5 000 →
  `C_next_input` = 81 000, `C_next` = 86 000. La formule qui ignorerait
  `O_last` donnerait 56 000 et sous-estimerait la fenêtre de 30 000.
* **Pas de double comptage** : le prompt système, les schémas d'outils et
  l'historique déjà présents dans `C_last` ne sont pas ajoutés une seconde
  fois. `O_last` est ajouté une seule fois, comme croissance de l'historique
  depuis `C_last` ; `O` du tour n'est jamais ajouté. `G_protocole_outils` ne
  compte que la croissance, et vaut `0` avec la provenance `included` si le
  harnais l'a déjà incluse.
* **Absence** : `O_last` absent (harnais qui ne publie que l'usage du tour,
  tour interrompu, relevé partiel) → `C_next` et `C_next_input` sont `null` ou
  un intervalle explicitement borné ; jamais `O` du tour, jamais `0`. Une
  réserve `O_reserve_next` inconnue rend `C_next` `null`, mais peut laisser
  `C_next_input` connu.
* Aucune valeur numérique de réserve n'est fixée par ce lot : les profils les
  déclareront, avec leur source et leur date. Sans déclaration, `C_next` n'est
  pas calculable et aucune décision en pourcentage n'est prise.
* La projection ne remplace jamais `C_last` : un dépassement de `C_last` reste
  un dépassement dur.

### 2.4 Qualité et provenance

Chaque valeur publiée porte une qualité :

| Qualité | Sens |
|---|---|
| `measured` | clé d'usage présente dans le flux, appel principal, dédupliquée |
| `derived` | calculée à partir de mesures (`C_next`, `Cost_turn` au barème) |
| `fallback` | repli explicitement nommé (ex. relectures du dernier tour pour `C_last` absent) |
| `partial` | couverture incomplète (appels manquants, tour interrompu, compaction sans après) |
| `unknown` | aucune mesure ou métadonnée ; valeur `null`, avec raison |

Une valeur `fallback` ou `partial` **n'est pas** une valeur `measured` : elle
est nommée comme telle dans la télémétrie et dans toute décision qui l'utilise.
Le passage d'une qualité à une autre est daté et attribué au profil.

Ces règles d'absence sont la **cible** du contrat. Le code de `main`
`a02b97c9b` peut encore produire `0` dans certains chemins (clé absente,
`context_tokens` sans champs, coût sans barème) ; CTX-02 les remplace par
`null`/`void` et une raison. Aucun lot ne doit lire un `0` actuel comme une
mesure certaine.

---

## 3. Profils

### 3.1 Clé de profil

Un profil est la combinaison explicite :

```
(modèle_effectif, harnais, version_harnais, mode, rôle, compte, mode_de_compte)
```

* `mode` ∈ {`non-résident`, `résident`}. Le champ `mode` du registre
  (`execute`/`externe`) est un autre concept : il ne remplace pas celui-ci.
* `rôle` est une classe de politique, pas une identité d'agent : `pilot`,
  `review`, `audit`, `build`, `docs`, `watch`, `other` (la table §3.6 nomme
  `audit`). Le rôle effectif vient du canon et de la fiche de l'agent ; le
  profil ne le devine pas.
* `compte` et `mode_de_compte` (`subscription`/`api-key`) font partie de
  l'identité pour le cache et le coût ; ils ne changent pas la fenêtre du
  modèle, mais leur changement invalide la lecture de `Cost` et des caches.

**Modèle effectif** : le modèle annoncé par le harnais pour le fil principal
(événement de session, `result`, `modelUsage`, `modelContextWindow`,
`config_option_update`). Le modèle *demandé* par le registre n'est qu'une
entrée ; s'il n'est pas confirmé, le profil porte `model_quality=requested`,
jamais `measured`. Un alias n'est résolu que par une table versionnée et
sans ambiguïté ; sinon `model_effectif=null`, `window=null`.

**Version du harnais** : version du binaire lue par
`harness_versions.probe` (`<binaire> --version`), enregistrée par hôte. La
version du descripteur (`1.0.0`, schéma 1) n'est pas la version du binaire et
ne doit pas la remplacer. Version illisible : `null` +
`version_unknown` dans `version_reasons` ; les `reasons` du profil restent
réservés au modèle et à `W`, pour que le corpus ne les attache pas à la
fenêtre.

### 3.2 Priorité et traçabilité des overrides

La précédence dépend de la clé ; elle est fixée par le code, pas par ce
document :

| Clé | Précédence |
|---|---|
| `context_max_tokens` | réglage explicite de l'agent, sinon `AMEESH_CONTEXT_MAX_TOKENS`, sinon défaut du paquet |
| `session.max_tokens`, `session.max_tokens_by_harness` | canon compilé au commit appliqué, sinon `AMEESH_SESSION_MAX_TOKENS` ; pas de réglage d'agent |
| `session_policy`, `turn_max_seconds`, `turn_mail_max` | réglage explicite de l'agent, sinon défaut de l'exécuteur |
| `resident`, `resident_sleep`, `resident_reread_max` | état local de l'hôte de l'agent, sinon défaut du paquet |

Tout override porte : portée, clé, valeur, **auteur**, **date**, **source**,
**raison**, valeur précédente, et profil(s) auxquels il s'applique. Un override
sans source ni date n'est pas traçable : il est refusé ou signalé, jamais
appliqué silencieusement.

Un override **ne survit pas** à un changement d'identité de profil s'il était
dérivé de l'ancien profil (fenêtre, seuil natif, marge, réserve). Il est
revalidé, conservé avec une nouvelle justification, ou retiré.

### 3.3 Revalidation à chaque bascule

Les événements suivants **invalident** `W`, la politique de compaction et les
composantes de `C_next` :

* changement de modèle effectif (y compris alias résolu différemment) ;
* changement de harnais ou de version de harnais ;
* passage non-résident ↔ résident ;
* changement de compte ou de mode de compte ;
* changement de rôle ;
* redémarrage du worker ou du harnais si la métadonnée n'est pas re-annoncée.

Après invalidation : `window=null` jusqu'à une nouvelle preuve runtime ; aucune
valeur de l'ancien profil n'est réutilisée, même si le nom du modèle est
identique. Une session peut continuer avec le nouveau profil, mais les mesures
`C_last`/`R_turn` redémarrent à la première mesure du nouveau profil et les
décisions de fenêtre attendent cette mesure.

### 3.4 Forme d'un profil (exemple de télémétrie)

```yaml
profile:
  model_effective: claude-opus-5-5
  model_quality: measured
  harness: claude
  harness_version: 2.1.309
  mode: non-resident
  role: pilot
  account: primaire
  credential_mode: subscription
  window_tokens: 1000000
  window_source: runtime:modelUsage.contextWindow
  window_quality: measured
  native_compaction:
    supported: true
    threshold: auto
    threshold_source: harness-default
    threshold_quality: unknown
  policy: legacy
  overrides: []
```

Ce n'est pas un fichier de configuration : c'est la forme que la télémétrie
doit pouvoir publier, avec `null` et une raison quand une preuve manque. Les
valeurs sont illustratives ; un profil actif est daté et revalidé.

### 3.5 Combinaisons actives observées (source 2026-10-16/17)

Les trois combinaisons ci-dessous sont celles du rejeu de la source. Les
fenêtres sont des **métadonnées runtime observées**, pas un catalogue ; elles
doivent être revalidées à chaque démarrage. Les versions de binaires sont
relevées sur l'hôte `ameesh-agents-2` le 2026-10-29.

| Id | Modèle effectif | Harnais / version | Mode | Rôles typiques | `W` observée | Source `W` | Compaction native | Contrôle Ameesh |
|---|---|---|---|---|---|---|---|---|
| P1 | `claude-opus-5-5` | Claude Code 2.1.309, `claude-stream-json` | non-résident | pilot, review, build | 1 000 000 | `modelUsage.contextWindow` (rejeu) | oui, `--autocompact` (`auto` ou 100k–1M) ; seuil `auto` non établi | aucun appel `/compact` en non-résident ; plafond `context_max_tokens` et rotation |
| P2 | `gpt-6.1-sol` | Codex CLI 0.159.3, `codex exec --json` | non-résident | review, build, audit | 475 000 | métadonnée runtime du rejeu | oui au protocole (`thread/compact/start`, `model_auto_compact_token_limit`) ; seuil non établi ; non piloté par Ameesh | plafond entre deux tours seulement ; aucun contrôle en cours de tour |
| P3 | `deepseek-flash` | DeepSeek Harness 0.2.1-rc.4, `dsh` headless | non-résident | build, docs, watch | 1 000 000 | métadonnée runtime du rejeu | non établie ; aucun événement de compaction exposé | plafond sur l'usage par étape ; pas de fenêtre, pas de compaction |

**Ces fenêtres sont observées au rejeu, pas nécessairement consommées par le
chemin non résident d'Ameesh** : Claude non-résident ne lit pas `modelUsage`
pour `W`, Codex non-résident ne lit pas `modelContextWindow`, et dsh n'expose
pas `W`. Le profil runtime reste donc `W=null` tant que CTX-02/03 ne l'expose
pas ; les valeurs du tableau servent de référence qualifiée, pas de valeur
supposée.

Le mode résident, **désactivé** au moment de la source (41 unités runner,
`resident off`), est couvert par les mêmes profils avec les capacités
suivantes ; il n'est pas activé par ce lot.

| Id | Profil résident | `W` | Compaction native / override Ameesh | Limite de contrôle |
|---|---|---|---|---|
| P1-R | Claude résident | `modelUsage.contextWindow` | natif + Ameesh `/compact` à 50 % de `W`, retry 600 s ; l'après vient de `compact_boundary` | la commande n'est pas une preuve ; rotation prioritaire |
| P2-R | Codex app-server résident | `modelContextWindow` | natif possible (`thread/compact/start`, `model_auto_compact_token_limit`) ; Ameesh ne le demande pas et ne lit pas `thread/compacted` | seuil natif inconnu ; rotation au plafond |
| P3-R | dsh ACP résident | `usage_update.size` | non établie ; le pont ACP n'expose pas de compaction | aucune demande possible ; rotation au plafond |

### 3.6 Rôles et politique

Le rôle ne crée pas de seuil : il sélectionne une politique et empêche de
réutiliser les valeurs d'un autre rôle.

| Rôle | Politique attendue | Session | Décision de fenêtre |
|---|---|---|---|
| `pilot` | longue session, rotation avec résumé (décision 0025 §3) | `taille` ou `par-lot` selon l'agent | compactage qualifié si `W` connue ; sinon plafond absolu |
| `review` | session fraîche par lot relu, gardée pour les re-freezes (0025 §2) | `par-lot` | plafond absolu ; compaction rarement utile |
| `audit` | session par mission d'audit, sans réutilisation d'un lot à l'autre | `par-lot` ou `taille` selon l'agent | plafond absolu ; compaction rarement utile |
| `build` | session fraîche par lot, gardée jusqu'à la fusion (0025 §1) | `par-lot` | plafond absolu ; compaction rarement utile |
| `docs` / `watch` | session courte par lot ou par passage | `par-lot` | plafond absolu |
| `other` | politique legacy, sans inférence | selon l'agent | aucune décision en pourcentage |

Le profil **complet** est le croisement d'une ligne 3.5 et d'un rôle 3.6. Une
valeur observée pour `pilot` ne vaut pas pour `review` ; une valeur observée en
non-résident ne vaut pas en résident.

**Rôle publié.** `turn.context.profile.role` porte la classe de politique,
jamais le nom de la fiche : le **premier** rôle déclaré dans `roles:` du canon
est le rôle principal, et les synonymes sont ramenés à cette classe
(`orchestrateur` → `pilot`, `relecture`/`qa` → `review`, `auditeur` → `audit`,
`implementation`/`composition` → `build`, `documentation`/`release-notes` →
`docs`, `github-watch`/`veille` → `watch`, le reste → `other`). Le mappage est
déterministe et versionné avec le code (`context_profile.ROLE_BY_CANON`) ; un
premier rôle hors vocabulaire, une fiche sans rôle ou un rôle illisible ne
devient jamais une supposition : il vaut `other` (le profil ne saute pas au
rôle suivant pour se fabriquer une politique).

---

## 4. Compactage natif

### 4.1 Politique par harnais

| Combinaison | Support | Seuil natif | Événements | Échec / incomplet | Override Ameesh | Limites du contrôle |
|---|---|---|---|---|---|---|
| Claude non-résident | oui (`--autocompact`) | `auto` par défaut ; valeur exacte **non établie** ; explicite 100k–1M si fourni | `system/compact_boundary`, `compact_metadata` avant/après | après absent : pas de mesure d'après ; aucun échec exploité par Ameesh | aucun en non-résident | Ameesh ne demande pas `/compact` ; il ne peut que tourner au plafond |
| Claude résident | oui | natif `auto` + override Ameesh **50 % de `W`** (`COMPACT_SHARE`) | idem ; l'après met à jour `C_last` | pas d'après : garder l'avant, marquer `partial` ; retry 600 s borné ; pas de boucle | `/compact` à 50 % ; retry 600 s | la commande n'est pas un succès ; la rotation gagne sur le compactage |
| Codex non-résident | oui au protocole | `model_auto_compact_token_limit` ; valeur exacte **non établie** | `ContextCompaction` / `thread/compacted` ; non lus par Ameesh | inconnu ; Ameesh ne mesure pas l'après | aucun | `codex exec --json` n'expose pas de contrôle en cours de tour ; plafond entre tours seulement |
| Codex résident | oui au protocole | idem ; **non établi** | `thread/compacted` / item de compaction ; non lus par Ameesh | inconnu | aucun ; Ameesh ne lance pas `thread/compact/start` | fenêtre `modelContextWindow` mesurée, mais décision de compaction non pilotée |
| DeepSeek/dsh et ACP | non établi | non établi | aucun événement de compaction dans le pont ACP actuel | inconnu | aucun | seule la rotation au plafond est disponible |

### 4.2 Règles

* **Seuil natif non établi = pas de généralisation.** On ne suppose ni 50 %, ni
  un pourcentage de `W`, ni un seuil fixe. Le 50 % d'Ameesh en résident est un
  **override Ameesh**, pas le seuil natif du harnais.
* **Une compaction demandée n'est pas une compaction réussie.** Seul
  `post_tokens` (ou la mesure du prochain appel) prouve l'effet. Sans après :
  `C_last` reste l'avant, qualité `partial`, et la prochaine mesure tranche.
* **La rotation gagne toujours** sur un compactage : si `C_last` atteint le
  plafond dur, on clôture et on tourne ; on ne demande pas un compactage pour
  éviter une rotation due.
* **Pas de boucle** : après une demande, un délai de reprise (600 s en
  résident) empêche de redemander tant que le contexte n'est pas retombé.
* **Pas de compaction en cours de tour non résident** : Claude/DeepSeek/ACP
  peuvent seulement clôturer au point sûr ; Codex non-résident n'a aucune
  mesure par appel, donc aucune décision de compaction en cours de tour.
* **Publication** : `turn.context.compaction` porte la compaction native
  observée du tour (`pre`, `post`, `quality`, `reasons`, `trigger`) ; le
  runner la publie pour un tour résident (le driver) comme non résident
  (`compact_boundary` du flux principal). `decision.compacted` vaut `true`
  quand elle est observée, `false` quand le tour n'a pas compacté — une
  compaction demandée par la politique n'est pas une réussite (§4.2). Le
  `false` explicite est ce qui permet au rejeu de juger la fenêtre ; `null`
  reste réservé au tour dont la mesure est perdue.

---

## 5. Table de décision

### 5.1 Priorité

1. **Garde de sécurité et d'autorité** : bail perdu, humain urgent,
   préemption, retrait d'hôte, pause opérateur, budget, batterie critique,
   plafond de durée, courrier borné. Aucune maintenance de contexte ne retarde
   ces gardes.
2. **Plafond dur de contexte** : `C_last >= context_max_tokens` → clôture au
   prochain point sûr, puis rotation avant le tour suivant.
3. **Entretien de fenêtre qualifié** : `C_next`/`C_next_input` proche de `W`, avec
   fenêtre connue et compaction supportée → compactage ou renouvellement.
4. **Budget cumulé** : `R_turn`/`R_session` au-delà d'un budget configuré →
   clôture sûre, même session, sans rotation ni compactage pour ce seul motif
   (cible CTX-04 ; aucun budget de tour n'est activé par ce lot).
5. **Frontière de lot / identité** : `par-lot`, lot fini, changement de
   modèle/compte/harnais/version → session ou profil dédié, revalidation.
6. **Alertes et information** : `session_too_big`, `resident_reread_max`,
   `long_turn` ; elles informent, elles ne remplacent pas une décision.

### 5.2 Les quatre décisions

| Déclencheur | Mesure | Décision | Ne fait pas |
|---|---|---|---|
| **Budget cumulé** | `R_turn`, `R_session` | clôture sûre au point sûr, puis continuation sur la même session ; budget explicite et tracé | ne présente pas le budget comme un % de `W` ; ne tourne pas la session pour ce seul motif ; ne déclenche pas un compactage de fenêtre |
| **Occupation de fenêtre** | `C_last`, `C_next_input`, `C_next`, `W` | compactage natif qualifié si supporté et `C_next` proche de `W` (ou `C_next_input` proche de `W - O_reserve_next`) ; sinon rotation au plafond dur ; projection inconnue → aucune décision en pourcentage | n'utilise pas `R_turn`/`R_session` comme taille de fenêtre ; n'invente pas `W` |
| **Frontière de lot / identité** | lot du tour, profil | session dédiée `par-lot`, fermeture du lot fini, nouveau profil et revalidation à chaque bascule | ne réutilise pas une fenêtre, un seuil natif ou une marge de l'ancien profil |
| **Garde de sécurité** | bail, humain, hôte, budget, durée, courrier | traitement immédiat, prioritaire sur tout entretien | ne diffère jamais une garde pour « finir le contexte » |

La ligne **budget cumulé** est une **cible** (CTX-04) : aucun budget de tour
n'est activé par ce lot, et les compteurs legacy (`session_max_tokens`,
`session_too_big`) ne sont pas requalifiés en budget de tour.

### 5.3 Compatibilité legacy : noms, mesures, valeurs

**Lecture rapide.** `context_max_tokens` est l'ancien nom d'un budget de
relecture (L60) devenu, depuis #2336, un plafond de **fenêtre** ;
`session_max_tokens` est la **garde historique de taille de session** (L11) ;
aucun des deux n'est un budget de relecture par tour. Le budget de relecture
par tour est une **cible** de CTX-04, pas un réglage existant.

Ce lot ne change **aucune** valeur ni aucun comportement. Le tableau suivant
fixe la lecture exacte des noms existants.

| Nom legacy | Défaut / portée | Mesure réelle (main `a02b97c9b`) | Cache | Décision | Nom cible (documentation) |
|---|---|---|---|---|---|
| `context_max_tokens` | 15 000 000 ; par agent ; `0` = off ; vide = défaut | `C_last` : contexte du dernier appel du fil principal ; repli sur les relectures du dernier tour inscrit au grand livre (`last_turn_reread`), **hors Codex** ; Codex sans relevé d'appel : pas de rotation sur la somme | Claude : entrée + cache lu + cache écrit ; Codex : entrée ; DeepSeek/ACP : entrée + cache lu | clôture au point sûr, puis rotation ; jamais un budget cumulé | `window.max_tokens` (plafond absolu de `C_last`) |
| `session_max_tokens` | 150 000 ; canon `session.max_tokens` / `max_tokens_by_harness` ; sinon commun ; #4448 | `S_hist` = `sum(input_tokens + output_tokens)` des tours de la session ; Codex : dernier relevé cumulé du fil côté compteur vivant, somme des deltas côté grand livre ; reprise du grand livre au redémarrage | **hétérogène** : Claude/DeepSeek entrée non cachée ; Codex entrée cache inclus ; cache écrit exclu | rotation sur la taille (L11), au minimum de tours, jamais pendant un tour | `legacy.session_size_max` (garde de taille, pas fenêtre, pas coût) |
| `session_too_big` | même mesure et même seuil que `session_max_tokens` (#4448) ; `--session-tokens`/`AMEESH_ALERT_SESSION_TOKENS` = surcharge globale | `S_hist` de la session courante, minimum de tours, aucun tour en cours ; silencieuse après rotation | idem `S_hist` | alerte seulement ; la rotation est décidée par la même mesure dans `rotation_due` | `legacy.session_size_alert` |
| `resident_reread_max` | 50 000 000 ; réglage local ; `0` = jamais | `R_session_L81` = somme des `turn_reread` résidents ; **inclut le cache écrit** ; distinct de `R_session` | Claude : entrée + cache lu + cache écrit ; autres : idem selon clés | alerte douce → rotation au moment calme (L81), pas une fenêtre | `reread.session_alert` |
| `context_max_tokens` (nom historique) | — | avant #2336 : somme des relectures du tour ; le nom est resté | — | **ne pas réutiliser cette lecture** : le code actuel mesure `C_last` | `window.max_tokens` ; l'ancien sens est retiré |

**Sémantique de `0` et du vide** :

* `context_max_tokens=0` : plafond désactivé (documenté) ; vide = défaut 15 M.
* `session_max_tokens` : `0` n'est **pas** une extinction documentée ; le canon
  refuse `0` ; ne pas l'utiliser comme « off ». Vide = défaut/canon.
* `resident_reread_max=0` : jamais de rotation sur le cumul résident.
* `session_policy=jamais` : aucune rotation L11/L60 ; les gardes de sécurité
  restent.
* `W=null` : inconnu, jamais `0` ; `C_next=null` si une composante manque ;
  `R_turn=null` si aucune mesure ; aucun `0` par défaut.

**Fenêtre de compatibilité** : les noms legacy restent acceptés et documentés
au moins jusqu'à une version majeure ultérieure. Les noms cibles sont des
alias de télémétrie ; aucune migration de configuration n'est faite ici.
CTX-02 peut publier les deux jeux de champs en parallèle ; CTX-04 peut ajouter
un mode opt-in, mais le mode legacy garde exactement sa lecture.

### 5.4 Écarts documentaires constatés (hors périmètre de ce lot)

Ces écarts ne sont pas corrigés ici (une seule empreinte :
`docs/design/ctx-01-compteurs-contexte.md`) ; ils sont signalés pour un lot de
documentation distinct.

* `site/docs/reference/configuration.md` décrit encore `AMEESH_CONTEXT_MAX_TOKENS`
  comme « when the turn re-reads more input tokens (cache included) » : c'est
  l'ancien sens d'avant #2336. Le code mesure `C_last`.
* `src/ameesh/config.py` décrit `session_max_tokens` comme « taille de contexte
  et latence » ; la mesure réelle est `S_hist`, hétérogène selon le harnais.
* `docs/V1-MAILBOX-RUNNER.md` dit « the size is read from the usage in each
  harness's stream » sans préciser la convention de cache ; le présent contrat
  la précise.
* `src/ameesh/storage/postgres/costs.py::session_total` dit « cache compris » ;
  la requête somme `input_tokens + output_tokens`, donc le cache n'est inclus
  que là où `input_tokens` le contient déjà (Codex). Le commentaire est
  ambigu ; la mesure réelle est celle du tableau §5.3.
* `session_too_big` a changé de sens avec #4448 (taille de session, plus
  relectures d'un tour) : les tableaux d'alertes qui citent l'ancien sens
  doivent être relus.

---

## 6. Cinq contre-exemples et cas limites

### 6.1 Les cinq reproductions de `replay_context.py`

`replay_context.py` est une annexe locale de l'étude du 2026-10-17, absente du
dépôt. Les cinq reproductions sont transcrites ici depuis la note de la
roadmap ; chaque ligne est rattachée à une exigence et à la vérification qui la
couvrira. Le rejeu d'origine (Ameesh 1.11.4) et l'état de `main` sont séparés :
le contrat ne prétend pas que l'ancien résultat est encore le résultat actuel.

| # | Reproduction | Résultat au rejeu d'origine | État sur `main` `a02b97c9b` | Attendu cible | Exigence | Vérification |
|---|---|---|---|---|---|---|
| R1 | **Ordre des modèles dans `modelUsage`** : un modèle auxiliaire (200 k) précède le principal (1 M) | la fenêtre du principal peut être lue comme 200 k ; compactage/rotation au mauvais seuil | défaut latent : `resident._on_result` prend la première entrée avec `contextWindow` | résoudre le modèle **principal effectif** ; si ambigu → `W=null` ; jamais la fenêtre auxiliaire | EX-06, EX-07 | CTX-03 : auxiliaire 200 k en premier, principal 1 M ; la fenêtre retenue reste 1 M |
| R2 | **Création du cache** : un premier appel Claude écrit 3 000 jetons de cache | le contexte est sous-estimé si l'écriture est omise ; le coût est mal ventilé si elle est confondue avec une relecture | `context_tokens` inclut l'écriture ; `R_turn` et `S_hist` ne l'incluent pas ; le grand livre la porte à part | `C_last` inclut l'écriture ; `R_turn` l'exclut ; `Cost` la porte séparément | EX-01, EX-03 | CTX-02 : fixtures Claude avec `cache_creation_input_tokens` |
| R3 | **Granularité de remontée Codex** : dix appels de 60 k, un seul cumul de fin de tour | la somme des appels (600 k) est prise pour le contexte ; rotation à tort | `C_last` Codex lu dans `last_token_usage` (60 k) ; plafond entre tours seulement ; `S_hist` Codex = dernier cumul côté compteur vivant, somme des deltas côté grand livre (#4448) | une capacité par harnais : Codex non-résident ne décide pas en cours de tour ; jamais la somme comme `C_last` | EX-03, EX-05, EX-08 | CTX-02/03 : journal Codex à dix appels, plafond 400 k, pas de rotation |
| R4 | **Priorité du budget cumulé sur le compactage** : 105 arrêts cumulés à 400 000, contexte médian 53 114 | le budget cumulé est lu comme une fenêtre ; arrêt/rotation alors que la fenêtre est loin d'être pleine | #2336 a séparé `context_max_tokens` (C_last) de la somme ; un budget cumulé n'actionne plus la fenêtre | budget cumulé → clôture sûre, même session ; fenêtre → compactage/renouvellement ; jamais un pourcentage | EX-02, EX-09 | CTX-04 : 400 k cumulés, C_last 50 k, fenêtre 1 M → pas de rotation de fenêtre |
| R5 | **Garde historique 150 000 et conventions de cache** : Claude et Codex comparés sur le même 150 k | le même nombre ne mesure pas la même chose ; Codex peut tourner au minimum de trois tours | `session_max_tokens` par harnais (#4448) ; `S_hist` inclut le cache pour Codex, pas pour Claude/DeepSeek ; `session_too_big` = même mesure et même seuil | documenter la convention par harnais ; garder 150 k comme repli commun ; aucune comparaison brute entre harnais | EX-03, EX-10 | CTX-02/05 : même `S_hist` sur Claude et Codex, décisions et alertes distinctes |

### 6.2 Cas limites

| Cas | Comportement attendu | Exigence |
|---|---|---|
| **Modèle inconnu** | `W=null`, `window_quality=unknown` ; pas de pourcentage ni de compaction native ; plafond absolu de `C_last` et gardes legacy seulement ; aucune reprise du modèle précédent | EX-05, EX-07 |
| **Alias** | résolution explicite et versionnée seulement ; alias ambigu ou inconnu → `null` ; le modèle demandé n'est pas le modèle effectif | EX-06, EX-07 |
| **Bascule de modèle** | profil invalidé ; `W` et compactage revalidés ; `C_last`/`R_turn` repartent à la première mesure du nouveau profil ; la session peut continuer, sans valeur de l'ancien modèle | EX-06 |
| **Bascule de compte** | coût/cache revalidés ; `Cost` ne mélange pas forfait et API ; `W` seulement si le modèle et le harnais sont inchangés et la métadonnée re-annoncée | EX-06, EX-12 |
| **Bascule de harnais / version** | nouveau profil ; conventions d'usage et de cache changent ; aucune comparaison brute avec l'ancien ; session réouverte si le harnais/le compte/le dossier change (règle actuelle) | EX-03, EX-06 |
| **Cache inclus / exclu** | appliquer la table §2.2 ; jamais additionner `input` et `cached` pour Codex ; jamais comparer un total Claude à un total Codex | EX-03 |
| **Sous-agent** | mesuré à part ; exclu de `C_last`, `R_turn`, `R_session` du fil principal ; jamais un point sûr du tour principal | EX-04 |
| **Tour à plusieurs appels** | `O` est la sortie du tour, jamais celle du dernier appel ; `C_next` utilise seulement `O_last` ; si `O_last` n'est pas publié, projection `null`, pas de décision en pourcentage | EX-04, EX-05 |
| **Interruption avant résultat** | usage par appel s'il existe (`partial`) ; sinon `null` ; pas de `0` ; `O_last` du dernier appel s'il est publié, sinon projection `null` ; Codex sans relevé d'appel : pas de rotation sur la somme ; le tour de résumé raté devient le dernier tour mesuré | EX-04, EX-05 |
| **Compactage incomplet** | pas de `post_tokens` → garder l'avant, marquer `partial`, ne pas inventer l'après ; retry borné ; rotation si le plafond dur est atteint | EX-08 |
| **Tour de résumé** | mesure `C_last` par appel si le flux en publie ; sinon repli `R_turn` du dernier tour, nommé `fallback`, **jamais** pour Codex | EX-05 |
| **Session reprise après redémarrage** | `S_hist` relu du grand livre ; `C_last`/`R_turn` non rechargés ; la décision de fenêtre attend le tour suivant | EX-04, EX-05 |
| **Fenêtre absente** | `W=null` ; pas de compaction en pourcentage ; plafond absolu et rotation legacy seulement | EX-07 |

### 6.3 Exigences et traçabilité

| Id | Exigence | Où |
|---|---|---|
| EX-01 | chaque grandeur a unité, périmètre, source, cache, remise à zéro, déduplication, absence | §2.1, §2.2 |
| EX-02 | `C_last`, `O_last`, `C_next_input`, `C_next`, `R_turn`, `R_session`, `O`, `D`, `Cost` sont distingués | §2, §5 |
| EX-03 | conventions de cache par harnais, sans comparaison brute | §2.2, §6.2 |
| EX-04 | déduplication : appel, blocs, cumuls, sous-agents, reprise | §2.1, §2.4, §6.2 |
| EX-05 | absence = `null` + raison ; repli nommé ; jamais `0` ni valeur supposée | §2.1, §6.2 |
| EX-06 | profil = modèle effectif + harnais/version + mode + rôle ; override traçable ; revalidation | §3 |
| EX-07 | fenêtre runtime, principale, jamais auxiliaire/précédente/catalogue par défaut | §3.1, §3.3, §6 |
| EX-08 | compaction native : support, seuil, événements, échec, contrôle, sans généralisation | §4 |
| EX-09 | table de décision : cumul → clôture ; fenêtre → entretien ; lot/identité → dédié ; sécurité → priorité | §5 |
| EX-10 | compatibilité legacy, `0`/vide, aucune valeur changée | §5.3 |
| EX-11 | les cinq reproductions et les cas limites sont rattachés à une exigence | §6 |
| EX-12 | coût de forfait ≠ facture API ; budget ≠ pourcentage de fenêtre | §2.1, §5.2, §6.2 |

---

## 7. Conformité, vérification et suite

### 7.1 Ce que ce lot livre

* ce contrat `ameesh-context/1` et ses profils ;
* les cinq reproductions rattachées à une exigence ;
* la lecture exacte des noms legacy et des valeurs `0`/vide ;
* une liste d'écarts documentaires à corriger hors de cette empreinte.

### 7.2 Ce que ce lot ne fait pas

* aucun changement de `context_max_tokens`, `session_max_tokens`,
  `session_policy`, `resident_reread_max`, `turn_max_seconds` ;
* aucune migration, aucun nouveau champ de base, aucun nouveau réglage ;
* aucune activation du résident, aucun essai de modèle, aucun déploiement ;
* aucune décision sur les valeurs cibles (seuil codex, part de compactage,
  réserves d'outils) : elles appartiennent à CTX-02/03/04/05 et à l'owner.

### 7.3 Vérification attendue des lots suivants

* **CTX-02** : fixtures par harnais pour `C_last`, `O_last`, `C_next_input`,
  `C_next`, `R_turn`, `S_hist`, cache écrit, blocs répétés, cumuls Codex,
  sous-agents, interruption.
* **CTX-03** : `modelUsage` avec auxiliaire en premier ; alias ; bascule de
  modèle ; `post_tokens` absent ; fenêtre `null`.
* **CTX-04** : budget de tour opt-in → clôture sûre sans rotation ; fenêtre
  connue → compactage/renouvellement ; budget legacy inchangé.
* **CTX-05** : rejeu des 363 tours/105 arrêts, des cinq reproductions et des
  scénarios multi-tours ; décisions observées vs contrefactuelles séparées.
* **CTX-06** : pilote orchestrateur, bilans 24 h/48 h, retour legacy.

### 7.4 Inconnues nommées (à ne pas combler par supposition)

* seuil natif exact de Claude `--autocompact auto` ;
* seuil natif exact de Codex `model_auto_compact_token_limit` et portée
  `total` / `body_after_prefix` ;
* politique native de DeepSeek/dsh et ACP (support, seuil, événements) ;
* réserves numériques de sortie, de consigne et d'outils pour `C_next` ;
* table d'alias de modèles et de fenêtres, si elle est nécessaire ;
* valeurs cibles par rôle (CTX-04/CTX-06).

Toute inconnue qui bloque une décision est nommée dans la télémétrie ; elle ne
devient jamais une valeur par défaut silencieuse.

### 7.5 Journal des versions

| Version | Date | Changement |
|---|---|---|
| `ameesh-context/1` r1 | 2026-10-29 | création : contrat, profils, décisions, cinq reproductions, compatibilité legacy |
| `ameesh-context-corpus/1` r1 | 2026-10-31 | corpus de rejeu CTX-05a (§8) |

---

## 8. Corpus de rejeu — `ameesh-context-corpus/1`

CTX-05a (#5134) reconstruit un corpus de tours, **en lecture seule**, à partir
de deux sources : les événements `turn.context` publiés par CTX-02b (#4870) et
le grand livre `turn_costs`. Il alimente le rejeu contrefactuel de CTX-05b ;
il ne prend aucune décision et ne rejoue aucune politique.

### 8.1 Forme

Un fichier JSONL : une ligne par tour, schéma `ameesh-context-corpus/1`.
Chaque ligne ne porte que les clés de la liste blanche du module
`src/ameesh/context_corpus.py` ; une clé inconnue est refusée à la lecture
comme à l'écriture. Aucun prompt, résultat d'outil, texte de courrier, secret
ni chemin privé n'est recopié. Les mesures absentes restent `null` avec une
raison, jamais `0` (§2.4).

| Champ | Contenu |
|---|---|
| `schema` | `ameesh-context-corpus/1` |
| `turn_id`, `agent`, `started_ts`, `ended_ts`, `duration_s` | identité et fenêtre du tour ; `started_ts`/`ended_ts` viennent de la publication |
| `harness`, `model`, `model_quality`, `mode`, `role` | profil effectif du tour |
| `session` | identifiant **opaque** : `sha256:` + 32 hex, lié à l'agent ; jamais l'identifiant brut |
| `session_seq` | rang du tour dans sa session (1..N), stable et suffisant au rejeu |
| `W` | fenêtre du modèle effectif : `value`, `quality`, `reasons`, `granularity`, `source` (provenance runtime) |
| `auxiliary_models` | modèles auxiliaires observés (jamais utilisés pour `W`) |
| `C_last`, `O_last`, `C_next_input`, `C_next`, `R_turn`, `R_session`, `R_session_base`, `O`, `S_hist`, `cache_read_tokens`, `cache_write_tokens` | mesures au même format `{value, quality, reasons, granularity, source}` ; `R_session_base` est le cumul de relecture **avant** le tour |
| `decision` | décision **observée** : `observed` ∈ `none`/`close`/`rotation`/`compaction`, `cause` normalisée, `compacted` (vrai si une compaction est publiée, `null` sinon), et les blocs `close`, `rotation`, `compaction`, `window` ; `compaction.observed`/`requested` séparent la réussite de la demande |
| `policy` | politique en vigueur au tour : `session`, `context_max_tokens`, `session_max_tokens`, budgets opt-in, part d'entretien |
| `legacy` | les onze compteurs legacy publiés à côté des mesures |
| `quality`, `reasons` | qualité d'ensemble (mesures principales) et raisons, triées |
| `source` | `event_id`, `observation_id`, `ledger_id`, schéma source, hôte |

La publication `turn.context` de CTX-02b ne porte pas encore le profil CTX-03 : sur le corpus réel, `W` reste donc `null` avec `window_unknown`, et `C_next_input`/`C_next` restent `null` hors opt-in CTX-04b. Les fixtures R1 et R4 portent, elles, une fenêtre observée. Aucune valeur n'est supposée pour combler ces absences.

`S_hist` est reconstruit du grand livre : somme des `input_tokens +
output_tokens` des lignes `turn_costs` de la session jusqu'au tour inclus.
`R_session_base` suit la même lecture : somme des entrées relues (cache lu
compris, Codex entrée seule) des lignes **précédentes** ; sans ligne de grand
livre, il se déduit de `R_session - R_turn`, marqué `derived` ou `partial`. Un
`turn_id` sans ligne de grand livre reprend le `session_tokens` legacy de
l'événement, marqué `fallback` avec `ledger_row_missing` ; un écart entre les
deux marque `session_tokens_mismatch` et laisse la ligne `partial` — le grand
livre reste la mesure de référence, l'écart est nommé.

`C_next_input` et `C_next` ne sont publiés que si la décision de fenêtre les a
publiés (`decision.window`) ; sinon ils restent `null` avec
`projection_not_published`. Une compaction **demandée** n'est pas une
compaction réussie : `post_tokens` reste `null`, la raison
`post_tokens_unknown` le dit (§4.2).

### 8.2 Export et bornes

```
ameesh context corpus export --out FILE [--days N | --since T] [--until T]
                             [--harness H] [--batch N] [--force]
ameesh context corpus check FILE
```

L'export est borné par la fenêtre d'insertion des événements et par le
harnais ; il lit le grand livre de chaque session une fois. Le corpus réel
n'est **pas** versionné dans le dépôt : l'écriture refuse un chemin dans un
arbre Git et pose le fichier en `0600`. Un événement hors schéma ou sans agent
est écarté avec sa raison, jamais transformé en ligne inventée.

### 8.3 Reproductions et vérification

Les cinq reproductions R1 à R5 de §6.1 sont versionnées dans
`tests/fixtures/ctx05/` et se relisent avec `read_corpus` : ordre de
`modelUsage` (R1), écriture de cache Claude (R2), dix appels Codex (R3),
400 k cumulés pour un `C_last` de 50 k et `W` de 1 M (R4), garde 150 k et
conventions de cache (R5). Les tests vérifient la liste blanche, l'absence en
`null` avec raison, la session opaque, l'écriture `0600` hors dépôt et
l'export borné.

---

## Annexes

### A. Base factuelle datée

Source : plan `e18`, conversation
`conversation:smichea:2026-10-17:roadmap-gestion-contexte`.

* Ameesh 1.11.4 au moment de l'étude ; 41 unités runner actives, `resident
  off` ; 288 sessions échantillonnées. Le canon appliqué au 2026-10-29
  (`5a488730`) n'a pas de bloc `session` : les trois harnais retombent sur le
  seuil commun de 150 000.
* Rejeu fixe du 2026-10-16 02:19:40 au 2026-10-17 02:19:40 Europe/Paris :
  363 tours d'orchestrateur, 105 arrêts cumulés à 400 000 (plafond de l'agent
  observé, pas le défaut de 15 M), tous rapprochés des compteurs du flux.
* Sur les tours coupés, contexte d'entrée maximal par tour : médiane 53 114,
  maximum 93 714 ; fenêtre Claude principale annoncée 1 000 000. Ce ne sont pas
  des fenêtres pleines.
* Fenêtres observées : `gpt-6.1-sol` via Codex exec 475 000 ;
  `claude-opus-5-5` via Claude stream-json 1 000 000 ; `deepseek-flash` via dsh
  headless 1 000 000. Métadonnées runtime, pas catalogue universel.
* Cinq reproductions : ordre des modèles dans `modelUsage` ; création du cache ;
  granularité de remontée Codex ; priorité du budget cumulé sur le compactage ;
  garde historique 150 000 et conventions de cache différentes.

### B. Références de code (main `a02b97c9b`)

* `src/ameesh/adapters.py` : `usage_tokens`, `reread_tokens`,
  `context_tokens` ; `ClaudeStream`, `CodexStream`, `DshStream`, `AcpStream`.
* `src/ameesh/runner.py` : `AgentWorker.rotation_due`, `context_rotation_due`,
  `turn_close_due`, `last_turn_context_reread`, `_codex_last_call_context`,
  `_restore_session_counters`.
* `src/ameesh/cost.py` : `TurnUsage`, `TurnUsage.reread_tokens`,
  `CostBook.turn_usage`, `CostBook.record`.
* `src/ameesh/storage/postgres/costs.py` : `session_total` (#4448).
* `src/ameesh/mechanics.py` : `Session`, `Document.session_max_tokens`,
  `mechanics.session_max_tokens` (#4448).
* `src/ameesh/exploitation.py` : `alerts`, seuil `session_too_big` (#4448).
* `src/ameesh/resident_runner.py` : `turn_reread`, `maintenance`,
  `COMPACT_SHARE`, `COMPACT_RETRY_S`, `DEFAULT_REREAD_MAX`.
* `src/ameesh/resident.py` : `TurnResult`, `_on_result`, `_on_compact`.
* `src/ameesh/harness_versions.py` : `probe`, `parse_version`.
* Tests : `tests/test_4448_rotation_par_harnais.py`,
  `tests/test_2932_rotation_codex.py`, `tests/test_l60_rotation_deepseek.py`,
  `tests/test_l81_resident_rotation.py`, `tests/test_l75_resident.py`.
* Docs : `docs/EXPLOITATION.md` (L60, L105, alertes), `docs/V1-MAILBOX-RUNNER.md`,
  décision 0025.

### C. Glossaire

* **appel** : une requête au modèle, identifiée par un `call_id` quand le
  harnais en publie un.
* **fil principal** : la conversation de l'agent ; les sous-agents en sont
  exclus pour les plafonds et les décisions de session.
* **fenêtre** : nombre maximal de jetons de contexte du modèle effectif.
* **occupation** : jetons réellement présents dans le contexte à un instant
  donné (`C_last`) ; **projection** : estimation pour le prochain appel
  (`C_next`).
* **relecture** : jetons d'entrée relus par un appel ou un tour, cache lu
  compris ; ce n'est pas l'occupation de la fenêtre.
* **rotation** : résumé dans l'ancienne session puis nouvelle session (L11).
* **renouvellement** : décision de rotation ou de compactage à une frontière
  de tour, selon la politique.

## 9. Rejeu contrefactuel — `ameesh-context-replay/1`

CTX-05b (#5135) rejoue un corpus §8 à travers les fonctions livrées
(`AgentWorker.reread_budget_due`, `resident_runner.window_maintenance`), sans
les recopier. Rien n'est activé : aucune écriture au registre ni au grand livre ;
le rapport s'écrit hors dépôt, en `0600`.

```
ameesh context replay --corpus FILE --candidates FILE --out FILE [--force]
```

Les réglages candidats sont une entrée, jamais une valeur du banc :
`{"candidates": [{"id", "match": {harness, model, role}, "settings": {…}}]}`, la
première entrée qui correspond s'applique. Réglages admis :
`turn_reread_budget`, `session_reread_budget`, `window_maintenance_share`,
`window_input_reserve`, `window_output_reserve`, `context_max_tokens`,
`session_max_tokens`, `session_min_turns`, `session_policy`. Un plafond ou la
politique absents reprennent la valeur observée au tour ; le reste absent demeure
éteint ou inconnu, et nommé.

Chaque tour porte `observed` (lu du corpus) et `counterfactual` (`decision`,
`cause`, `guard`, `reread`, `window`, `unknowns`), séparés ; `decision` à `null`
= pas de rejeu (aucun candidat, ou session divergente). La synthèse par profil
donne les écarts, les rotations évitées ou ajoutées (`unknown` : session non comparable, jamais comptée évitée), les inconnues et `judgeable`
(tours avec un `C_last`, tours où CTX-04b peut juger) ; rien n'est additionné entre
harnais. `decision.compacted` à `null` (non publié) vaut mesure inconnue pour la
projection. Les limites (mesures de fin de tour, divergence après
compaction ou rotation contrefactuelle, plafond de durée non rejoué) sont reprises
dans le rapport.
