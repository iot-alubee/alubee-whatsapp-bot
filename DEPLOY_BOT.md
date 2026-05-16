# Deploy WhatsApp bot to Google Cloud Run

The **Flask security portal** is deployed separately from `alubee_flask_app/`.  
This service is the **Twilio webhook** (`main.py` at repo root).

## Prerequisites

- `gcloud` CLI logged in
- GCP project with **Cloud Run** API enabled
- Firestore project: `whatsapp-approval-system`
- Twilio WhatsApp number configured

## 1. IAM (Firestore)

Grant the **Cloud Run runtime service account** on project `whatsapp-approval-system`:

- **Cloud Datastore User** (or Firebase-compatible Firestore access)

Do **not** mount `firebase-adminsdk.json` on Cloud Run unless you use Secret Manager; prefer IAM + ADC.

## 2. Deploy from repo root

```bash
cd "path/to/alubee-whatsapp-bot-system"

export PROJECT_ID=your-gcp-project-id
export REGION=asia-south1

gcloud run deploy alubee-whatsapp-bot \
  --source . \
  --platform managed \
  --region $REGION \
  --project $PROJECT_ID \
  --allow-unauthenticated
```

Build context must be the **repo root** (where this folder’s `Dockerfile` and `main.py` live).

## 3. Required environment variables

Set in Cloud Run → **Edit revision** → **Variables**:

| Variable | Description |
|----------|-------------|
| `TWILIO_ACCOUNT_SID` | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_WHATSAPP_NUMBER` | e.g. `whatsapp:+14155238886` |
| `FIREBASE_PROJECT_ID` | `whatsapp-approval-system` (default in Dockerfile) |
| `MD_WHATSAPP_NUMBER` | Optional; MD WhatsApp id for approvals |

**Do not set** on Cloud Run (causes Invalid JWT if key is disabled):

- `FIREBASE_CREDENTIALS_JSON`
- `GOOGLE_APPLICATION_CREDENTIALS`

Use **Secret Manager** for Twilio values in production:

```bash
gcloud secrets create twilio-auth-token --data-file=-
# paste token, Ctrl-D

gcloud run services update alubee-whatsapp-bot \
  --region=$REGION \
  --set-secrets=TWILIO_AUTH_TOKEN=twilio-auth-token:latest
```

## 4. Twilio webhook URL

After deploy, copy the service URL and set in Twilio Console:

```
https://YOUR-SERVICE-XXXX.run.app/webhook
```

Method: **POST**.

## 5. Verify

```bash
curl https://YOUR-SERVICE-XXXX.run.app/health
# {"status":"ok"}
```

Send **Hi** on WhatsApp and confirm the menu appears.

## Local development

```bash
pip install -r requirements.txt
export TWILIO_ACCOUNT_SID=...
export TWILIO_AUTH_TOKEN=...
export TWILIO_WHATSAPP_NUMBER=whatsapp:+...
# optional: GOOGLE_APPLICATION_CREDENTIALS=firebase-adminsdk.json
uvicorn main:app --reload --port 8000
```

Use ngrok or similar to expose `/webhook` for Twilio during local testing.
