# Obsidian Azure Sync

Bidirectional sync for your Obsidian vault across multiple machines, backed by **Azure Blob Storage** with **Azure AD (Entra ID)** authentication. Infrastructure is managed with **Terraform**, deployed via **GitHub Actions**.

---

## Architecture

```
Machine A / Machine B
  └── sync.py  ──(Azure AD OAuth2)──►  Azure Blob Storage
                                           obsidian-vault container
                                           (versioning + soft-delete)

GitHub Actions  ──(OIDC)──►  Terraform  ──►  Azure resources
```

**Resources provisioned by Terraform:**

| Resource | Purpose |
|---|---|
| Resource Group | Logical container |
| Storage Account (GRS) | Vault blob storage |
| Blob Container | One container per vault |
| Azure AD App Registration | OAuth2 client identity |
| Key Vault | Stores the client secret |
| RBAC assignment | `Storage Blob Data Contributor` on storage |

---

## Prerequisites

- Azure subscription
- Azure CLI (`az`) installed and logged in (`az login`)
- Python ≥ 3.10

---

## Manual CLI deployment

Use this if you want to skip Terraform and provision everything directly with the Azure CLI.

### Step 1 — Set variables

```bash
LOCATION="eastus"
ENVIRONMENT="prod"
SUFFIX=$(openssl rand -hex 4)
RG="rg-obsidian-sync-${ENVIRONMENT}"
STORAGE_ACCOUNT="stobsvault${SUFFIX}"
CONTAINER_NAME="obsidian-vault"
APP_NAME="obsidian-vault-sync-${ENVIRONMENT}"
KV_NAME="kv-obsidian-${SUFFIX}"
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
```

### Step 2 — Resource group

```bash
az group create -n $RG -l $LOCATION
```

### Step 3 — Storage account + blob container

```bash
az storage account create \
  -n $STORAGE_ACCOUNT \
  -g $RG \
  -l $LOCATION \
  --sku Standard_GRS \
  --kind StorageV2 \
  --allow-shared-key-access false

az storage account blob-service-properties update \
  --account-name $STORAGE_ACCOUNT \
  --resource-group $RG \
  --enable-versioning true \
  --delete-retention-days 30 \
  --container-delete-retention-days 7

az storage container create \
  -n $CONTAINER_NAME \
  --account-name $STORAGE_ACCOUNT \
  --auth-mode login
```

### Step 4 — Azure AD app registration + service principal

```bash
APP_ID=$(az ad app create \
  --display-name $APP_NAME \
  --sign-in-audience AzureADandPersonalMicrosoftAccount \
  --is-fallback-public-client true \
  --query appId -o tsv)

# Grant Azure Storage user_impersonation permission
az ad app permission add \
  --id $APP_ID \
  --api e406a681-f3d4-42a8-90b6-c2b029497af1 \
  --api-permissions 03e0da56-190b-40ad-a80c-ea378c433f7f=Scope

SP_OBJECT_ID=$(az ad sp create --id $APP_ID --query id -o tsv)
```

### Step 5 — Grant the app access to storage

```bash
az role assignment create \
  --assignee $SP_OBJECT_ID \
  --role "Storage Blob Data Contributor" \
  --scope /subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT
```

### Step 6 — Key Vault + client secret

```bash
DEPLOYER_OBJECT_ID=$(az ad signed-in-user show --query id -o tsv)

az keyvault create \
  -n $KV_NAME \
  -g $RG \
  -l $LOCATION \
  --sku standard \
  --retention-days 7

# Allow your own account to manage secrets
az keyvault set-policy \
  -n $KV_NAME \
  --object-id $DEPLOYER_OBJECT_ID \
  --secret-permissions get list set delete recover backup restore purge

# Create client secret and store it
CLIENT_SECRET=$(az ad app credential reset \
  --id $APP_ID \
  --display-name "obsidian-sync-secret" \
  --years 10 \
  --query password -o tsv)

# Allow the sync app to read its own secret
az keyvault set-policy \
  -n $KV_NAME \
  --object-id $SP_OBJECT_ID \
  --secret-permissions get

az keyvault secret set \
  --vault-name $KV_NAME \
  --name "obsidian-sync-client-secret" \
  --value $CLIENT_SECRET
```

### Step 7 — Print your config values

```bash
echo "tenant_id: \"common\""
echo "client_id: \"${APP_ID}\""
echo "storage_account_name: \"${STORAGE_ACCOUNT}\""
echo "container_name: \"${CONTAINER_NAME}\""
```

