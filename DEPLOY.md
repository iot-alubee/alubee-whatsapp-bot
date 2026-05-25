# Deploy Interakt OD bot (this folder → Cloud Run)

This directory is the **only** build context for the Interakt webhook service.  
Local development uses `../` (parent `Interakt/` folder with `.env`).

## Prerequisites

- [Google Cloud SDK](https://cloud.google.com/sdk) (`gcloud`) installed and logged in
- GCP project with **Cloud Run** enabled
- Firestore project: `whatsapp-approval-system`
- Interakt API key + WhatsApp number connected in [Interakt](https://app.interakt.ai)

## 1. Firestore IAM

On **`whatsapp-approval-system`**, grant the Cloud Run runtime service account:

- **Cloud Datastore User**

Use Application Default Credentials on Cloud Run. Do **not** ship `firebase-adminsdk.json` in the image.

**Do not set** on Cloud Run (can break ADC):

- `GOOGLE_APPLICATION_CREDENTIALS`
- `FIREBASE_CREDENTIALS_JSON`

## 2. Deploy

From **this folder** (`Interakt/Production`):

```powershell
cd "path\to\alubee-whatsapp-bot-system\Interakt\Production"

$env:PROJECT_ID = "alubee-prod"
$env:REGION = "asia-south1"
$env:SERVICE_NAME = "alubee-interakt-od-bot"

gcloud run deploy $env:SERVICE_NAME `
  --source . `
  --platform managed `
  --region $env:REGION `
  --project $env:PROJECT_ID `
  --allow-unauthenticated
```

Bash:

```bash
export PROJECT_ID=alubee-prod
export REGION=asia-south1
export SERVICE_NAME=alubee-interakt-od-bot

gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --platform managed \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --allow-unauthenticated
```

## 3. Cloud Run environment variables

Service → **Edit revision** → **Variables and secrets**:

| Variable | Required | Example |
|----------|----------|---------|
| `INTERAKT_API_KEY` | **Yes** | From [Developer settings](https://app.interakt.ai/settings/developer-setting) |
| `FIREBASE_PROJECT_ID` | Yes | `whatsapp-approval-system` |
| `JMD_I_WHATSAPP_NUMBER` | Yes | JMD1 employees — `whatsapp:+917339221730` |
| `JMD_II_WHATSAPP_NUMBER` | Yes | JMD2 employees — `whatsapp:+919659756070` |
| `MD_WHATSAPP_NUMBER` | Yes | `whatsapp:+917538866308` |
| `WHATSAPP_SESSION_HOURS` | No | `24` (default) |

Store `INTERAKT_API_KEY` in **Secret Manager** when possible.

## 4. Interakt webhook

After deploy, copy the service URL from Cloud Run, then in Interakt:

- Webhook URL: `https://YOUR-SERVICE-XXXX.run.app/webhook`
- Event: **message_received**
- Method: **POST**

Turn off Interakt **Greeting / welcome** automations so they do not clash with the bot.

## 5. Health check

```bash
curl "https://YOUR-SERVICE-XXXX.run.app/health"
```

Expected JSON includes `"status":"ok"` and `"api_key_set":true`.

## 6. Sync code before redeploy

After editing `../main.py` or `../interakt_api.py` locally, copy into this folder and keep the Cloud Run bootstrap in `main.py` (ADC on Cloud Run, optional `.env` when testing this folder locally):

```powershell
cd "path\to\alubee-whatsapp-bot-system\Interakt\Production"
Copy-Item ..\main.py .\main.py -Force
Copy-Item ..\interakt_api.py .\interakt_api.py -Force
# Restore Production-only blocks in main.py: _running_on_cloud_run, _init_firebase, health runtime
```

Or deploy from this folder after it has been synced (current `main.py` / `interakt_api.py` match parent + Cloud Run).

**Approval flow:** Employee → **JMD I** or **JMD II** (per user `jmd_route` from `load_users.py`) → **MD**. No manager step.

**Firestore users:** Run `python load_users.py` from repo root after changing `EMPLOYEES` so `jmd_route` is set on each user.

This image has **no** `.env` file. All config comes from Cloud Run env vars.
