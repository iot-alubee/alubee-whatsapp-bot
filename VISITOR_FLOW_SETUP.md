# Visitor WhatsApp Form (Interakt)

## Flow in production

1. Employee sends **Hi**
2. Chooses **Visitor Request** (menu option 5)
3. Bot sends **WhatsApp Form** template (`VISITOR_FLOW_TEMPLATE_NAME`)
4. Employee fills form and taps **Submit**
5. Webhook receives flow response → creates `VISITOR` request → **Visitor JMD → Visitor MD**
6. After MD approve → OTP to employee + guest (same as before)

If `VISITOR_FLOW_TEMPLATE_NAME` is not set, the bot falls back to chat step-by-step questions.

## Interakt setup (you already published the form)

Your flow **Visitor Request** is published (e.g. Flow ID `26519662057712629`).

1. ~~WhatsApp Forms → publish~~ **Done**
2. **Templates** → create an **approved** template with button type **WhatsApp Flow**, linked to that flow
3. **Developer settings** → enable webhooks:
   - `message_received` (incoming messages)
   - `message_api_flow_response` or **Completed Flow** (template API sends)

4. **Cloud Run env** (or local `.env`):

```
VISITOR_FLOW_TEMPLATE_NAME=your_approved_template_name
VISITOR_FLOW_TEMPLATE_LANGUAGE_CODE=en
VISITOR_FLOW_TEMPLATE_BODY_FIELDS=name
```

Use the same field names in the form as in `visitor_flow.json` when possible:

- `coming_on`, `coming_from`, `purpose`, `other_purpose`
- `no_of_people`, `visitor_name`, `visitor_mobile`

The code also matches Interakt auto-generated screen field keys (substring match).

## Health check

`GET /health` should show:

- `"visitor_flow_enabled": true`
- `"visitor_flow_template": "your_template_name"`

## Regenerate JSON from code

```bash
cd Interakt
python generate_visitor_flow_json.py
```

Import `visitor_flow.json` in Interakt only if you build the form from JSON; UI-built forms are fine too.
