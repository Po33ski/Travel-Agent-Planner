az deployment operation group list --resource-group aiagent-travel9871v6-rg --name resource_deployment --query "[?properties.provisioningState=='Failed'].{resource:properties.targetResource.resourceName, error:properties.statusMessage.error.code}" -o table


# Delete the entire deployment (So no more costs)
az group delete --name aiagent-travel9871v6-rg --yes;

# The users' sign-in (Microsoft Entra External ID) is not in this resource group: the external tenant
# travelplannercustomers.onmicrosoft.com with the app registration travel-planner-console and the user flow
# travel-planner-last-flow, linked to the subscription through its own resource group. Deleting this group keeps
# the tenant and its user accounts. It costs nothing (free up to 50,000 monthly active users); delete it only
# when the project is gone for good, in the Entra admin center (Entra ID > Overview > Manage tenants), after
# deleting its users and app registrations.

# View recently deleted Cognitive Services accounts in the region (to confirm deletion)
#az cognitiveservices account list-deleted --output table

# The following commands permanently delete the soft-deleted accounts.
# --location is the region the resources were deployed to (see scripts/01_resource_deployment.sh)
az cognitiveservices account purge `
    --location switzerlandnorth `
    --resource-group aiagent-travel9871v6-rg `
    --name aiagent-travel9871v6;

# The free (F0) Speech account also stays soft-deleted for 48 hours and, until purged,
# still counts towards the limit of one free Speech account per subscription.
az cognitiveservices account purge `
    --location switzerlandnorth `
    --resource-group aiagent-travel9871v6-rg `
    --name aiagent-travel9871v6-speech

# The same applies to the free (F0) Language account
az cognitiveservices account purge `
    --location switzerlandnorth `
    --resource-group aiagent-travel9871v6-rg `
    --name aiagent-travel9871v6-language;

# Find all soft-deleted Key Vaults in the region (to confirm deletion)
az keyvault list-deleted

# Find when the key vault will be permanently deleted (scheduled purge date)
az keyvault list-deleted --resource-type vault --query "[?name=='aiagent-travel9871v6-kv'].properties.scheduledPurgeDate"

# Permanently delete the soft-deleted Key Vault, so its name can be used again
# (not possible when purge protection is enabled)
az keyvault purge --name aiagent-travel9871v6-kv --location switzerlandnorth
