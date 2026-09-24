# Create a new environment
python -m venv tutorials

# Identify the new environment's Python executable path
tutorials\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# (optional) Freeze dependencies if needed
pip freeze > requirements.txt

# Install Azure CLI and restart VSCode
https://aka.ms/installazurecli

# Login to Azure
az login

# Install Bicep (or Update it)
az bicep install
az bicep upgrade