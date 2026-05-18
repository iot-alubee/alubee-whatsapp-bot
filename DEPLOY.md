# Deploy WhatsApp bot (Production folder → Cloud Run)

This directory is the **only** build context for the Twilio webhook service.  
The Flask security portal stays in `../alubee_flask_app/` (separate Cloud Run service).

## Prerequisites

- [Google Cloud SDK](https://cloud.google.com/sdk) (`gcloud`) installed and logged in
- GCP project with **Cloud Run** and **Artifact Registry** (or Cloud Build) enabled
- Firestore: project `whatsapp-approval-system`
- Twilio WhatsApp sender + Content templates (SIDs in `main.py`)

## 1. Firestore IAM

On project **`whatsapp-approval-system`**, grant the **Cloud Run runtime service account** (e.g. `PROJECT_NUMBER-compute@developer.gserviceaccount.com`):

- **Cloud Datastore User** (Firestore access)

Use Application Default Credentials on Cloud Run. Do **not** ship `firebase-adminsdk.json` in the container.

## 2. Deploy

From this folder:

```bash
cd "path/to/alubee-whatsapp-bot-system/Production"

export PROJECT_ID=alubee-prod
export REGION=asia-south1
export SERVICE_NAME=alubee-whatsapp-api-latest

gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --platform managed \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --allow-unauthenticated
```

`--source .` must run inside **`Production/`** (where this `Dockerfile` and `main.py` are).

## 3. Environment variables

Cloud Run → service → **Edit revision** → **Variables and secrets**:

| Variable | Required | Description |
|----------|----------|-------------|
| `TWILIO_ACCOUNT_SID` | Yes | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Yes | Twilio Auth Token (prefer Secret Manager) |
| `TWILIO_WHATSAPP_NUMBER` | Yes | e.g. `whatsapp:+91XXXXXXXXXX` |
| `FIREBASE_PROJECT_ID` | Yes | `whatsapp-approval-system` (also set in Dockerfile) |
| `MD_WHATSAPP_NUMBER` | Recommended | MD WhatsApp id for final approval |

**Do not set** on Cloud Run (disabled keys cause JWT errors):

- `FIREBASE_CREDENTIALS_JSON`
- `GOOGLE_APPLICATION_CREDENTIALS`

Example with Secret Manager:

```bash
gcloud secrets create twilio-auth-token --project="$PROJECT_ID" --data-file=-
# paste token, Ctrl-D / Ctrl-Z

gcloud run services update "$SERVICE_NAME" \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --set-secrets=TWILIO_AUTH_TOKEN=twilio-auth-token:latest
```

## 4. Twilio webhook

After deploy, set the WhatsApp sandbox or production sender webhook to:

```
https://YOUR-SERVICE-XXXX.run.app/webhook
```

Method: **POST**. (`POST /` is also accepted.)

## 5. Health check

```bash
curl "https://YOUR-SERVICE-XXXX.run.app/health"
```

Expected: `{"status":"ok"}`

Send **Hi** on WhatsApp to confirm the menu.

This folder has **no** `.env` file. All secrets and config come from **Cloud Run environment variables** (or Secret Manager). `.dockerignore` blocks `.env` if one is added by mistake.

For local testing, use the repo root (`../`) with its `.env` — not this folder.

## Syncing code from repo root

After changing the bot in `../main.py`, copy the latest into this folder before redeploying:

```bash
cp ../main.py ./main.py
```
