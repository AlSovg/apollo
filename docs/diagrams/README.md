# Схемы для курсовой (PlantUML, draw.io)

Исходники схем из `docs/architecture-diagrams.md` в нотации UML, оформлены по ГОСТ: монохром, Times New Roman, без теней. Общий стиль — `gost.iuml`. Подпись «Рисунок N — …» ставится в тексте работы.

| Файл | Схема | Нотация |
|---|---|---|
| `01-use-cases` | Контекст системы | UML, варианты использования |
| `03-seq-event-path` | Путь события от вебхука до письма | UML, последовательность |
| `04a-state-incident` | Жизненный цикл инцидента | UML, состояния |
| `04b-state-notification` | Жизненный цикл уведомления | UML, состояния |
| `05a-er-events` | Модель данных: события, инциденты, доставка | ER (IE) |
| `05b-er-users` | Модель данных: пользователи, роли, подписки | ER (IE) |
| `06a-deploy-two-nodes` | Публичный стенд, два узла | UML, развёртывание |
| `06b-deploy-dev-host` | Один хост для разработки | UML, развёртывание |
| `07a-scaling-current` | Масштабирование: сейчас | UML, развёртывание |
| `07b-scaling-target` | Масштабирование: целевая схема | UML, развёртывание |
| `08-seq-balancing` | Балансировка и отказ реплики | UML, последовательность |
| `09-seq-skip-locked` | Раздел очереди между воркерами | UML, последовательность |
| `11-trust-boundaries` | Границы доверия | UML, развёртывание |

Схемы по ГОСТ 19.701-90 (ЕСПД, схемы алгоритмов и данных) — в draw.io:

| Файл | Схема | Вид по ГОСТ 19.701 |
|---|---|---|
| `02-system-workflow.drawio` | Конвейер обработки событий | Схема работы системы (А4, альбомная) |
| `12-algorithm-delivery.drawio` | Доставка уведомлений (`services/delivery.py`) | Схема алгоритма (А4, книжная) |

Экспорт через draw.io desktop (`winget install JGraph.Draw`):

```bash
DRAWIO="$LOCALAPPDATA/Programs/draw.io/draw.io.exe"
"$DRAWIO" --export --format png --scale 3 --border 10 --output out/12-algorithm-delivery.png 12-algorithm-delivery.drawio
"$DRAWIO" --export --format svg --border 10 --output out/12-algorithm-delivery.svg 12-algorithm-delivery.drawio
```

## Рендер

Нужны Java, Graphviz и `tools/plantuml.jar` (в `.gitignore`, скачать с GitHub releases plantuml/plantuml).

```bash
cd docs/diagrams
java -jar ../../tools/plantuml.jar -charset UTF-8 -tsvg -o out *.puml
java -jar ../../tools/plantuml.jar -charset UTF-8 -tpng -Sdpi=300 -o out *.puml
```

Если `dot` не в PATH — задать `GRAPHVIZ_DOT="C:/Program Files/Graphviz/bin/dot.exe"`.
