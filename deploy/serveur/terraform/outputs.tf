# SPDX-License-Identifier: AGPL-3.0-only

output "ip_publique" {
  description = "Adresse publique de la VM (SSH restreint, WireGuard)."
  value       = scaleway_instance_ip.mesh.address
}

output "suite" {
  value = "ssh root@${scaleway_instance_ip.mesh.address} 'cloud-init status --wait && cat /root/ameesh-serveur.txt'"
}
