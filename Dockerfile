FROM python:3.12-slim

# git is required to clone repositories and build diffs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend
COPY run.py .

# Run as an unprivileged user; /data holds the database and cloned workspaces.
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data && chown -R app:app /data /app
USER app

# The Gemini key is NOT baked into the image. Supply GEMINI_API_KEY at run time.
ENV HOST=0.0.0.0 PORT=8000 DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8000'), timeout=3)"

CMD ["python", "run.py"]
