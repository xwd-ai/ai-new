FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && mkdir /data && chown 65534:65534 /data
COPY alembic.ini ./
COPY alembic ./alembic
COPY backend ./backend
COPY frontend ./frontend

USER 65534:65534
CMD ["sh", "-c", "python -m alembic upgrade head && exec python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000"]
