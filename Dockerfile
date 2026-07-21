FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

# Directorio persistente para perfil de navegador (Cloudflare cookies/fingerprint)
RUN mkdir -p /app/chrome_cf_profile
VOLUME /app/chrome_cf_profile

COPY . .

ENV CARDVAULT_CF_PROFILE_DIR=/app/chrome_cf_profile
CMD ["python", "scheduler.py", "--interval", "30"]
