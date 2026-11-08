# #6002 — pose AG3 du compte VM non root de l'adaptateur (README #6276, prép. 1, 2 partiel, 3, 4, 5, 7) — AUCUN geste

Auteur : bureau (BG, claude), 08/11 07:4xZ. Plan validé par mesh-design #200396. Activation : seulement après #6296 (contrôles réels) et #6298 (rôle vm_probe) actifs en staging, hors coupure, sur feu distinct.

## Relevé AG3 (lecture seule)
Aucun compte `vm-operator`, aucun `/opt/ameesh-vm-adapter` ; groupe `systemd-journal` présent ; sshd : drop-ins `50-ameesh-guest`, `60-ameesh-release`, `60-cloudimg-settings`, `AuthorizedKeysFile .ssh/authorized_keys …`, `PermitUserEnvironment no` ; AG3 en 41262fc ; DB via pgbouncer en socket sous `/home/ameesh` (inaccessible à vm-operator — d'où #6298).

## Script `pose-AG3-vm-operator.sh` (à blanc : préalables OK, aucun effet)
0. Préalables, STOP au premier écart (compte/fichiers absents, `sshd -t`, python3.12, visudo).
1. `useradd --system --shell /bin/sh` (sshd doit pouvoir lancer la commande forcée), mot de passe verrouillé, groupe `systemd-journal` seul en plus.
2. (partiel) `/opt/ameesh-vm-adapter` root:root sans g+w/o+w : source au SHA 41262fcde6bf, venv propre, `remote/vm-remote.py` 0644 ; contrôle `find` (aucun g+w/o+w, aucun non-root). **`remote.json` et connexion DB non posés** (attendent #6296/#6298) → `vm-remote.py` refuse tout sauf `true`/`journalctl` (échec fermé).
3. `/var/lib/ameesh-vm-remote` 0700 vm-operator.
4. `/etc/ssh/authorized_keys/vm-operator` root 0644 : `restrict,command="<dispatch>"` + clé publique du détenteur opérateur existant (agent@bureau-goal) ; drop-in `55-ameesh-vm-operator.conf` (Match User : AuthorizedKeysFile dédié, publickey seule, PermitTTY/forwardings/tunnel/UserEnvironment no, ForceCommand identique, `Match all` final) ; `sshd -t` ; `sshd -T` root et ameesh **identiques** avant/après ; reload sous session tenue.
5. `/etc/sudoers.d/ameesh-vm-operator` 0440 : `vm-operator ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff --no-block` seul, `visudo -cf` puis `visudo -c`.
7. `cible-ameesh-agents-3.v2.json` (BG, non installé) : `user: vm-operator`, argv du modèle 41262fcd (`/opt/ameesh-vm-adapter/remote/*`, `sudo -n systemctl poweroff --no-block`, journalctl sans sudo), probe `mty-amh-impl-15`.

## Preuves après pose (`tests-AG3-vm-operator.sh`)
`/usr/bin/true` rc 0 ; `journalctl -b -1 …` admis ; `id`, `/bin/sh -c id`, `sudo systemctl status` refusés ; `remote/quiesce` refus fermé (remote.json absent) ; PTY refusé.

## Retour arrière (`retour-AG3-vm-operator.sh`)
sudoers, drop-in sshd et clé retirés (`sshd -t`, reload), `userdel`, `/var/lib/ameesh-vm-remote` et `/opt/ameesh-vm-adapter` supprimés.

## Points à trancher par les relecteurs
- shell `/bin/sh` (nécessaire à ForceCommand) vs `nologin` (bloquerait la commande forcée) ;
- clé : réemploi de la clé opérateur existante d'agent@BG (pas de nouvelle clé) — alternative : clé dédiée ;
- l'étape 2 (venv sur AG3) pourrait réutiliser le venv root existant d'AG3 si le README l'entend ainsi (« Python isolé du venv root existant ») : ici venv dédié sous /opt.
