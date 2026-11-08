# #6321 — AG3 rootless, dossier v4 : delta de v3 (avis impl-03 #200943)

Bureau (BG), 08/11 vers 14:20Z. **AUCUN GESTE, aucune commande sur une VM.** Les versions v1 à v3
sont conservées.

impl-03 a relu la v2. Ses B1 et B2 sont ceux d'impl-11, déjà traités en v3, et restent couverts :
les témoins v3 passent sur la v4. La v4 traite son **B3** et ses N applicables.

## B3 — le témoin de lecture protégée de la phase C était un faux positif

**Défaut (v1 à v3).**
- La fixture se trouvait sous `ROOT`, en root 0700. Le démon rootless (uid 1001) ne peut pas
  traverser ce répertoire.
- `docker run` échouait donc **au montage**, et le contrôle `rc ≠ 0` passait sans prouver que le
  conteneur ne lit pas le fichier.

**v4 : `protected_read_probe()`.**
- Fixture root **0600** dans `/var/lib/ameesh-operator-6321-fixture`. Ce dossier est en root
  **0711** : le démon peut le traverser pour monter le fichier, sans pouvoir le lister. Il est créé
  et retiré par la sonde.
- La preuve n'est acceptée que si **le conteneur démarre**, que `cat` rend rc 1 avec
  « Permission denied » sur stderr, et que stdout est vide.
- Toute erreur de montage ou de démarrage (`Error response from daemon`, `invalid mount`, `OCI runtime`,
  `no such file or directory`) donne KO, et non une preuve.

**À signaler, sans le corriger ici (lot séparé) : le même défaut existe dans
`ref-5781/phase-C-local-verification.py`, l. 53-62.**
- Sur AG1, la fixture était sous `/root/ameesh-operator-5781-AG1-20261104` (0700).
- Le **critère 6 de #5781** (« lecture protégée refusée ») n'est donc **pas prouvé**.
- Il faut le revérifier sur AG1 avec une fixture traversable : lot à ouvrir par mesh-design.

## N appliqués à la v3

| N (#200943) | v4 |
|---|---|
| Santé exigée sans attente après le redémarrage du gestionnaire (phase B) | Boucle avec délai de 180 s par PG |
| `approved_active_units` incomplet | `docker.service` et `dbus.service` exigés dans la liste approuvée. Les scopes actifs doivent y être aussi : le contrôle existant arrête la phase avant tout geste si une unité ou un scope vivant n'est pas approuvé |
| Fenêtre entre `gpasswd --delete` et le drapeau | `docker_group_removal_intent` sauvé **avant** `gpasswd`. Le retour suit l'**état réel** : si l'intention est consignée et qu'ameesh est absent du groupe, il est rétabli ; sinon rien |
| Valeurs des avis non vérifiées | `all(isinstance(v, int) and v > 0 …)` |
| Pas de `sudo -n -l` sous ameesh | Témoin direct ajouté : `sudo -n -l` sous ameesh doit rendre un rc non nul, en plus de `sudo -l -U` |
| Restauration incomplète pour les répertoires | L'instantané note les répertoires absents (`.docker`, `.config/docker`, `.config/environment.d`). Le retour les retire s'ils sont vides, et garde puis **signale** les répertoires non vides |
| `ROOT` créé avant la garde de fenêtre | Le script ne crée plus jamais `ROOT` : l'opérateur le crée (0700) en y écrivant l'autorité. Un `--apply` hors fenêtre ne laisse donc rien |
| Preuve O7 après le SQL | `ports_and_guest_gate()` passe désormais **avant** le SQL, juste après la lecture protégée |
| `nft list ruleset` | Déjà fait en v3 |
| Index apt compressé (O2) | Inchangé : refus nommé si c'est le cas, à confirmer au feu |

## Témoins (`releve-6321-v4-temoins.txt`) : 30/30, aucun échec

| Témoin | Résultat |
|---|---|
| `temoin-6321-v4.py` | **11/11** |
| `temoin-6321-v3.py` (Docker simulé, B1/B2), relancé sur v4 | 8/8 |
| `temoin-6321-v3-fonctions.py`, relancé sur v4 | 11/11 |

Détail de `temoin-6321-v4.py` :
- **B3** :
  - « Permission denied » → OK, avec le dossier en 0711 et la fixture en 0600 au moment du montage ;
  - **erreur de montage → KO** (c'est le faux positif de v3) ;
  - contenu lu → KO ;
  - rc 1 sans refus → KO ;
  - le dossier de fixture est retiré dans tous les cas.
- **Groupe** :
  - intention sauvée et ameesh absent → rétabli ;
  - intention sauvée et ameesh présent → rien ;
  - sans intention → rien.
- **Restauration**, exécutée réellement dans un HOME jetable :
  - `.config/docker` créé par la pose → retiré ;
  - `environment.d` contenant un fichier étranger → gardé et signalé ;
  - fichier de la pose → retiré.

## Pièces

- `ag3-rootless-6321-v4.py`
- `ag3-rootless-6321-v3-v4.diff` (diff explicite)
- `temoin-6321-v4.py`
- `releve-6321-v4-temoins.txt`
- `SHA256SUMS`

Le feu `--apply` de chaque phase reste à mesh-design, le 09/11.
