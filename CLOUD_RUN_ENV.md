# Cloud Run environment variables

Set these in **Google Cloud Console → Cloud Run → your service → Edit & deploy new revision → Variables & secrets**.

Do **not** bake secrets into the Docker image. The container does not read a `.env` file in production.

## Required

| Name | Example / value |
|------|-----------------|
| `INTERAKT_API_KEY` | From [Interakt Developer settings](https://app.interakt.ai/settings/developer-setting) |
| `FIREBASE_PROJECT_ID` | `whatsapp-approval-system` |
| `JMD_I_WHATSAPP_NUMBER` | `whatsapp:+917339221730` (OD Unit I JMD) |
| `JMD_II_WHATSAPP_NUMBER` | `whatsapp:+919659756070` (OD Unit II JMD) |
| `MD_WHATSAPP_NUMBER` | `whatsapp:+917538866308` (OD final MD) |
| `VISITOR_JMD_I_WHATSAPP_NUMBER` | Your visitor Unit I JMD |
| `VISITOR_JMD_II_WHATSAPP_NUMBER` | Your visitor Unit II JMD |
| `VISITOR_MD_WHATSAPP_NUMBER` | Your visitor final MD |
| `VISITOR_OTP_TEMPLATE_NAME` | `visitor_pass_code` |
| `VISITOR_OTP_TEMPLATE_LANGUAGE_CODE` | `en` |
| `VISITOR_OTP_TEMPLATE_BODY_FIELDS` | `otp` |
| `VISITOR_OTP_TEMPLATE_AUTH_BUTTON` | `true` |

## Optional

| Name | Default | Purpose |
|------|---------|---------|
| `WHATSAPP_SESSION_HOURS` | `24` | Approver must message Alubee within this window for Approve/Deny buttons |
| `VISITOR_TEST_JMD_WHATSAPP_NUMBER` | — | Pilot: test JMD (both units if I/II not set) |
| `VISITOR_TEST_JMD_I_WHATSAPP_NUMBER` | — | Pilot: test JMD Unit I |
| `VISITOR_TEST_JMD_II_WHATSAPP_NUMBER` | — | Pilot: test JMD Unit II |
| `VISITOR_TEST_MD_WHATSAPP_NUMBER` | — | Pilot: test MD |
| `VISITOR_TEST_EMPLOYEE_WHATSAPP_NUMBERS` | — | Comma-separated; only these employees use TEST visitor approvers |

## Do not set on Cloud Run

These break Application Default Credentials for Firestore:

- `GOOGLE_APPLICATION_CREDENTIALS`
- `FIREBASE_CREDENTIALS_JSON`
- `FIREBASE_CREDENTIALS_PATH`

Grant the Cloud Run service account **Cloud Datastore User** on `whatsapp-approval-system` instead.

## Verify after deploy

```bash
curl "https://YOUR-SERVICE.run.app/health"
```

Check:

- `"api_key_set": true`
- `"visitor_approvers_configured": true`
- `"visitor_otp_template": "visitor_pass_code"`

## Update env on existing service (gcloud)

Replace placeholders and run from `Interakt/Production/`:

```powershell
gcloud run services update alubee-interakt-od-bot `
  --region asia-south1 `
  --project alubee-prod `
  --set-env-vars "FIREBASE_PROJECT_ID=whatsapp-approval-system,WHATSAPP_SESSION_HOURS=24,VISITOR_OTP_TEMPLATE_NAME=visitor_pass_code,VISITOR_OTP_TEMPLATE_LANGUAGE_CODE=en,VISITOR_OTP_TEMPLATE_BODY_FIELDS=otp,VISITOR_OTP_TEMPLATE_AUTH_BUTTON=true"
```

Set secrets and phone numbers separately (Console UI is easier for many vars). Use **Secret Manager** for `INTERAKT_API_KEY` when possible.
