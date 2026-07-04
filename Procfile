web: alembic --config api/alembic.ini upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --app-dir api
worker: OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES rq worker checks --worker-class rq.SimpleWorker --url $REDIS_URL
scheduler: python -m workers.scheduler
