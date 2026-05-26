# Interakt OD bot (no Meta templates)

Uses Interakt session payloads on `POST https://api.interakt.ai/v1/public/message/`:

| Step | Interakt |
|------|----------|
| Hi → menu | **InteractiveList** — View Options (5 request types) |
| OD reason | **InteractiveButton** — Unit I / II / Other |
| Company vehicle | **InteractiveButton** — YES / NO |
| Vehicle pick | **Text** only — dynamic numbered list (all vehicles; reply number or ID) |
| Manager / MD approval | **InteractiveButton** — Approve / Deny (active session only) |
| Confirmations | **Text** |

Turn off Interakt Greeting / welcome automations in the dashboard.

**Approval chain:** Employee → **JMD I or JMD II** (from employee list: `JMD1` / `JMD2`, stored as `jmd_route`) → **MD (final)**. Env: `JMD_I_WHATSAPP_NUMBER`, `JMD_II_WHATSAPP_NUMBER`, `MD_WHATSAPP_NUMBER`. Notifications only if that approver messaged Alubee within `WHATSAPP_SESSION_HOURS` (default 24). Re-run `python load_users.py` after changing the employee list.

## Local setup

```powershell
cd Interakt
copy .env.example .env
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Webhook: `https://YOUR-NGROK/webhook` — **message_received**.

## Cloud Run deploy

Use **`Production/`** as the build context (see `Production/DEPLOY.md`). Set env vars in Cloud Run — no `.env` in the image.

## Files

- `interakt_api.py` — `send_list_menu`, `send_reply_buttons`, `send_text`, `ensure_customer`
- `main.py` — webhook + Firestore OD flow
