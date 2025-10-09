"""One-time setup script to get Dropbox OAuth refresh token."""

import os
import webbrowser
from dotenv import load_dotenv
from urllib.parse import urlencode
import http.server
import socketserver
from urllib.parse import urlparse, parse_qs

# Load environment
load_dotenv()

APP_KEY = os.getenv('DROPBOX_APP_KEY')
APP_SECRET = os.getenv('DROPBOX_APP_SECRET')
REDIRECT_URI = 'http://localhost:8501'

print("=" * 60)
print("  DROPBOX OAUTH SETUP - ONE TIME ONLY")
print("=" * 60)
print()

if not APP_KEY or not APP_SECRET:
    print("❌ Error: DROPBOX_APP_KEY or DROPBOX_APP_SECRET not found in .env")
    print("   Please make sure they are set correctly.")
    exit(1)

print(f"✅ App Key: {APP_KEY}")
print(f"✅ App Secret: {APP_SECRET[:4]}...{APP_SECRET[-4:]}")
print()

# Step 1: Generate authorization URL
auth_params = {
    'client_id': APP_KEY,
    'response_type': 'code',
    'redirect_uri': REDIRECT_URI,
    'token_access_type': 'offline'  # This gives us a refresh token
}

auth_url = f"https://www.dropbox.com/oauth2/authorize?{urlencode(auth_params)}"

print("STEP 1: Authorize the app")
print("-" * 60)
print("I'll open your browser to authorize the app.")
print("Click 'Allow' to give the app access to your Dropbox.")
print()
print("Authorization URL:")
print(auth_url)
print()

input("Press ENTER to open browser... ")

# Open browser
webbrowser.open(auth_url)

print()
print("STEP 2: Copy the code")
print("-" * 60)
print("After you click 'Allow', you'll be redirected to a page")
print("that says 'This site can't be reached' or similar.")
print()
print("Look at the URL bar - it will look like:")
print("http://localhost:8501?code=XXXXXXXXXXXXX")
print()
print("Copy EVERYTHING after 'code=' (the long string)")
print()

auth_code = input("Paste the authorization code here: ").strip()

if not auth_code:
    print("❌ No code provided. Exiting.")
    exit(1)

# Step 3: Exchange code for refresh token
print()
print("STEP 3: Getting refresh token...")
print("-" * 60)

import requests

token_url = 'https://api.dropbox.com/oauth2/token'
token_data = {
    'code': auth_code,
    'grant_type': 'authorization_code',
    'client_id': APP_KEY,
    'client_secret': APP_SECRET,
    'redirect_uri': REDIRECT_URI
}

response = requests.post(token_url, data=token_data)

if response.status_code != 200:
    print(f"❌ Error getting token: {response.text}")
    exit(1)

token_info = response.json()
refresh_token = token_info.get('refresh_token')

if not refresh_token:
    print("❌ No refresh token received. Response:")
    print(token_info)
    exit(1)

print("✅ Success! Got refresh token!")
print()

# Step 4: Save to .env
print("STEP 4: Saving to .env file...")
print("-" * 60)

# Read current .env
with open('.env', 'r') as f:
    env_content = f.read()

# Update refresh token line
if 'DROPBOX_REFRESH_TOKEN=' in env_content:
    # Replace existing empty line
    env_content = env_content.replace(
        'DROPBOX_REFRESH_TOKEN=',
        f'DROPBOX_REFRESH_TOKEN={refresh_token}'
    )
else:
    # Add new line
    env_content += f'\nDROPBOX_REFRESH_TOKEN={refresh_token}\n'

# Write back
with open('.env', 'w') as f:
    f.write(env_content)

print("✅ Refresh token saved to .env!")
print()
print("=" * 60)
print("  🎉 SETUP COMPLETE!")
print("=" * 60)
print()
print("Your Dropbox access is now PERMANENT!")
print("The app will automatically refresh tokens when needed.")
print()
print("You can now:")
print("  1. Restart the Streamlit app (it will auto-load the new token)")
print("  2. Use the pipeline without worrying about tokens expiring")
print()
print("Refresh token (saved in .env):")
print(f"{refresh_token[:20]}...{refresh_token[-20:]}")
print()
