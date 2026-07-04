FROM python:3.12-slim

WORKDIR /app

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /shared
COPY api/app ./app
COPY api/alembic ./alembic
COPY api/alembic.ini .

ENV PYTHONPATH=/:/app

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
