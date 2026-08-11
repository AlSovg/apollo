# syntax=docker/dockerfile:1
# Подстановка локальной модели для стенда. В продуктивную конфигурацию не идёт.

FROM python:3.12-slim

RUN useradd --system --uid 10003 --no-create-home mockuser \
    && pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30"

WORKDIR /app
COPY deploy/mocks/llm/app.py ./app.py

USER 10003
EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
