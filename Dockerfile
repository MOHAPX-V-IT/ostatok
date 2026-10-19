FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home app
COPY --chown=10001:10001 ostatok ./ostatok
COPY --chown=10001:10001 migrations ./migrations
COPY --chown=10001:10001 console ./console
USER 10001:10001
EXPOSE 4511
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4511/health', timeout=4)" || exit 1
CMD ["python", "-m", "uvicorn", "ostatok.app:app", "--host", "0.0.0.0", "--port", "4511", "--limit-concurrency", "100"]
