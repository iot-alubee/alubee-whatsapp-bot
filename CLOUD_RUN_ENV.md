# Cloud Run environment variables

Set these in **Google Cloud Console → Cloud Run → your service → Edit & deploy new revision → Variables & secrets**.

Do **not** bake secrets into the Docker image. The container does not read a `.env` file in production.

## Approvers by request type

| Request type | Who approves |
|--------------|--------------|
| **OD** (menu option 1) | `JMD_I_WHATSAPP_NUMBER`, `JMD_II_WHATSAPP_NUMBER`, `MD_WHATSAPP_NUMBER` |
| **Visitor** (menu option 5) | **Same** JMD I / JMD II / MD as OD |
| **Leave** (menu option 3) | JMD → MD (same as OD) |
| **Permission — employee** (menu option 4, For Myself) | JMD → MD (same as OD) |
| **Permission — CL** (supervisor, For CL) | `PPC_WHATSAPP_NUMBER` → `HR_WHATSAPP_NUMBER` |

You do **not** need separate `VISITOR_JMD_*` or `VISITOR_MD_*` variables in production. Remove them from Cloud Run if still set (they are ignored).

Minimum for both flows:

- `JMD_I_WHATSAPP_NUMBER` (alias: `JMD_WHATSAPP_NUMBER`)
- `JMD_II_WHATSAPP_NUMBER` — required when **Visiting to = Both** (must differ from JMD I)
- `MD_WHATSAPP_NUMBER`

Optional: `VISITOR_ROUTE_BY_UNIT=true` — Unit II employees (`jmd_route` JMD2) use `JMD_II` for visitor routing; default is everyone uses `JMD_I`.

## Required

| Name | Example / value |
|------|-----------------|
| `INTERAKT_API_KEY` | From [Interakt Developer settings](https://app.interakt.ai/settings/developer-setting) |
| `FIREBASE_PROJECT_ID` | `whatsapp-approval-system` |
| `JMD_I_WHATSAPP_NUMBER` | Unit I JMD |
| `JMD_II_WHATSAPP_NUMBER` | Unit II JMD |
| `MD_WHATSAPP_NUMBER` | Final MD |
| `PPC_WHATSAPP_NUMBER` | CL permission — first approver (PPC) |
| `HR_WHATSAPP_NUMBER` | CL permission — final approver (HR) |
| `VISITOR_OTP_TEMPLATE_NAME` | `visitor_pass_code` |
| `VISITOR_OTP_TEMPLATE_LANGUAGE_CODE` | `en` |
| `VISITOR_OTP_TEMPLATE_BODY_FIELDS` | `otp` |
| `VISITOR_OTP_TEMPLATE_AUTH_BUTTON` | `true` |

## Optional — OD WhatsApp Form (menu option 6, test)

Chat **OD Request** (option 1) does not use these. Defaults work without setting env:

| Name | Default | Purpose |
|------|---------|---------|
| `OD_FLOW_TEMPLATE_NAME` | `od_request` | **Interakt template name** (not Meta Flow ID) |
| `OD_FLOW_TEMPLATE_LANGUAGE_CODE` | `en` | Template language |
| `OD_FLOW_TEMPLATE_BODY_FIELDS` | *(empty)* | Only if template body has `{{1}}` etc. |

## Optional — Visitor / Leave / Permission WhatsApp Forms (menu 7–9, test)

Chat options 3–5 unchanged. Set template name when form is approved in Interakt:

| Name | Purpose |
|------|---------|
| `VISITOR_FLOW_TEMPLATE_NAME` | Visitor - Form |
| `LEAVE_FLOW_TEMPLATE_NAME` | Leave - Form |
| `PERMISSION_FLOW_TEMPLATE_NAME` | Permission - Form |
| `*_FLOW_TEMPLATE_LANGUAGE_CODE` | `en` |
| `*_FLOW_TEMPLATE_BODY_FIELDS` | `name` if template body has one variable |

All forms use the **same** flow endpoint URL (`alubee-whatsapp-flow-endpoint` → `/flow`).

## Optional

| Name | Default | Purpose |
|------|---------|---------|
| `WHATSAPP_SESSION_HOURS` | `24` | Approver must message Alubee within this window for Approve/Deny buttons |
| `TEST_MD_WHATSAPP_NUMBER` | — | Legacy only — old leave/permission test rows in Firestore |
| `PPC_WHATSAPP_NUMBER` | — | **Required for CL permission** (with HR) |
| `HR_WHATSAPP_NUMBER` | — | **Required for CL permission** (with PPC) |
| `VISITOR_ROUTE_BY_UNIT` | `false` | `true` = Unit II employees use `JMD_II` for visitor routing |
| `VISITOR_TEST_*` | — | **Pilot only** — alternate JMD/MD for listed test employees |

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
- `"visitor_uses_od_approvers": true`
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

Set `JMD_I_WHATSAPP_NUMBER`, `JMD_II_WHATSAPP_NUMBER`, `MD_WHATSAPP_NUMBER`, and secrets in the Console UI. Use **Secret Manager** for `INTERAKT_API_KEY` when possible.
