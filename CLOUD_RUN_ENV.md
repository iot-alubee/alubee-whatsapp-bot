# Cloud Run environment variables

## Settings file (non-secrets)

**Edit `Interakt/bot_config.env`** in the repo for approver numbers, template names, flow templates, etc.  
It is **deployed with the bot image** — the app reads it at startup (not from Cloud Run env).

After editing `bot_config.env`, redeploy from `Interakt/Production/`.

## Secrets only on Cloud Run

Set **only** these in **Google Cloud Console → Cloud Run → Variables & secrets**:

| Name | Purpose |
|------|---------|
| `INTERAKT_API_KEY` | [Interakt Developer settings](https://app.interakt.ai/settings/developer-setting) |
| `WHATSAPP_CLOUD_API_TOKEN` | Meta token — IT/Maintenance issue photo download from Flow |

Do **not** put secrets in `bot_config.env`. Do **not** duplicate template names / approver numbers on Cloud Run unless you need an emergency override (file wins for non-secrets).

Local dev: copy `Interakt/.env.example` → `Interakt/.env` for secrets; settings still come from `bot_config.env`.

---

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

## Required on Cloud Run (secrets)

| Name | Example / value |
|------|-----------------|
| `INTERAKT_API_KEY` | From Interakt Developer settings |
| `WHATSAPP_CLOUD_API_TOKEN` | Meta permanent token (if IT photos enabled) |

All other keys below live in **`bot_config.env`** — remove them from Cloud Run when possible.

## Legacy Cloud Run vars (now in bot_config.env)
| `PPC_WHATSAPP_NUMBER` | CL permission — first approver (PPC) |
| `HR_WHATSAPP_NUMBER` | CL permission — final approver (HR) |
| `VISITOR_OTP_TEMPLATE_NAME` | `visitor_pass_code` |
| `VISITOR_OTP_TEMPLATE_LANGUAGE_CODE` | `en` |
| `VISITOR_OTP_TEMPLATE_BODY_FIELDS` | `otp` |
| `VISITOR_OTP_TEMPLATE_AUTH_BUTTON` | `true` |

## WhatsApp Flow utility templates (menu Form options)

Set these after Meta approves each **Utility** template in Interakt. Body must include `{{1}}` (employee name).

| Name | Approved template |
|------|-------------------|
| `OD_FLOW_TEMPLATE_NAME` | `od_request_v02` |
| `VISITOR_FLOW_TEMPLATE_NAME` | `visitor_request_v02` |
| `LEAVE_FLOW_TEMPLATE_NAME` | `leave_request_v02` |
| `PERMISSION_FLOW_TEMPLATE_NAME` | `permission_request_02` |
| `*_FLOW_TEMPLATE_LANGUAGE_CODE` | `en` |
| `*_FLOW_TEMPLATE_BODY_FIELDS` | `name` |

**Permission only:** bot sends `flow_token=perm_{phone}` (no `flow_action_data`).  
**OD / Leave / Visitor:** template + body only (no flow button parameters).

All forms use flow endpoint `https://alubee-whatsapp-flow-….run.app/flow` (Data Exchange).

### Copy-paste block for Cloud Run

```
OD_FLOW_TEMPLATE_NAME=od_request_v02
OD_FLOW_TEMPLATE_LANGUAGE_CODE=en
OD_FLOW_TEMPLATE_BODY_FIELDS=name
VISITOR_FLOW_TEMPLATE_NAME=visitor_request_v02
VISITOR_FLOW_TEMPLATE_LANGUAGE_CODE=en
VISITOR_FLOW_TEMPLATE_BODY_FIELDS=name
LEAVE_FLOW_TEMPLATE_NAME=leave_request_v02
LEAVE_FLOW_TEMPLATE_LANGUAGE_CODE=en
LEAVE_FLOW_TEMPLATE_BODY_FIELDS=name
PERMISSION_FLOW_TEMPLATE_NAME=permission_request_02
PERMISSION_FLOW_TEMPLATE_LANGUAGE_CODE=en
PERMISSION_FLOW_TEMPLATE_BODY_FIELDS=name
```

## JMD / MD approval templates (OD, Leave, Visitor)

Utility templates with **Quick Reply** buttons (`Approve`, `Deny`; Leave also `Manage`).  
**No 24h session required** — bot uses defaults if env vars are unset.

| Type | Default template name | Env override |
|------|----------------------|--------------|
| OD | `od_approval` | `OD_APPROVAL_TEMPLATE_NAME` |
| Leave | `leave_approval` | `LEAVE_APPROVAL_TEMPLATE_NAME` |
| Visitor | `visitor_approval` | `VISITOR_APPROVAL_TEMPLATE_NAME` |

```
APPROVAL_TEMPLATE_LANGUAGE_CODE=en
```

Override only if your Meta template names differ:

```
OD_APPROVAL_TEMPLATE_NAME=od_approval
LEAVE_APPROVAL_TEMPLATE_NAME=leave_approval
VISITOR_APPROVAL_TEMPLATE_NAME=visitor_approval
```

Body field order (`*_APPROVAL_TEMPLATE_BODY_FIELDS`) — see `.env.example` if your Meta `{{1}}`… order differs.

Session **Approve/Deny** buttons are fallback only if template send fails.

## IT engineer assignment templates

Two **Utility** templates (no 24h session). Same body text and `{{1}}`–`{{8}}` in both.

| When | Env var | Meta template |
|------|---------|---------------|
| User **attached photo** | `IT_ENGINEER_ASSIGN_TEMPLATE_NAME` | `it_ticket_notification` — **Image** header (dynamic issue photo) |
| User **did not attach photo** | `IT_ENGINEER_ASSIGN_BODY_TEMPLATE_NAME` | `it_ticket_notification_no_image` — **no header** |

```
IT_ENGINEER_ASSIGN_TEMPLATE_NAME=it_ticket_notification
IT_ENGINEER_ASSIGN_BODY_TEMPLATE_NAME=it_ticket_notification_no_image
IT_ENGINEER_ASSIGN_TEMPLATE_LANGUAGE_CODE=en
```

No default/fallback image is used. Create the no-header template in Meta with header = **None** and the same body as the image version.

Session image/text is only used if template send fails.

## Optional

| Name | Default | Purpose |
|------|---------|---------|
| `WHATSAPP_SESSION_HOURS` | `24` | Session window for permission approvals and legacy Approve/Deny button fallback |
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
