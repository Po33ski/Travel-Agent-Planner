# First, create a resource group (if you haven't already)
az group create `
--name aiagent-travel987-rg `
--location eastus

# Deploy the Bicep file
az deployment group create `
--resource-group aiagent-travel987-rg `
--template-file resource_deployment.bicep `
--parameters projectPrefix="aiagent-travel987" `
  userPrincipalId="$(az ad signed-in-user show --query id -o tsv)"

# Get your deployment outputs
az deployment group show `
--resource-group aiagent-travel987-rg `
--name resource_deployment `
--query properties.outputs