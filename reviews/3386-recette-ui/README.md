# Recette live `/ui/` — lot #3386 (U1)

**Statut : fait** (recette agent ; aucun code produit, aucun déploiement, aucune fusion).
Date : 2026-10-24 18:42–19:05Z · hôte : **ameesh-agents-1** (VM staging, pas pc-smichea).

| | |
|---|---|
| Version recettée | `f71d2bd478dc49ead174416bece221c646586220` (`venv-courant`, ameesh 1.20.0) |
| Serveur | `ameesh serve --port 8780 --human human:smichea`, écoute **127.0.0.1 seulement** |
| URL | <http://127.0.0.1:8780/ui/> |
| Navigateur | Chromium 156 headless (Playwright 1.64.1), viewport 1440×1000, `fr-FR`, `Europe/Paris` |
| Jeton | `~/.config/ameesh/serve-token` (0600, créé au premier démarrage puis supprimé ; jamais dans ce dépôt) |
| Service avant / après | absent avant ; **arrêté après** (0 écoute sur 8780, `service-after.txt`) |

Le serveur est en mode local : les captures ont été prises sur la VM staging elle-même, avec un
navigateur headless, par la même origine que `/ui/`. C'est une recette de l'interface et du flux
temps réel, pas une exposition réseau.

## Résultat : les 4 écrans sont alimentés, « En direct »

| Écran | Capture | Données vues (24/10) |
|---|---|---|
| Projets | [01-projets.png](shots/01-projets.png) | 3 projets : ameesh (45 lots ouverts, 25 non lus), nexlink (23, 4), omarchy (0, 4471) ; dépenses 24 h ; statut **En direct** |
| Chronologie | [04-chronologie.png](shots/04-chronologie.png) | projet `ameesh`, fenêtre 23–24 oct., 153 lignes (état, demandé, gelé, relu, fusionné), statut **En direct** |
| Agent | [02-agent.png](shots/02-agent.png) | `ameesh-dev5`, Au travail, hôte ameesh-agents-1, harnais deepseek, lot #3386, activité courante, statut **En direct** |
| Décisions | [03-decisions.png](shots/03-decisions.png) | 6 demandes en attente, dont #132239 et #132995 (échéances, options, recommandation), statut **En direct** |

La page de connexion est aussi conservée : [00-connexion.png](shots/00-connexion.png).
L'en-tête affiche « Maillage **0.1.0** » alors que le paquet est 1.20.0 : c'est le défaut #3394
ci-dessous, pas un état voulu.

## Flux SSE : connecté, et rafraîchissement observé sans rechargement

- `GET /api/v1/events` → **200**, `Content-Type: text/event-stream`, `X-Accel-Buffering: no`
  ([en-têtes](evidence/sse-headers.txt)). Trames brutes de 35 s dans
  [sse-raw.txt](evidence/sse-raw.txt) : `retry: 3000`, heartbeat, `id: <tx-id-cluster>`,
  `event: lot.updated|thread.entry|turn.started|cost.recorded`.
- Dans le navigateur, **une seule connexion** partagée par les vues : 11 trames / 5 771 octets /
  19 événements sur ~66 s, sans erreur ([probe-report.json](evidence/probe-report.json)).
- Preuve fonctionnelle : le fil du lot #3386 était ouvert (`Fil`), un courrier a été déposé dans
  ce fil (`agent-mail send … --lot 3386`). Il est apparu **974 ms** plus tard, sans rechargement,
  et le compteur d'événements SSE est passé de 15 à 19 :
  [05-fil-avant.png](shots/05-fil-avant.png) → [06-fil-apres.png](shots/06-fil-apres.png).

## Défauts trouvés → lots séparés

| Lot | Constat | Mesure |
|---|---|---|
| **#3394** | `/.well-known/ameesh` et l'en-tête `/ui/` annoncent **0.1.0** (protocole ACP) au lieu de la version du paquet **1.20.0**. `serve.discovery()` utilise `__version__` au lieu de `version()`. | `discovery.json`, capture 01 |
| **#3395** | L'écran **Chronologie** charge le snapshot `progress` complet : 39–48 s et ~1,1 Mo par lecture, alors qu'il n'affiche que `lots` + `window` (agents 482 ko et roadmap 394 ko inutiles). Le rafraîchissement SSE peut relancer ce calcul en boucle. | 39,6 / 38,5 s ; 48 s dans le navigateur ; `timings.txt` |
| **#3397** | L'écran **Alertes** (`GET /api/v1/alerts`) répond en **93–102 s** (CLI `ameesh alerts --json` : 72 s). Hors des 4 écrans demandés, mesuré sur le même serveur. | 93,1 / 95,7 / 101,7 s ; `timings.txt` |

Observation non bloquante (pas de lot) : avant la saisie du jeton, `<ameesh-app>` tente déjà
`GET /api/v1/me` et laisse un **401** dans la console ; aucun effet visible, le formulaire de jeton
s'affiche normalement.

## Reproduction

```sh
# sur ameesh-agents-1, version staging f71d2bd
/home/ameesh/.local/share/ameesh/venv/bin/ameesh serve --port 8780 --human human:smichea &
TOKEN=$(cat ~/.config/ameesh/serve-token)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8780/.well-known/ameesh
# navigateur (Playwright) : ouvrir http://127.0.0.1:8780/ui/, saisir $TOKEN, capturer les 4 écrans
# preuve SSE directe :
curl -sN -H "Authorization: Bearer $TOKEN" -H 'Accept: text/event-stream' \
     http://127.0.0.1:8780/api/v1/events
kill %1   # le service n'était pas lancé avant la recette
```

Les mesures, le rapport machine du navigateur, les en-têtes et les trames SSE sont dans
[`evidence/`](evidence/). Les captures sont des PNG 1440×1000 non retouchés.

## Limites

- Recette agent headless, pas une séance owner : aucune manipulation de fenêtre ou de clavier.
- Le serveur étant `127.0.0.1` par construction, l'URL n'est joignable que depuis l'hôte staging ;
  c'est le mode local v1, pas un défaut de la recette.
- Les temps de la Chronologie et des Alertes dépendent de la charge de la base au moment de la
  mesure ; ils ont été répétés (39–48 s et 93–102 s) et recoupés avec la CLI.
