"""Выражения путей для декларативного маппинга полей.

Поддерживается подмножество JSONPath, покрывающее задачу маппинга:

    $.alerts[0].labels.severity
    $.status
    alerts[0].annotations.summary        (ведущий $. необязателен)

Фильтры, срезы и рекурсивный спуск (`..`, `[?(...)]`, `[*]`) не поддержаны
осознанно: они не нужны для соответствия полей, а полноценная библиотека
JSONPath — это ещё одна зависимость в контуре при том, что дерево пакетов
мы держим маленьким намеренно (раздел 2 AGENTS.md). Неподдержанный синтаксис
даёт понятную ошибку, а не молчаливый None.
"""

import re
from typing import Any

# Ключом в JSON может быть почти любая строка, включая кириллицу, поэтому
# запрещены только символы, ломающие сам разбор пути
_FORBIDDEN_IN_NAME = re.compile(r"[\[\]$]")
_UNSUPPORTED = ("..", "[?", "[*", "[:", ")")

SUPPORTED_SYNTAX = "$.поле.вложенное[0].поле"


class PathError(ValueError):
    """Выражение записано неверно или использует неподдержанный синтаксис."""


def parse_path(expression: str) -> list[str | int]:
    """Разобрать выражение в последовательность шагов."""
    raw = expression.strip()
    if not raw:
        raise PathError("Пустое выражение пути")
    for marker in _UNSUPPORTED:
        if marker in raw:
            raise PathError(
                f"Синтаксис '{marker}' не поддерживается. Допустимо: {SUPPORTED_SYNTAX}"
            )

    raw = raw.removeprefix("$")
    raw = raw.removeprefix(".")

    steps: list[str | int] = []
    for part in raw.split("."):
        if not part:
            raise PathError(f"Пустой шаг в выражении '{expression}'")
        # Имя может нести индексы: labels[0][1]
        name, _, indexes = part.partition("[")
        if name:
            if _FORBIDDEN_IN_NAME.search(name):
                raise PathError(f"Недопустимое имя поля '{name}' в '{expression}'")
            steps.append(name)
        if indexes:
            for chunk in indexes.rstrip("]").split("]["):
                if not chunk.lstrip("-").isdigit():
                    raise PathError(f"Индекс должен быть числом: '[{chunk}]'")
                steps.append(int(chunk))
    if not steps:
        raise PathError(f"Выражение '{expression}' не содержит шагов")
    return steps


def extract(payload: Any, expression: str) -> Any:
    """Значение по пути. Отсутствие значения — None, а не ошибка.

    Ошибкой считается только неверно записанное выражение: тогда интегратор
    должен увидеть проблему сразу, на кнопке «тест».
    """
    current = payload
    for step in parse_path(expression):
        if current is None:
            return None
        if isinstance(step, int):
            if not isinstance(current, list) or not -len(current) <= step < len(current):
                return None
            current = current[step]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(step)
    return current
