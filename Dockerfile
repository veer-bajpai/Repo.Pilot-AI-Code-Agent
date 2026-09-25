# ============================================================
# Stage 1: Build React frontend
# ============================================================
FROM node:22-slim AS web-build

WORKDIR /web

COPY frontend-react/package*.json ./
RUN npm ci

COPY frontend-react/ ./
RUN npm run build


# ============================================================
# Stage 2: Python backend + production runtime
# ============================================================
FROM python:3.12-slim

# ------------------------------------------------------------
# System dependencies
# git is required for repository cloning/workspace operations.
# ca-certificates is required for HTTPS connections.
# ------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*


# ------------------------------------------------------------
# Application directory
# ------------------------------------------------------------
WORKDIR /app


# ------------------------------------------------------------
# Python dependencies
# ------------------------------------------------------------
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt


# ------------------------------------------------------------
# Backend application
# ------------------------------------------------------------
COPY backend ./backend


# ------------------------------------------------------------
# Alembic configuration
#
# IMPORTANT:
# alembic upgrade head runs from /app, so alembic.ini must
# exist in /app.
# ------------------------------------------------------------
COPY alembic.ini ./alembic.ini


# ------------------------------------------------------------
# Production frontend
# ------------------------------------------------------------
COPY --from=web-build /web/dist ./frontend


# ------------------------------------------------------------
# Application entry point
# ------------------------------------------------------------
COPY run.py ./run.py


# ------------------------------------------------------------
# Create unprivileged application user
#
# /data is used for persistent/local application data.
# ------------------------------------------------------------
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /data /app


USER app


# ------------------------------------------------------------
# Runtime environment
#
# DATABASE_URL and GEMINI_API_KEY should be supplied through
# Render Environment Variables, NOT baked into this image.
# ------------------------------------------------------------
ENV HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1


# ------------------------------------------------------------
# Persistent data directory
# ------------------------------------------------------------
VOLUME ["/data"]


# ------------------------------------------------------------
# Render/web-service port
# ------------------------------------------------------------
EXPOSE 8000


# ------------------------------------------------------------
# Health check
# ------------------------------------------------------------
HEALTHCHECK \
    --interval=30s \
    --timeout=5s \
    --start-period=10s \
    --retries=3 \
    CMD python -c "import urllib.request, os; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8000'), timeout=3)"


# ------------------------------------------------------------
# Production startup
#
# 1. Run database migrations.
# 2. Start RepoPilot.
#
# DATABASE_URL is read by backend/alembic/env.py.
# ------------------------------------------------------------
CMD ["sh", "-c", "alembic upgrade head && python run.py"]