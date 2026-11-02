FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py VERSION ./
EXPOSE 8080
# 1 Worker, da die Vorschläge im Speicher liegen
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "--timeout", "180", "app:app"]
