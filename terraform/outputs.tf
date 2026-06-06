output "resource_group_name" {
  description = "Name of the created resource group."
  value       = azurerm_resource_group.main.name
}

output "storage_account_name" {
  description = "Storage account that holds the vault blobs."
  value       = azurerm_storage_account.vault.name
}

output "vault_container_name" {
  description = "Blob container name."
  value       = azurerm_storage_container.vault.name
}

output "tenant_id" {
  description = "Azure AD tenant ID."
  value       = data.azurerm_client_config.current.tenant_id
}

output "client_id" {
  description = "Azure AD application (client) ID for the sync app."
  value       = azuread_application.sync.client_id
}

output "key_vault_name" {
  description = "Key Vault name where the client secret is stored."
  value       = azurerm_key_vault.main.name
}

output "key_vault_secret_name" {
  description = "Name of the secret inside Key Vault."
  value       = azurerm_key_vault_secret.client_secret.name
}

output "sync_config_snippet" {
  description = "Copy this into your sync_client/config.yaml on each machine."
  sensitive   = false
  value = yamlencode({
    tenant_id            = data.azurerm_client_config.current.tenant_id
    client_id            = azuread_application.sync.client_id
    storage_account_name = azurerm_storage_account.vault.name
    container_name       = azurerm_storage_container.vault.name
  })
}
