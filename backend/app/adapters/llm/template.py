"""Шаблонная сводка вместо языковой модели.

Тот же контракт LlmClient, но текст собирается детерминированно: из контекста
инцидента вычленяются правило, объект, критичность, значения метрик и
динамика, и подставляются в фиксированные конструкции предложений. Никаких
запросов наружу, поэтому подходит для стенда без ВМ с моделью и для защиты:
результат воспроизводим, а разбор «почему в сводке написано это» упирается в
строку кода.
"""

import re
from dataclasses import dataclass, field

from app.adapters.llm.base import LlmResult

MODEL_NAME = "template-v1"

_SEVERITY_WORDS = {
    1: "Информационное событие",
    2: "Предупреждение",
    3: "Проблема средней критичности",
    4: "Проблема высокой критичности",
    5: "Критическая проблема",
}

# (подстрока в названии правила или условии, последствие, что проверить первым)
_HINTS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("недоступ", "unreachable", "down", "icmp", "ping"),
        "объект не отвечает, сервисы на нём недоступны",
        "доступность узла по сети и состояние питания или гипервизора",
    ),
    (
        ("диск", "disk", "space", "filesystem"),
        "при заполнении диска остановятся запись и работа служб",
        "свободное место и то, что быстрее всего растёт на разделе",
    ),
    (
        ("cpu", "процессор", "загрузк"),
        "рост задержек и очередей на объекте",
        "процессы, которые загружают процессор",
    ),
    (
        ("памят", "memory", "swap"),
        "нехватка памяти ведёт к своппингу и аварийному завершению процессов",
        "потребление памяти по процессам",
    ),
    (
        ("репликац", "replication"),
        "реплика отстаёт, при отказе основного узла возможна потеря данных",
        "состояние репликации и связность между узлами",
    ),
    (
        ("температур", "temperature", "temp"),
        "перегрев может привести к отключению оборудования",
        "охлаждение и вентиляцию в помещении или стойке",
    ),
    (
        ("5xx", "ошибк", "error"),
        "пользователи получают отказы в обслуживании",
        "журналы приложения и последние изменения",
    ),
    (
        ("отклик", "latency", "response"),
        "сервис отвечает медленно, растёт риск таймаутов у клиентов",
        "нагрузку и состояние зависимых систем",
    ),
    (
        ("перезапуск", "restart", "uptime"),
        "служба перезапускалась, возможны кратковременные сбои",
        "причину перезапуска в журналах службы",
    ),
    (
        ("interface", "интерфейс", "link", "bandwidth", "канал"),
        "потеря или перегрузка канала связи",
        "состояние порта и загрузку канала на обоих концах",
    ),
)

_DEFAULT_HINT = (
    "возможна деградация обслуживания, если показатель продолжит ухудшаться",
    "состояние объекта и данные источника",
)

_EVENT_RE = re.compile(
    r"^\s+(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) — (?P<kind>\w+)"
    r"(?:, значение (?P<value>.+))?$"
)
_NUMBER_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")


@dataclass
class _Parsed:
    fields: dict[str, str] = field(default_factory=dict)
    kinds: list[str] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    raw_values: list[str] = field(default_factory=list)


def _parse(context: str) -> _Parsed:
    parsed = _Parsed()
    for line in context.splitlines():
        event = _EVENT_RE.match(line)
        if event:
            parsed.kinds.append(event["kind"])
            value = event["value"]
            if value:
                parsed.raw_values.append(value)
                if _NUMBER_RE.match(value.strip()):
                    parsed.values.append(float(value.strip().replace(",", ".")))
            continue
        key, sep, value = line.partition(": ")
        if sep and not line.startswith(" "):
            parsed.fields[key] = value.strip()
    return parsed


def _number(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}".replace(".", ",")


def _hint(rule: str, expression: str) -> tuple[str, str]:
    haystack = f"{rule} {expression}".lower()
    for keywords, consequence, check in _HINTS:
        if any(keyword in haystack for keyword in keywords):
            return consequence, check
    return _DEFAULT_HINT


def _severity_word(fields: dict[str, str]) -> str:
    match = re.match(r"(\d)", fields.get("Критичность", ""))
    return _SEVERITY_WORDS.get(int(match[1]), "Событие") if match else "Событие"


def _dynamics(parsed: _Parsed, closed: bool) -> str | None:
    """Предложение про метрику: последнее значение и как оно менялось."""
    if parsed.values:
        first, last = parsed.values[0], parsed.values[-1]
        if len(parsed.values) > 1 and first != last:
            direction = "выросло" if last > first else "снизилось"
            text = (
                f"Значение показателя {direction} с {_number(first)} до {_number(last)}"
            )
        else:
            text = f"Последнее значение показателя — {_number(last)}"
    elif parsed.raw_values:
        text = f"Последнее значение показателя — {parsed.raw_values[-1]}"
    else:
        text = None

    repeats = parsed.fields.get("Повторов события")
    if repeats and repeats.isdigit() and int(repeats) > 1:
        suffix = f"событие повторилось {repeats} раз"
        text = f"{text}, {suffix}" if text else suffix.capitalize()
    if text is None:
        return None
    return f"{text}{' (проблема закрыта)' if closed else ''}."


class TemplateSummarizer:
    """LlmClient без модели: сводка собирается из полей контекста."""

    async def summarize(self, prompt: str, context: str) -> LlmResult:
        parsed = _parse(context)
        fields = parsed.fields
        rule = fields.get("Правило", "инцидент")
        closed = fields.get("Статус") == "закрыт" or (
            bool(parsed.kinds) and parsed.kinds[-1] == "resolved"
        )

        where = f" на объекте {fields['Объект']}" if "Объект" in fields else ""
        group = f" (группа {fields['Группа объектов']})" if "Группа объектов" in fields else ""
        source = f", источник {fields['Источник']}" if "Источник" in fields else ""
        state = "закрыто" if closed else "открыто"
        parts = [
            f"{_severity_word(fields)} «{rule}»{where}{group}{source}: {state}."
        ]

        dynamics = _dynamics(parsed, closed)
        if dynamics:
            parts.append(dynamics)

        if closed:
            parts.append("Убедитесь, что показатель остаётся в норме, и зафиксируйте причину.")
        else:
            consequence, check = _hint(rule, fields.get("Условие срабатывания", ""))
            parts.append(f"Чем грозит: {consequence}. Проверьте в первую очередь: {check}.")

        return LlmResult(content=" ".join(parts), model=MODEL_NAME, latency_ms=0)
