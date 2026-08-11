"""Базовый класс всех Pydantic-схем API.

extra="forbid" обязателен для каждой схемы (раздел 4.1 ТЗ): неизвестное
поле в запросе — ошибка валидации, а не молчаливое игнорирование.
"""

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
