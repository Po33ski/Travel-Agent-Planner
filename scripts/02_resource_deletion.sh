az deployment operation group list --resource-group aiagent-travel9871v5-rg --name resource_deployment --query "[?properties.provisioningState=='Failed'].{resource:properties.targetResource.resourceName, error:properties.statusMessage.error.code}" -o table


# Delete the entire deployment (So no more costs)
az group delete --name aiagent-travel9871v5-rg --yes;

# View recently deleted Cognitive Services accounts in the region (to confirm deletion)
#az cognitiveservices account list-deleted --output table

# The following commands permanently delete the soft-deleted accounts.
# --location is the region the resources were deployed to (see scripts/01_resource_deployment.sh)
az cognitiveservices account purge `
    --location switzerlandnorth `
    --resource-group aiagent-travel9871v5-rg `
    --name aiagent-travel9871v5

# The free (F0) Speech account also stays soft-deleted for 48 hours and, until purged,
# still counts towards the limit of one free Speech account per subscription.
az cognitiveservices account purge `
    --location switzerlandnorth `
    --resource-group aiagent-travel9871v5-rg `
    --name aiagent-travel9871v5-speech

# The same applies to the free (F0) Language account
az cognitiveservices account purge `
    --location switzerlandnorth `
    --resource-group aiagent-travel9871v5-rg `
    --name aiagent-travel9871v5-language

# Find all soft-deleted Key Vaults in the region (to confirm deletion)
az keyvault list-deleted

# Find when the key vault will be permanently deleted (scheduled purge date)
az keyvault list-deleted --resource-type vault --query "[?name=='aiagent-travel9871v5-kv'].properties.scheduledPurgeDate"

# Permanently delete the soft-deleted Key Vault, so its name can be used again
# (not possible when purge protection is enabled)
az keyvault purge --name aiagent-travel9871v5-kv --location switzerlandnorth