Copy these into `~/.obsidian-sync/config.yaml` (see [Configure the sync client](#5--configure-the-sync-client-on-each-machine) below).

---

## Terraform (CI/CD) deployment

> The following steps set up Terraform + GitHub Actions to manage infrastructure automatically.
> Skip this section if you used the manual CLI deployment above.

---

## 1 — Bootstrap Terraform remote state

Terraform needs a storage account to store its own state **before** it can create the main infrastructure. Run this once from your local machine:

```bash
LOCATION="eastus"
RG="rg-obsidian-sync-tfstate"
SA="stobsidiantfstate"   # must be globally unique — change if taken

az group create -n $RG -l $LOCATION
az storage account create -n $SA -g $RG -l $LOCATION --sku Standard_LRS
az storage container create -n tfstate --account-name $SA
```

---

## 2 — Create a service principal for GitHub Actions (OIDC)

```bash
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
REPO="your-github-username/your-repo-name"

# Create service principal with Contributor + User Access Administrator
az ad sp create-for-rbac \
  --name "sp-obsidian-sync-cicd" \
  --role Contributor \
  --scopes /subscriptions/$SUBSCRIPTION_ID \
  --json-auth

# Also grant User Access Administrator so Terraform can create RBAC assignments
az role assignment create \
  --assignee "sp-obsidian-sync-cicd" \
  --role "User Access Administrator" \
  --scope /subscriptions/$SUBSCRIPTION_ID

# Enable OIDC federated credential (preferred — no secret needed in GitHub)
APP_ID=$(az ad app list --display-name sp-obsidian-sync-cicd --query "[0].appId" -o tsv)

az ad app federated-credential create --id $APP_ID --parameters '{
  "name": "github-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:'"$REPO"':ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

---

## 3 — Set GitHub Actions secrets and variables

In your repo → **Settings → Secrets and variables → Actions**:

**Secrets** (sensitive):

| Name | Value |
|---|---|
| `AZURE_CLIENT_ID` | App ID of `sp-obsidian-sync-cicd` |
| `AZURE_TENANT_ID` | Your Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Your subscription ID |
| `TF_STATE_STORAGE_ACCOUNT` | `stobsidiantfstate` (from step 1) |
| `TF_STATE_RESOURCE_GROUP` | `rg-obsidian-sync-tfstate` (from step 1) |

**Variables** (non-sensitive, optional overrides):

| Name | Default | Description |
|---|---|---|
| `AZURE_LOCATION` | `eastus` | Azure region |
| `ENVIRONMENT` | `prod` | Environment tag |

---

## 4 — Deploy infrastructure

```bash
git add .
git commit -m "initial infrastructure"
git push origin main
```

The GitHub Actions workflow will run `terraform init → plan → apply` automatically on push to `main`.

After apply, get your config snippet:

```bash
cd terraform
terraform output sync_config_snippet
```

---

## 5 — Configure the sync client on each machine

### Install dependencies

```bash
cd sync_client
pip install -r requirements.txt
```

### Create config file

```bash
mkdir -p ~/.obsidian-sync
cp sync_client/config.example.yaml ~/.obsidian-sync/config.yaml
```

Edit `~/.obsidian-sync/config.yaml` and fill in the values from `terraform output sync_config_snippet`.
Set `vault_path` to the absolute path of your Obsidian vault.

### First-time authentication

On the first run, the device-code flow will prompt you to visit a URL and enter a code:

```bash
python sync_client/sync.py --dry-run
```

```
To sign in, use a web browser to open https://microsoft.com/devicelogin
and enter the code XXXXXXXX to authenticate.
```

After you authenticate, the token is cached in `~/.obsidian-sync/` and future runs are silent.

---

## 6 — Running the sync

```bash
# Bidirectional (default)
python sync_client/sync.py

# Push only (local → cloud)
python sync_client/sync.py --push

# Pull only (cloud → local)
python sync_client/sync.py --pull

# Preview changes without writing
python sync_client/sync.py --dry-run

# Also delete files removed on the other side (use with care)
python sync_client/sync.py --delete
```

### Automate with cron (macOS / Linux)

```bash
# Edit crontab
crontab -e

# Sync every 15 minutes
*/15 * * * * cd /path/to/obsidian-azure-sync && python sync_client/sync.py >> ~/.obsidian-sync/sync.log 2>&1
```

### Automate with Task Scheduler (Windows)

```powershell
$action  = New-ScheduledTaskAction -Execute "python" `
             -Argument "C:\path\to\sync_client\sync.py" `
             -WorkingDirectory "C:\path\to\obsidian-azure-sync"
$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 15) -Once -At (Get-Date)
Register-ScheduledTask -TaskName "ObsidianSync" -Action $action -Trigger $trigger
```

---

## Conflict resolution

The sync client uses **last-writer-wins** based on file modification timestamps:

- If the local file is newer → upload to cloud
- If the cloud blob is newer → download to local
- If within 2 seconds → skip (avoids thrashing on FAT/exFAT filesystems)

A warning is logged for every conflict so you can review it.

---

## Files ignored by default

The following are never synced (per-machine state):

- `.obsidian/workspace.json`
- `.obsidian/workspace-mobile.json`
- `.DS_Store`, `Thumbs.db`
- `*.tmp`, `~$*` (temp files)

Add more patterns to `IGNORE_PATTERNS` in `sync.py`.

---

## Security notes

- Shared-key access is **disabled** on the storage account — all access goes through Azure AD tokens.
- The client secret is stored in **Azure Key Vault**, not in the config file.
- The config file on each machine contains only `tenant_id`, `client_id`, and storage coordinates — no secrets.
- Blob versioning and soft-delete are enabled (30-day retention) so accidental overwrites are recoverable.

---

## Terraform state management

```bash
# Plan without applying
cd terraform
terraform plan

# Force apply (also available as manual workflow_dispatch in GitHub Actions)
terraform apply

# Destroy all resources
terraform destroy
```

The GitHub Actions workflow also supports manual `plan`, `apply`, and `destroy` via **workflow_dispatch**.
