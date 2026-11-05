# First, create a resource group (if you haven't already)
# All resources are deployed to the resource group's region. Switzerland North is the closest region to Poland
# that supports every service in this project (Poland Central has no Speech service; Sweden Central and
# Germany West Central are marked "high demand" for new Azure AI Search services).
az group create `
--name aiagent-travel9871v6-rg `
--location switzerlandnorth

# Load .env into this PowerShell session (Bicep cannot read the file itself)
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
    Set-Item "env:$($Matches[1])" $Matches[2].Trim().Trim('"', "'")
  }
}

# Deploy the Bicep file (the third-party API keys from .env are stored in Key Vault).
# signInClientId: Application (client) ID of the app registration travel-planner-console in the External ID
# tenant travelplannercustomers.onmicrosoft.com (registered in the Entra admin center, not by a script: az login
# here works in the Azure tenant, the app lives in the external tenant). A client ID is not a secret.
az deployment group create `
--resource-group aiagent-travel9871v6-rg `
--template-file resource_deployment.bicep `
--parameters projectPrefix="aiagent-travel9871v6" `
  userPrincipalId="$(az ad signed-in-user show --query id -o tsv)" `
  signInClientId="30f4eb66-72c8-4a54-8e5f-56814ad22d09" `
  visualCrossingApiKey="$env:VISUAL_CROSSING_API_KEY" `
  googleApiKey="$env:GOOGLE_API_KEY"

# Get your deployment outputs
az deployment group show `
--resource-group aiagent-travel9871v6-rg `
--name resource_deployment `
--query properties.outputs


############################################################################################################################
# For use without the Azure Key Vault
# Get the keys for .env (never commit them)
# STORAGE_CONNECTION_STRING - Search indexer reads the blob container
az storage account show-connection-string `
--resource-group aiagent-travel9871v6-rg `
--name aiagenttravel9871v6st `
--query connectionString -o tsv

# AOAI_API_KEY - Search calls the embedding and chat models
az cognitiveservices account keys list `
--resource-group aiagent-travel9871v6-rg `
--name aiagent-travel9871v6 `
--query key1 -o tsv