# First, create a resource group (if you haven't already)
az group create `
--name aiagent-travel-rg `
--location eastus

# Deploy the Bicep file
az deployment group create `
--resource-group aiagent-travel-rg `
--template-file resource_deployment.bicep `
--parameters projectPrefix="aiagent-travel" `
  userPrincipalId="$(az ad signed-in-user show --query id -o tsv)"

# Get your deployment outputs
az deployment group show `
--resource-group aiagent-travel-rg `
--name resource_deployment `
--query properties.outputs