FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -c "import pypdf, pypdfium2, PIL; print(\"PDF-Bibliotheken ok\")"
COPY app.py VERSION ./
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8080
# 1 Worker, da die Vorschläge im Speicher liegen
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "--timeout", "180", "app:app"]
