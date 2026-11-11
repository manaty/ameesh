# SPDX-License-Identifier: AGPL-3.0-only

variable "scw_profile" {
  description = "Profil Scaleway NOMMÉ de l'organisation. Obligatoire : pas de profil par défaut."
  type        = string
}

variable "organization_id" {
  description = "Organisation Scaleway attendue ; Terraform refuse d'agir si le projet n'en fait pas partie."
  type        = string
}

variable "project_id" {
  description = "Projet commun aux nœuds (un réseau privé ne traverse pas les projets)."
  type        = string
}

variable "organisation" {
  description = "Identifiant court de l'organisation (nom du réseau, étiquettes)."
  type        = string
}

variable "sous_reseau" {
  description = "Sous-réseau IPv4 privé, distinct de WireGuard (10.77.0.0/24) et de Docker (172.17.0.0/16)."
  type        = string
  default     = "10.78.0.0/24"
  # Règle de reseau-prive.sh (sous_reseau_valide) : forme canonique, /16 à /29,
  # entier dans une plage RFC 1918, sans recouvrir WireGuard (ni Docker ici).
  # Deux réseaux se recouvrent si leurs bases coïncident au plus court préfixe.
  validation {
    condition = try(
      cidrhost(var.sous_reseau, 0) == split("/", var.sous_reseau)[0]
      && tonumber(split("/", var.sous_reseau)[1]) >= 16
      && tonumber(split("/", var.sous_reseau)[1]) <= 29
      && anytrue([for r in ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"] :
        cidrhost("${split("/", var.sous_reseau)[0]}/${split("/", r)[1]}", 0) == split("/", r)[0]
      ])
      && alltrue([for r in ["10.77.0.0/24", "172.17.0.0/16"] :
        cidrhost("${split("/", var.sous_reseau)[0]}/${min(split("/", var.sous_reseau)[1], split("/", r)[1])}", 0)
        != cidrhost("${split("/", r)[0]}/${min(split("/", var.sous_reseau)[1], split("/", r)[1])}", 0)
      ]),
      false
    )
    error_message = "Sous-réseau invalide (forme canonique RFC 1918, /16 à /29) ou recouvrant WireGuard (10.77.0.0/24) ou Docker (172.17.0.0/16)."
  }
}

variable "noeuds" {
  description = "Serveurs rattachés, par nom d'hôte ameesh : identifiant Scaleway et adresse privée réservée (sans préfixe)."
  type = map(object({
    server_id = string
    adresse   = string
  }))
  validation {
    condition     = length(distinct([for n in values(var.noeuds) : n.adresse])) == length(var.noeuds)
    error_message = "Deux nœuds ont la même adresse privée."
  }
}

variable "zone" {
  type    = string
  default = "fr-par-1"
}

variable "region" {
  type    = string
  default = "fr-par"
}
