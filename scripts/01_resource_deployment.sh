# First, create a resource group (if you haven't already)
az group create `
--name aiagent-travel9871v3-rg `
--location swedencentral

# Deploy the Bicep file
az deployment group create `
--resource-group aiagent-travel9871v3-rg `
--template-file resource_deployment.bicep `
--parameters projectPrefix="aiagent-travel9871v3" `
  userPrincipalId="$(az ad signed-in-user show --query id -o tsv)"

# Get your deployment outputs
az deployment group show `
--resource-group aiagent-travel9871v3-rg `
--name resource_deployment `
--query properties.outputs

# Get the keys for .env (never commit them)
# STORAGE_CONNECTION_STRING - Search indexer reads the blob container
az storage account show-connection-string `
--resource-group aiagent-travel9871v3-rg `
--name aiagenttravel9871v3st `
--query connectionString -o tsv

# AOAI_API_KEY - Search calls the embedding and chat models
az cognitiveservices account keys list `
--resource-group aiagent-travel9871v3-rg `
--name aiagent-travel9871v3 `
--query key1 -o tsv

# SPEECH_KEY - the client synthesizes the agent's replies to audio
az cognitiveservices account keys list `
--resource-group aiagent-travel9871v3-rg `
--name aiagent-travel9871v3-speech `
--query key1 -o tsv