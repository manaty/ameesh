# SPDX-License-Identifier: AGPL-3.0-only

output "reseau_prive_id" {
  value = scaleway_vpc_private_network.base.id
}

output "adresses" {
  description = "Adresse privée de chaque nœud, à passer à reseau-prive.sh interface."
  value       = { for k, n in var.noeuds : k => "${n.adresse}/${split("/", var.sous_reseau)[1]}" }
}

output "mac" {
  description = "Adresse MAC de la carte privée de chaque nœud (reseau-prive.sh interface --mac)."
  value       = { for k, nic in scaleway_instance_private_nic.noeud : k => nic.mac_address }
}
