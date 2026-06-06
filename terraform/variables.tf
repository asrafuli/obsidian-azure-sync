variable "location" {
  description = "Azure region for all resources."
  type        = string
  default     = "eastus"
}

variable "environment" {
  description = "Environment tag (e.g. prod, dev)."
  type        = string
  default     = "prod"
}

variable "vault_container_name" {
  description = "Blob container name that stores the Obsidian vault."
  type        = string
  default     = "obsidian-vault"
}

variable "storage_replication" {
  description = "Storage account replication type (LRS, GRS, ZRS, etc.)."
  type        = string
  default     = "GRS"
}

variable "allowed_ip_ranges" {
  description = "Optional list of IP CIDR ranges allowed to access the storage account. Leave empty to allow all IPs (default)."
  type        = list(string)
  default     = []
}

variable "key_vault_sku" {
  description = "Key Vault SKU (standard or premium)."
  type        = string
  default     = "standard"
}

variable "tags" {
  description = "Additional tags to apply to all resources."
  type        = map(string)
  default     = {}
}
