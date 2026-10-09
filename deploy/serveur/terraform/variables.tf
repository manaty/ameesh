# SPDX-License-Identifier: AGPL-3.0-only

variable "scw_profile" {
  description = "Profil Scaleway NOMMÉ de l'organisation (ex. « manaty »). Obligatoire : pas de profil par défaut."
  type        = string
}

variable "organization_id" {
  description = "Identifiant de l'organisation Scaleway attendue ; Terraform refuse d'agir si le projet n'en fait pas partie."
  type        = string
}

variable "project_id" {
  description = "Projet de l'hébergeur où créer la VM : celui de l'ORGANISATION du mesh (0033 §1), jamais celui d'une autre."
  type        = string
}

variable "organisation" {
  description = "Identifiant court de l'organisation (étiquette, nom du mesh)."
  type        = string
}

variable "name" {
  description = "Nom de la VM."
  type        = string
  default     = "ameesh-mesh"
}

variable "instance_type" {
  description = "Gabarit de la VM. Les agents lancent des tests (conteneurs, suites JS) : prévoir de la mémoire."
  type        = string
  default     = "DEV1-L"
}

variable "disk_gb" {
  description = "Taille du volume racine (Go) : sessions, dépôts de travail, images de conteneurs. 80 Go de disque local sont inclus dans le prix d'une DEV1-L."
  type        = number
  default     = 80
}

variable "volume_type" {
  description = "Type du volume racine : « l_ssd » (local, inclus pour les DEV1) ou « sbs_volume » (bloc, facturé à part)."
  type        = string
  default     = "l_ssd"
}

variable "ssh_cidrs" {
  description = "Plages autorisées en SSH (administration). Vide = SSH fermé, accès par WireGuard seulement."
  type        = list(string)
  default     = []
}

variable "admin_ssh_keys" {
  description = "Clés SSH publiques des SEULS administrateurs du mesh (accès root). Les autres clés de l'organisation chez l'hébergeur ne sont pas installées."
  type        = list(string)
  validation {
    condition     = length(var.admin_ssh_keys) > 0
    error_message = "Au moins une clé d'administrateur."
  }
}

variable "wireguard_port" {
  description = "Port UDP de WireGuard."
  type        = number
  default     = 51820
}

variable "ameesh_ref" {
  description = "Version d'ameesh installée (étiquette ou commit de manaty/ameesh)."
  type        = string
  default     = "main"
}

variable "zone" {
  type    = string
  default = "fr-par-1"
}

variable "region" {
  type    = string
  default = "fr-par"
}
