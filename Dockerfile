FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    chromium chromium-sandbox \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && playwright install --with-deps chromium-headless-shell

COPY . .

ENV CHROME_PATH=/usr/bin/chromium
CMD ["python", "scheduler.py", "--interval", "30"]
