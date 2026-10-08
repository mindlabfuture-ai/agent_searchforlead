FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY leadagent ./leadagent
ENV PYTHONUNBUFFERED=1 LEADS_DB=/data/leads.db
CMD ["python", "-m", "leadagent", "serve"]
