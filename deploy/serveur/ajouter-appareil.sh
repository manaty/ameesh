#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Ajoute un appareil (poste d'un humain, autre serveur) au réseau WireGuard du
# mesh (lot L55). À lancer en root sur le serveur :
#
#   ajouter-appareil.sh <nom>        → affiche la configuration WireGuard de l'appareil
#   ajouter-appareil.sh --liste      → appareils déclarés
#
# La clé privée de l'appareil n'est affichée qu'une fois et n'est pas gardée.
set -euo pipefail
CONF=/etc/wireguard/wg0.conf
NET=10.77.0
if [[ ${1:-} == --liste ]]; then grep -E '^# appareil ' "$CONF" || echo "aucun appareil"; exit; fi
nom=${1:?nom de l\'appareil}
[[ $nom =~ ^[a-z0-9-]{1,32}$ ]] || { echo "nom invalide : $nom" >&2; exit 2; }
grep -q "^# appareil $nom " "$CONF" && { echo "$nom est déjà déclaré" >&2; exit 2; }
for n in $(seq 2 254); do grep -q "AllowedIPs = $NET.$n/32" "$CONF" || break; done
cle=$(wg genkey); pub=$(echo "$cle" | wg pubkey)
cat >> "$CONF" <<CONF

# appareil $nom $NET.$n
[Peer]
PublicKey = $pub
AllowedIPs = $NET.$n/32
CONF
wg syncconf wg0 <(wg-quick strip wg0)
ip=$(curl -fsS --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
port=$(awk -F' *= *' '/^ListenPort/{print $2}' "$CONF")
cat <<OUT
# --- /etc/wireguard/ameesh.conf sur l'appareil « $nom » (clé privée : à garder secrète) ---
[Interface]
Address = $NET.$n/32
PrivateKey = $cle

[Peer]
PublicKey = $(cat /etc/wireguard/serveur.pub)
Endpoint = $ip:$port
AllowedIPs = $NET.1/32
PersistentKeepalive = 25
# --- puis : wg-quick up ameesh ; base du mesh : postgresql://ameesh@$NET.1:5432/ameesh?sslmode=require
OUT
