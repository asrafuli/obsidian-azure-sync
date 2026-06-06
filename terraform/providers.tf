terraform {
  required_version = ">= 1.5"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.100"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 2.50"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state in Azure Blob — bootstrapped separately (see README)
  backend "azurerm" {
    resource_group_name  = "rg-obsidian-sync-tfstate"
    storage_account_name = "stobsidiantfstate"   # override via -backend-config
    container_name       = "tfstate"
    key                  = "obsidian-sync.tfstate"
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }
  }
}

provider "azuread" {}
