# syntax=docker/dockerfile:1

# ---- Tailwind CSS build ---------------------------------------------------------
FROM node:20-alpine AS assets
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tailwind.config.js ./
COPY app ./app
RUN npm run build:css

# ---- Flask application (web, Celery worker and beat) ----------------------------
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    FLASK_APP=wsgi.py
WORKDIR /app

RUN useradd --create-home --uid 1000 allied

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
COPY --from=assets /build/app/static/css/app.css app/static/css/app.css
RUN mkdir -p instance/uploads && chown -R allied:allied instance

USER allied
EXPOSE 12005
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]

# ---- Nginx reverse proxy with the static files baked in -------------------------
FROM nginx:1.27-alpine AS nginx
COPY deploy/nginx/nginx.conf /etc/nginx/nginx.conf
COPY --from=app /app/app/static /usr/share/nginx/static
