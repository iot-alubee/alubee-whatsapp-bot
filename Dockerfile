# WhatsApp bot — deploy from repo root (not alubee_flask_app)
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV PORT=8080
ENV FIREBASE_PROJECT_ID=whatsapp-approval-system
EXPOSE 8080

CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
