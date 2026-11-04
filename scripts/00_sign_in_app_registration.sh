# Registers the app with which the concierge chat signs users in (Microsoft Entra ID; an app registration is free).
# Run it once, before scripts/01_resource_deployment.sh: the deployment looks the app up by its display name
# and stores its client ID in Key Vault (ENTRA-APP-CLIENT-ID). Running it again changes nothing.
#
# Who can sign in: work or school accounts of any tenant and personal Microsoft accounts (outlook.com,
# hotmail.com, live.com, or a Microsoft account created with any other e-mail address).
# The chat is a public client (no secret): MSAL opens the browser and receives the answer on http://localhost.
#
# Keep the display name in sync with scripts/01_resource_deployment.sh and scripts/02_resource_deletion.sh.
$appName = "aiagent-travel9871v6-sign-in"

$appId = az ad app list --display-name $appName --query "[0].appId" -o tsv
if (-not $appId) {
  # Personal Microsoft accounts need access tokens of version 2: the app gets them first, then the audience
  # that includes personal accounts (Entra ID refuses that audience on an app that still has version 1).
  $appId = az ad app create `
    --display-name $appName `
    --sign-in-audience AzureADMyOrg `
    --is-fallback-public-client true `
    --public-client-redirect-uris "http://localhost" `
    --requested-access-token-version 2 `
    --query appId -o tsv
  az ad app update --id $appId --sign-in-audience AzureADandPersonalMicrosoftAccount
}

# Check: signInAudience AzureADandPersonalMicrosoftAccount, redirect URI http://localhost
az ad app show --id $appId --query "{appId:appId, signInAudience:signInAudience, redirectUris:publicClient.redirectUris}"
