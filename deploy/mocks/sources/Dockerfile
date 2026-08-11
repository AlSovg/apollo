# syntax=docker/dockerfile:1
# Эмулятор Zabbix и SolarWinds для стенда. В продуктивную конфигурацию не идёт.

FROM python:3.12-slim

RUN useradd --system --uid 10004 --no-create-home mockuser \
    && pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27"

WORKDIR /app
COPY deploy/mocks/sources/app.py ./app.py

USER 10004
EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
