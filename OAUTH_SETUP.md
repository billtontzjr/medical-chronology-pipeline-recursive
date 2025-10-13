# Dropbox OAuth Setup Guide

This guide will help you set up permanent Dropbox access that **never expires** using OAuth 2.0 with refresh tokens.

## Why OAuth Instead of Access Tokens?

- **Access Tokens**: Expire every 4 hours, requires manual regeneration
- **OAuth Refresh Tokens**: Never expire, automatically refreshes access tokens

## Prerequisites

- A Dropbox account
- Admin access to create a Dropbox app

## Step 1: Create Dropbox App

1. Go to [Dropbox App Console](https://www.dropbox.com/developers/apps)
2. Click **"Create App"**
3. Configure the app:
   - Choose API: **Scoped access**
   - Choose access: **Full Dropbox**
   - Name your app: `medical-chronology-pipeline` (or any name)
4. Click **"Create App"**

## Step 2: Configure App Permissions

1. In your newly created app, go to the **Permissions** tab
2. Enable these **Individual Scopes** only:
   - ✅ `account_info.read`
   - ✅ `files.metadata.read`
   - ✅ `files.content.read`
   - ✅ `sharing.read`
3. **IMPORTANT**: Do NOT enable any Team scopes
4. Click **"Submit"** at the bottom to save

## Step 3: Get App Credentials

1. Go to the **Settings** tab
2. Find the **OAuth 2** section
3. Copy your **App key** and **App secret**
4. Under **Redirect URIs**, add: `http://localhost:8501`
5. Click **"Add"** to save the redirect URI

## Step 4: Update Your .env File

1. Open your `.env` file (or create one from `.env.example`)
2. Add your Dropbox credentials:

```bash
# Dropbox OAuth Configuration
DROPBOX_APP_KEY=your_app_key_here
DROPBOX_APP_SECRET=your_app_secret_here
DROPBOX_REFRESH_TOKEN=
```

Leave `DROPBOX_REFRESH_TOKEN` empty for now - we'll fill it in the next step.

## Step 5: Run OAuth Setup Script

1. Open terminal and navigate to your project directory:
```bash
cd /Users/billtontz/medical-chronology-pipeline
```

2. Activate your virtual environment:
```bash
source venv/bin/activate
```

3. Run the OAuth setup script:
```bash
python setup_dropbox_oauth.py
```

4. Follow the prompts:
   - Press ENTER to open your browser
   - Click **"Allow"** to authorize the app
   - You'll be redirected to a page that says "This site can't be reached"
   - Copy the **entire code** from the URL bar (everything after `code=`)
   - Paste it into the terminal
   - The script will automatically save the refresh token to your `.env` file

## Step 6: Verify Setup

1. Restart your Streamlit app:
```bash
streamlit run app.py
```

2. Check the sidebar - you should see:
   - ✅ Dropbox OAuth configured
   - ✅ Google Vision API key loaded
   - ✅ Anthropic API key loaded

## Step 7: Update on Render (For Deployed App)

If you have the app deployed on Render:

1. Go to [Render Dashboard](https://dashboard.render.com/)
2. Select your `medical-chronology-app` service
3. Go to **Environment** in the left sidebar
4. Add/Update these environment variables:
   - `DROPBOX_APP_KEY` = your app key
   - `DROPBOX_APP_SECRET` = your app secret
   - `DROPBOX_REFRESH_TOKEN` = your refresh token from .env
5. Click **"Save Changes"**
6. Render will automatically redeploy

## Troubleshooting

### "Missing Dropbox OAuth credentials"
- Make sure all three env vars are set: `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`
- Check for extra spaces or line breaks in your .env file

### "OAuth 2 access token is for entire Dropbox Business team"
- Remove ALL team scopes from your Dropbox app permissions
- Keep only the Individual Scopes listed in Step 2
- Generate a new refresh token after updating permissions

### "Failed to refresh token"
- Your refresh token may be invalid
- Re-run `python setup_dropbox_oauth.py` to get a new token
- Make sure your App secret is correct in .env

### Setup script fails at authorization step
- Make sure you added the redirect URI: `http://localhost:8501`
- Try using a different browser if the authorization page doesn't load
- Make sure you're logged into the correct Dropbox account

## Success!

Once configured, your app will:
- ✅ Never require manual token regeneration
- ✅ Automatically refresh access tokens every 4 hours
- ✅ Continue working indefinitely without intervention

You can now safely use the app without worrying about expired tokens!
