# ─────────────────────────────────────────────────────────────────────────────
# Data sources
# ─────────────────────────────────────────────────────────────────────────────

data "azurerm_client_config" "current" {}
data "azuread_client_config" "current" {}

# ─────────────────────────────────────────────────────────────────────────────
# Locals
# ─────────────────────────────────────────────────────────────────────────────

locals {
  # Storage account names: 3-24 chars, lowercase alphanumeric only
  storage_suffix = substr(random_string.suffix.result, 0, 8)

  common_tags = merge(
    {
      project     = "obsidian-sync"
      environment = var.environment
      managed_by  = "terraform"
    },
    var.tags
  )
}

resource "random_string" "suffix" {
  length  = 8
  special = false
  upper   = false
}

# ─────────────────────────────────────────────────────────────────────────────
# Resource group
# ─────────────────────────────────────────────────────────────────────────────

resource "azurerm_resource_group" "main" {
  name     = "rg-obsidian-sync-${var.environment}"
  location = var.location
  tags     = local.common_tags
}

# ─────────────────────────────────────────────────────────────────────────────
# Storage account + blob container
# ─────────────────────────────────────────────────────────────────────────────

resource "azurerm_storage_account" "vault" {
  name                     = "stobsvault${local.storage_suffix}"
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = var.storage_replication
  account_kind             = "StorageV2"

  # Disable shared-key access — all access goes through Azure AD
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true

  blob_properties {
    versioning_enabled = true

    delete_retention_policy {
      days = 30
    }

    container_delete_retention_policy {
      days = 7
    }
  }

  # Optional IP-based firewall
  dynamic "network_rules" {
    for_each = length(var.allowed_ip_ranges) > 0 ? [1] : []
    content {
      default_action             = "Deny"
      ip_rules                   = var.allowed_ip_ranges
      bypass                     = ["AzureServices"]
    }
  }

  tags = local.common_tags
}

resource "azurerm_storage_container" "vault" {
  name                  = var.vault_container_name
  storage_account_name  = azurerm_storage_account.vault.name
  container_access_type = "private"
}

# ─────────────────────────────────────────────────────────────────────────────
# Azure AD app registration (OAuth2 client for the sync tool)
# ─────────────────────────────────────────────────────────────────────────────

resource "azuread_application" "sync" {
  display_name = "obsidian-vault-sync-${var.environment}"

  # Allow public client flows so the Python client can use device code / ROPC
  fallback_public_client_enabled = true

  # Required API permissions: Azure Storage user_impersonation
  required_resource_access {
    resource_app_id = "e406a681-f3d4-42a8-90b6-c2b029497af1" # Azure Storage

    resource_access {
      id   = "03e0da56-190b-40ad-a80c-ea378c433f7f" # user_impersonation
      type = "Scope"
    }
  }

  tags = ["obsidian-sync"]
}

resource "azuread_service_principal" "sync" {
  client_id                    = azuread_application.sync.client_id
  app_role_assignment_required = false
  tags                         = ["obsidian-sync"]
}

# Client secret — stored in Key Vault; used only for CI/CD non-interactive flows
resource "azuread_application_password" "sync" {
  application_id = azuread_application.sync.id
  display_name   = "obsidian-sync-secret"
  end_date       = timeadd(timestamp(), "87600h") # 10 years; rotate via Key Vault
}

# ─────────────────────────────────────────────────────────────────────────────
# RBAC — grant the app's service principal access to storage
# ─────────────────────────────────────────────────────────────────────────────

resource "azurerm_role_assignment" "sync_blob_contributor" {
  scope                = azurerm_storage_account.vault.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azuread_service_principal.sync.object_id
}

# ─────────────────────────────────────────────────────────────────────────────
# Key Vault — store the client secret
# ─────────────────────────────────────────────────────────────────────────────

resource "azurerm_key_vault" "main" {
  name                       = "kv-obsidian-${local.storage_suffix}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = var.key_vault_sku
  purge_protection_enabled   = false
  soft_delete_retention_days = 7

  tags = local.common_tags
}

# Grant the deployer (CI/CD service principal) full secret management
resource "azurerm_key_vault_access_policy" "deployer" {
  key_vault_id = azurerm_key_vault.main.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = data.azurerm_client_config.current.object_id

  secret_permissions = [
    "Get", "List", "Set", "Delete", "Recover", "Backup", "Restore", "Purge"
  ]
}

# Grant the sync app's service principal read access to its own secret
resource "azurerm_key_vault_access_policy" "sync_app" {
  key_vault_id = azurerm_key_vault.main.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = azuread_service_principal.sync.object_id

  secret_permissions = ["Get"]

  depends_on = [azurerm_key_vault_access_policy.deployer]
}

resource "azurerm_key_vault_secret" "client_secret" {
  name         = "obsidian-sync-client-secret"
  value        = azuread_application_password.sync.value
  key_vault_id = azurerm_key_vault.main.id

  depends_on = [azurerm_key_vault_access_policy.deployer]
}
