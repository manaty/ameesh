# #6321 — AG3 rootless, dossier v2 : delta de v1 selon les réponses de mesh-design (#200904)

Bureau (BG), 08/11 ~14:05Z. Branche `review/bureau-6321-ag3-rootless`. La v1 est conservée.

**AUCUN GESTE, et aucune commande lancée sur une VM** pour ce delta (consigne du coordinateur). Les
nouvelles fonctions ont été éprouvées localement (`temoin-6321-v2.py`, 10/10), avec des sorties
simulées d'après les relevés v1 et `deploy/guest/install.sh`.

Le feu `--apply` de **chaque** phase revient à mesh-design, le 09/11.

| Point | Réponse du pilote | v2 |
|---|---|---|
| O1 | Seules les 4 images de base | Inchangé : `IMAGES` = postgres:17, postgres:16, node:22-bookworm-slim et ubuntu:24.04, chargées par identifiant. Les images construites par les tours restent dans le rootful. Le **nettoyage du rootful** (images et volume anonyme `b8cbe748`) se fera dans un **lot séparé**, après les 7 jours de conservation des sources |
| O2 | containerd 24.04.4 accepté | `containerd_proof()` : `dpkg --verify` (déjà fait), version (`dpkg-query`) et origine (`apt-cache policy`, `*** <version> 500` puis `noble-updates/main` exigés). Si `/var/cache/apt/archives/containerd_<version>_amd64.deb` existe, son sha256 doit égaler le `SHA256` de l'index `*_dists_noble-updates_main_binary-amd64_Packages`, sinon refus. Si le `.deb` est absent du cache, c'est dit (« non comparé »). Témoins : égal → OK, `.deb` altéré → refus, autre origine → refus, `.deb` absent → dit |
| O3 | Les 4 PG | Inchangé (déjà le cas) |
| O4 | Laisser le pause Podman | Le critère 1 de la phase B est explicite dans le code : aucun processus uid 1001 avec 112, **pause Podman du 08/11 13:42Z compris**. Aucun geste séparé, aucune commande podman |
| O5 | 09/11, 13:00-15:30Z, après C3 | `--apply` refusé hors de la fenêtre (`WINDOW`), en plus de l'exclusion des coupures |
| O6 | Clos (#5784 livré) | Retiré des points ouverts |
| O7 | Preuve en phase C | `ports_and_guest_gate()` (lecture) : `ss -ltnH` montre 55432 et 55441 à 55443 liés **uniquement** à 127.0.0.1 (jamais 0.0.0.0 ni ::). `/etc/ameesh-guest/firewall.nft` (règle par uid, `tcp dport { … } reject`, install.sh l.594) refuse les 4 ports. Témoins : conforme → OK ; un port en 0.0.0.0 → refus ; un port sur :: → refus ; port absent de la garde → refus |

Le reste de v1 est inchangé : phases, retour, critères, leçons de #5781.

Pièces v2 :
- `ag3-rootless-6321-v2.py` ;
- `ag3-rootless-6321-v1-v2.diff` (diff explicite) ;
- `temoin-6321-v2.py` ;
- `SHA256SUMS` mis à jour.

Relecture : impl-11 (codex) et impl-03 (deepseek), après leurs deltas C3 v2.3 et #6002 v4.3/v4.4.

Points ouverts restants :
- le libellé exact de l'autorité de chaque phase, à écrire au feu ;
- la confirmation que `/var/lib/apt/lists` n'est pas compressé sur AG3. S'il l'est, la comparaison
  du `.deb` refuse avec un motif nommé, et un avis du pilote sera nécessaire.
