# Cloud Run environment variables

Set these in **Google Cloud Console → Cloud Run → your service → Edit & deploy new revision → Variables & secrets**.

Do **not** bake secrets into the Docker image. The container does not read a `.env` file in production.

## Visitor vs OD (important)

| Request type | Who approves |
|--------------|--------------|
| **OD** (menu option 1) | `JMD_I_*`, `JMD_II_*`, `MD_WHATSAPP_NUMBER` |
| **Visitor** (menu option 5) | `VISITOR_JMD_*`, `VISITOR_MD_*` only |

**Every employee** who submits a visitor request goes to the **visitor** JMD and MD you set below — not the OD approvers. You do **not** need `VISITOR_TEST_*` for normal use (leave those unset).

Minimum for visitor (all users, one JMD + one MD):

- `VISITOR_JMD_I_WHATSAPP_NUMBER` — your new visitor JMD  
  (alias: `VISITOR_JMD_WHATSAPP_NUMBER`)
- `VISITOR_MD_WHATSAPP_NUMBER` — your new visitor MD  

Optional: `VISITOR_JMD_II_WHATSAPP_NUMBER` only if `VISITOR_ROUTE_BY_UNIT=true` and Unit II should use a different visitor JMD.

## Required

| Name | Example / value |
|------|-----------------|
| `INTERAKT_API_KEY` | From [Interakt Developer settings](https://app.interakt.ai/settings/developer-setting) |
| `FIREBASE_PROJECT_ID` | `whatsapp-approval-system` |
| `JMD_I_WHATSAPP_NUMBER` | OD Unit I JMD |
| `JMD_II_WHATSAPP_NUMBER` | OD Unit II JMD |
| `MD_WHATSAPP_NUMBER` | OD final MD |
| `VISITOR_JMD_I_WHATSAPP_NUMBER` | **All** visitor requests → this JMD (unless route-by-unit) |
| `VISITOR_MD_WHATSAPP_NUMBER` | **All** visitor requests → this MD |
| `VISITOR_OTP_TEMPLATE_NAME` | `visitor_pass_code` |
| `VISITOR_OTP_TEMPLATE_LANGUAGE_CODE` | `en` |
| `VISITOR_OTP_TEMPLATE_BODY_FIELDS` | `otp` |
| `VISITOR_OTP_TEMPLATE_AUTH_BUTTON` | `true` |
| `VISITOR_FLOW_TEMPLATE_NAME` | Approved template with WhatsApp Flow button (e.g. `visitor_request_form`) |
| `VISITOR_FLOW_TEMPLATE_LANGUAGE_CODE` | `en` |
| `VISITOR_FLOW_TEMPLATE_BODY_FIELDS` | `name` (if template body has one variable) |

## Optional

| Name | Default | Purpose |
|------|---------|---------|
| `WHATSAPP_SESSION_HOURS` | `24` | Approver must message Alubee within this window for Approve/Deny buttons |
| `VISITOR_JMD_II_WHATSAPP_NUMBER` | same as `VISITOR_JMD_I` | Only used when `VISITOR_ROUTE_BY_UNIT=true` and employee is Unit II |
| `VISITOR_ROUTE_BY_UNIT` | `false` | `true` = Unit II employees use `VISITOR_JMD_II`; else **everyone** uses `VISITOR_JMD_I` |
| `VISITOR_TEST_*` | — | **Pilot only** — leave unset in production |

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
- `"visitor_flow_enabled": true`
- `"visitor_flow_template": "visitor_request_form"` (your template name)

## Interakt webhooks (visitor form)

Enable in Developer settings:

- `message_received`
- **Completed Flow** / `message_api_flow_response` (form submit)

## Update env on existing service (gcloud)

Replace placeholders and run from `Interakt/Production/`:

```powershell
gcloud run services update alubee-interakt-od-bot `
  --region asia-south1 `
  --project alubee-prod `
  --set-env-vars "FIREBASE_PROJECT_ID=whatsapp-approval-system,WHATSAPP_SESSION_HOURS=24,VISITOR_OTP_TEMPLATE_NAME=visitor_pass_code,VISITOR_OTP_TEMPLATE_LANGUAGE_CODE=en,VISITOR_OTP_TEMPLATE_BODY_FIELDS=otp,VISITOR_OTP_TEMPLATE_AUTH_BUTTON=true"
```

Set `VISITOR_JMD_I_WHATSAPP_NUMBER`, `VISITOR_MD_WHATSAPP_NUMBER`, and secrets in the Console UI (easier for phone numbers). Use **Secret Manager** for `INTERAKT_API_KEY` when possible.
