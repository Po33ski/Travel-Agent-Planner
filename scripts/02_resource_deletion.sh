az deployment operation group list --resource-group aiagent-travel9871-rg --name resource_deployment --query "[?properties.provisioningState=='Failed'].{resource:properties.targetResource.resourceName, error:properties.statusMessage.error.code}" -o table


# Delete the entire deployment (So no more costs)
az group delete --name aiagent-travel9871-rg --yes;

# View recently deleted Cognitive Services accounts in the region (to confirm deletion)
#az cognitiveservices account list-deleted --output table

# The following commands permanently delete the soft-deleted accounts.
az cognitiveservices account purge `
    --location swedencentral `
    --resource-group aiagent-travel9871-rg `
    --name aiagent-travel9871