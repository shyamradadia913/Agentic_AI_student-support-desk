# Works on Hugging Face Spaces (Docker), Railway, Fly.io, Koyeb or any container host.
FROM python:3.11-slim
RUN useradd -m -u 1000 user
WORKDIR /app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .
USER user
# The database is created and seeded with the demo data on first start.
ENV PORT=7860 COOKIE_SECURE=1 DB_PATH=/tmp/support.db PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
