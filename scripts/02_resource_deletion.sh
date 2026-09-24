# Delete the entire deployment (So no more costs)
az group delete --name aiagent-travel987-rg --yes

# View recently deleted Cognitive Services accounts in the region (to confirm deletion)
az cognitiveservices account list-deleted --output table

# The following commands permanently delete the soft-deleted accounts.
az cognitiveservices account purge `
    --location eastus `
    --resource-group aiagent-travel987-rg `
    --name aiagent-travel987