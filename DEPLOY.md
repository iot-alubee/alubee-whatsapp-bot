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
| `MANAGER_WHATSAPP_NUMBER` | Yes | `whatsapp:+919994246682` |
| `JMD_WHATSAPP_NUMBER` | Yes | `whatsapp:+917339221730` |
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

After editing `../main.py` or `../interakt_api.py` locally:

```powershell
Copy-Item ..\main.py .\main.py
Copy-Item ..\interakt_api.py .\interakt_api.py
```

Then re-apply Cloud Run–specific changes in this folder’s `main.py` if you maintain them only here, or keep Cloud Run logic in the parent and copy wholesale.

This image has **no** `.env` file. All config comes from Cloud Run env vars.
