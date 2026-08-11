"""Маскирование чувствительных значений перед отправкой в модель.

По умолчанию выключено: модель работает внутри контура, и подмена значений
только ухудшила бы качество сводки. Флаг существует на случай, когда модель
вынесут за пределы доверенной зоны — тогда имена хостов и адреса уходить
наружу не должны.

Подстановка обратимая: в ответе модели плейсхолдеры заменяются исходными
значениями, поэтому дежурный видит нормальный текст.
"""

import re
from dataclasses import dataclass, field

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_FQDN_RE = re.compile(r"\b[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?(?:\.[a-zA-Z]{2,})+\b")


@dataclass
class Masker:
    """Двусторонняя замена значений на плейсхолдеры."""

    _forward: dict[str, str] = field(default_factory=dict)
    _reverse: dict[str, str] = field(default_factory=dict)

    def _placeholder(self, value: str, prefix: str) -> str:
        if value in self._forward:
            return self._forward[value]
        used = sum(1 for name in self._reverse if name.startswith(f"<{prefix}_"))
        placeholder = f"<{prefix}_{used + 1}>"
        self._forward[value] = placeholder
        self._reverse[placeholder] = value
        return placeholder

    def add(self, value: str | None, prefix: str) -> None:
        """Зарегистрировать значение, известное из контекста (имя хоста, группа)."""
        if value and len(value) > 2:
            self._placeholder(value, prefix)

    def mask(self, text: str) -> str:
        # Сначала известные значения: они длиннее и специфичнее регулярок
        for value, placeholder in sorted(
            self._forward.items(), key=lambda item: len(item[0]), reverse=True
        ):
            text = text.replace(value, placeholder)

        text = _IPV4_RE.sub(lambda match: self._placeholder(match.group(), "IP"), text)
        return _FQDN_RE.sub(lambda match: self._placeholder(match.group(), "HOST"), text)

    def unmask(self, text: str) -> str:
        for placeholder, value in self._reverse.items():
            text = text.replace(placeholder, value)
        return text

    @property
    def used(self) -> bool:
        return bool(self._forward)
