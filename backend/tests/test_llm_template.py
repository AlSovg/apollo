"""Шаблонная сводка: чистая функция от текста контекста, БД и сети не нужны."""

from app.adapters.llm import TemplateSummarizer, build_llm_client
from app.config import Settings

OPEN_DISK = """Источник: zabbix
Правило: Диск заполнен на 94%
Критичность: 4 из 5 (в источнике: High)
Статус: открыт
Объект: gpn-db-prod-02
Группа объектов: Базы данных
Повторов события: 3
Лента событий:
  2026-08-10 10:00:00 — problem, значение 88
  2026-08-10 10:20:00 — update, значение 94
"""

CLOSED = """Источник: zabbix
Правило: Репликация остановлена
Критичность: 5 из 5 (в источнике: Disaster)
Статус: закрыт
Объект: gpn-db-prod-02
Лента событий:
  2026-08-10 05:00:00 — problem, значение 0
  2026-08-10 05:38:00 — resolved, значение 1
"""


async def test_open_incident_uses_fields_metrics_and_hint() -> None:
    result = await TemplateSummarizer().summarize("prompt", OPEN_DISK)

    text = result.content
    assert "Проблема высокой критичности «Диск заполнен на 94%»" in text
    assert "на объекте gpn-db-prod-02 (группа Базы данных)" in text
    assert "выросло с 88 до 94" in text
    assert "повторилось 3 раз" in text
    assert "свободное место" in text
    assert result.model == "template-v1"


async def test_closed_incident_has_no_check_hint() -> None:
    text = (await TemplateSummarizer().summarize("prompt", CLOSED)).content

    assert "закрыто" in text
    assert "(проблема закрыта)" in text
    assert "Проверьте в первую очередь" not in text


async def test_unknown_rule_falls_back_to_generic_hint() -> None:
    context = "Правило: Необычное\nКритичность: 3 из 5 (в источнике: x)\nСтатус: открыт\n"

    text = (await TemplateSummarizer().summarize("prompt", context)).content

    assert "состояние объекта и данные источника" in text


async def test_output_is_deterministic() -> None:
    first = await TemplateSummarizer().summarize("prompt", OPEN_DISK)
    second = await TemplateSummarizer().summarize("prompt", OPEN_DISK)

    assert first.content == second.content


def test_factory_returns_template_client() -> None:
    settings = Settings(database_url="postgresql+asyncpg://x:x@db/x", llm_provider="template")

    assert isinstance(build_llm_client(settings), TemplateSummarizer)
