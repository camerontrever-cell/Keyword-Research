FROM python:3.12-slim

# No third-party packages: the app is standard library only.
WORKDIR /app
COPY app.py analysis.py competitor.py xlsx_writer.py index.html ./

# Writable state (per-visitor history + exports)
RUN mkdir -p /app/history /app/exports
ENV PORT=8080 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# Single process, multi-threaded. Sessions live in memory, so do NOT scale to
# multiple replicas without moving SESSIONS to Redis (see DEPLOY.md).
CMD ["python3", "app.py"]
