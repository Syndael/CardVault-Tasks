FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-dejavu-core xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

RUN mkdir -p /app/chrome_cf_profile
VOLUME /app/chrome_cf_profile

COPY . .

ENV CARDVAULT_CF_PROFILE_DIR=/app/chrome_cf_profile
ENV DISPLAY=:99
CMD Xvfb :99 -screen 0 1280x1024x24 & python scheduler.py --interval 30
