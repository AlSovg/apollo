import httpx


async def test_health_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_ready_without_db_returns_503(client: httpx.AsyncClient) -> None:
    # БД в тестовом окружении недоступна — готовность обязана честно отвечать 503
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


async def test_request_id_header_is_set(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.headers.get("X-Request-ID")

    echoed = await client.get("/health", headers={"X-Request-ID": "test-id-123"})
    assert echoed.headers["X-Request-ID"] == "test-id-123"


async def test_ingest_without_db_returns_503_with_retry_after(
    client: httpx.AsyncClient,
) -> None:
    # Узел БД недоступен: приём отвечает 503 и просит повторить, а не 500
    response = await client.post("/api/v1/ingest/zabbix", json={"event": 1})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
