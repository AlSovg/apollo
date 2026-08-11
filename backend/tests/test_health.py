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
