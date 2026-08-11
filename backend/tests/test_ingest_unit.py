"""Юнит-тесты приёма: разбор тела, аутентификация, хэш. Без БД."""

import hashlib
import hmac

import pytest

from app.api.errors import ApiError
from app.services.ingest import (
    build_payload,
    check_ip_allowed,
    payload_hash,
    signed_bytes,
    token_hash,
    verify_signature,
    verify_token,
)


def test_verify_token_accepts_only_matching_token() -> None:
    stored = token_hash("правильный-токен")
    assert verify_token("правильный-токен", stored) is True
    assert verify_token("другой-токен", stored) is False
    assert verify_token(None, stored) is False
    # Источник без выданного токена не принимает ничего: fail closed
    assert verify_token("любой", None) is False


def test_verify_signature() -> None:
    secret = b"s3cret"
    body = b'{"event_id": "59"}'
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()

    assert verify_signature(body, signature, secret) is True
    assert verify_signature(body, signature.upper(), secret) is True
    assert verify_signature(body, "deadbeef", secret) is False
    assert verify_signature(body, None, secret) is False
    # Секрет не задан — подпись не требуется
    assert verify_signature(body, None, None) is True
    # Изменённое тело ломает подпись
    assert verify_signature(body + b" ", signature, secret) is False


def test_check_ip_allowed() -> None:
    assert check_ip_allowed("10.0.0.1", []) is True  # список пуст — проверка выключена
    assert check_ip_allowed("90.156.212.194", ["90.156.212.194"]) is True
    assert check_ip_allowed("90.156.212.195", ["90.156.212.194"]) is False
    assert check_ip_allowed("10.0.3.14", ["10.0.0.0/8"]) is True
    assert check_ip_allowed(None, ["10.0.0.0/8"]) is False
    assert check_ip_allowed("не-адрес", ["10.0.0.0/8"]) is False


def test_build_payload_from_json() -> None:
    payload = build_payload(b'{"event_id": "59"}', "application/json", "")
    assert payload == {"event_id": "59"}


def test_build_payload_from_form() -> None:
    body = b"name=High+CPU&severity=Critical"
    payload = build_payload(body, "application/x-www-form-urlencoded; charset=utf-8", "")
    assert payload == {"name": "High CPU", "severity": "Critical"}


def test_build_payload_from_query_string() -> None:
    # Так шлёт SolarWinds: параметры в URL, тело пустое
    payload = build_payload(b"", "", "name=High+CPU+Utilization&node=WINDOWS-STD3-80")
    assert payload == {"name": "High CPU Utilization", "node": "WINDOWS-STD3-80"}


def test_build_payload_rejects_broken_json() -> None:
    with pytest.raises(ApiError) as exc:
        build_payload(b"{not json", "application/json", "")
    assert exc.value.status_code == 400


def test_build_payload_rejects_empty_request() -> None:
    with pytest.raises(ApiError) as exc:
        build_payload(b"", "application/json", "")
    assert exc.value.code == "EMPTY_PAYLOAD"


def test_hash_and_signed_bytes_use_query_when_body_is_empty() -> None:
    query = "name=A&node=B"
    assert payload_hash(b"", query) == hashlib.sha256(query.encode()).digest()
    assert signed_bytes(b"", query) == query.encode()

    body = b'{"a": 1}'
    assert payload_hash(body, query) == hashlib.sha256(body).digest()
    assert signed_bytes(body, query) == body


def test_different_query_strings_hash_differently() -> None:
    # Иначе все события SolarWinds схлопнулись бы в один хэш пустого тела
    assert payload_hash(b"", "node=A") != payload_hash(b"", "node=B")
