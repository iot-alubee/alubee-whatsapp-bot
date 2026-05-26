# Deploy Alubee Interakt bot (Production folder → Cloud Run)

Deploy **only** from `Interakt/Production/`. Local dev uses parent `Interakt/` with `.env`.

## Layout

| File | Role |
|------|------|
| `main.py` | Webhook, menu, Cloud Run Firebase init |
| `od_request.py` | OD flow |
| `visitor_request.py` | Visitor flow + guest OTP |
| `approval.py` | JMD → MD (OD + visitor approvers) |
| `interakt_api.py` | Interakt API (text, buttons, templates) |
| `bot_shared.py` | Shared Firestore helpers |

## Prerequisites

- Google Cloud SDK (`gcloud`), logged in
- Firestore project `whatsapp-approval-system`
- Interakt API key + webhook on your WhatsApp number
- Template **`visitor_pass_code`** approved (guest OTP)

## 1. Sync code before deploy

```powershell
cd "path\to\alubee-whatsapp-bot-system\Interakt\Production"
.\sync-from-parent.ps1
```

## 2. Firestore IAM

Grant the Cloud Run service account **Cloud Datastore User** on `whatsapp-approval-system`.

Do **not** set on Cloud Run: `GOOGLE_APPLICATION_CREDENTIALS`, `FIREBASE_CREDENTIALS_JSON`.

## 3. Deploy

```powershell
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

## 4. Cloud Run environment variables

**Set all values in Cloud Run** (Console → Variables and secrets). The image does not load `.env`.

Full checklist: **`CLOUD_RUN_ENV.md`** · quick list: **`.env.example`**

Minimum:

| Variable | Purpose |
|----------|---------|
| `INTERAKT_API_KEY` | **Required** |
| `FIREBASE_PROJECT_ID` | `whatsapp-approval-system` |
| `JMD_I_WHATSAPP_NUMBER` | OD — Unit I JMD |
| `JMD_II_WHATSAPP_NUMBER` | OD — Unit II JMD |
| `MD_WHATSAPP_NUMBER` | OD — final approver |
| `VISITOR_JMD_I_WHATSAPP_NUMBER` | Visitor — Unit I JMD |
| `VISITOR_JMD_II_WHATSAPP_NUMBER` | Visitor — Unit II JMD |
| `VISITOR_MD_WHATSAPP_NUMBER` | Visitor — final approver |
| `VISITOR_OTP_TEMPLATE_NAME` | `visitor_pass_code` |
| `VISITOR_OTP_TEMPLATE_LANGUAGE_CODE` | `en` |
| `VISITOR_OTP_TEMPLATE_BODY_FIELDS` | `otp` |
| `VISITOR_OTP_TEMPLATE_AUTH_BUTTON` | `true` |

Optional pilot testing (test JMD/MD for listed employees only):

- `VISITOR_TEST_JMD_WHATSAPP_NUMBER`, `VISITOR_TEST_MD_WHATSAPP_NUMBER`
- `VISITOR_TEST_EMPLOYEE_WHATSAPP_NUMBERS`

## 5. Interakt webhook

- URL: `https://YOUR-SERVICE.run.app/webhook`
- Event: `message_received`
- Disable conflicting Interakt greeting automations

## 6. Health check

```bash
curl "https://YOUR-SERVICE.run.app/health"
```

Expect: `"status":"ok"`, `"api_key_set":true`, `"runtime":"cloud_run"`, `"visitor_approvers_configured":true`, `"visitor_otp_template":"visitor_pass_code"`.

## 7. Flows

- **OD:** Employee → OD JMD (by unit) → OD MD  
- **Visitor:** List pick 1–10 visitors → names → guest **WhatsApp** number → organization → Visitor JMD → Visitor MD → OTP to employee + guest (`visitor_pass_code` template)  
- Approvers need **Hi** to Alubee within 24h for Approve/Deny buttons. Guests do not.

Run `python load_users.py` from repo root after changing employees.
