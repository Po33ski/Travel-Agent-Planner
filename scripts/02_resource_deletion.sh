az deployment operation group list --resource-group aiagent-travel9871v4-rg --name resource_deployment --query "[?properties.provisioningState=='Failed'].{resource:properties.targetResource.resourceName, error:properties.statusMessage.error.code}" -o table


# Delete the entire deployment (So no more costs)
az group delete --name aiagent-travel9871v4-rg --yes;

# View recently deleted Cognitive Services accounts in the region (to confirm deletion)
#az cognitiveservices account list-deleted --output table

# The following commands permanently delete the soft-deleted accounts.
az cognitiveservices account purge `
    --location swedencentral `
    --resource-group aiagent-travel9871v4-rg `
    --name aiagent-travel9871v4

# The free (F0) Speech account also stays soft-deleted for 48 hours and, until purged,
# still counts towards the limit of one free Speech account per subscription.
az cognitiveservices account purge `
    --location swedencentral `
    --resource-group aiagent-travel9871v4-rg `
    --name aiagent-travel9871v4-speech