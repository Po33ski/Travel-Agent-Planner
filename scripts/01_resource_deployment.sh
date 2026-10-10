# First, create a resource group (if you haven't already)
az group create `
--name aiagent-travel9871v4-rg `
--location swedencentral

# Load .env into this PowerShell session (Bicep cannot read the file itself)
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
    Set-Item "env:$($Matches[1])" $Matches[2].Trim().Trim('"', "'")
  }
}

# Deploy the Bicep file (the third-party API keys from .env are stored in Key Vault)
az deployment group create `
--resource-group aiagent-travel9871v4-rg `
--template-file resource_deployment.bicep `
--parameters projectPrefix="aiagent-travel9871v4" `
  userPrincipalId="$(az ad signed-in-user show --query id -o tsv)" `
  visualCrossingApiKey="$env:VISUAL_CROSSING_API_KEY" `
  tavilyApiKey="$env:TAVILY_API_KEY"

# Get your deployment outputs
az deployment group show `
--resource-group aiagent-travel9871v4-rg `
--name resource_deployment `
--query properties.outputs


############################################################################################################################
# For use without the Azure Key Vault
# Get the keys for .env (never commit them)
# STORAGE_CONNECTION_STRING - Search indexer reads the blob container
az storage account show-connection-string `
--resource-group aiagent-travel9871v4-rg `
--name aiagenttravel9871v4st `
--query connectionString -o tsv

# AOAI_API_KEY - Search calls the embedding and chat models
az cognitiveservices account keys list `
--resource-group aiagent-travel9871v4-rg `
--name aiagent-travel9871v4 `
--query key1 -o tsv