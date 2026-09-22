FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PDFAIRY_HOST=0.0.0.0 \
    PDFAIRY_ENV=production \
    PDFAIRY_ENABLE_OFFICE=false \
    PDFAIRY_TEMP_DIR=/tmp/pdfairy

WORKDIR /app
RUN groupadd --system --gid 10001 pdfairy && useradd --system --uid 10001 --gid pdfairy --home-dir /app pdfairy \
    && mkdir -p /tmp/pdfairy && chown pdfairy:pdfairy /tmp/pdfairy
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py index.html styles.css app.js favicon.svg pdfairy-fairy-logo.png pdfairy-favicon.png privacy.html terms.html robots.txt ./
USER pdfairy
EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python", "-c", "import os,urllib.request; port=os.getenv('PDFAIRY_PORT') or os.getenv('PORT','8080'); urllib.request.urlopen('http://127.0.0.1:'+port+'/health',timeout=3)"]
CMD ["python", "server.py"]
