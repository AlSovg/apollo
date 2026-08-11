"""Конфигурация приложения.

Все значения берутся из переменных окружения. У секретов (DATABASE_URL)
значений по умолчанию нет намеренно: запуск без явной конфигурации должен
падать, а не молча подключаться куда-то.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # postgresql+asyncpg://user:password@host:5432/dbname — секрет, без дефолта
    database_url: str

    app_env: str = "dev"
    log_level: str = "INFO"

    # Пул соединений с БД. По умолчанию SQLAlchemy даёт 5+10, чего не хватает
    # на пиковом приёме — запросы начинают ждать соединение, а не базу
    db_pool_size: int = 20
    db_max_overflow: int = 20

    # На сколько месяцев вперёд создавать партиции при старте
    partitions_months_ahead: int = 1

    # Локальное время сервера Zabbix: в payload таймзоны нет. Используется,
    # если у источника не задана timezone в реестре event_sources.
    zabbix_tz: str = "Europe/Moscow"

    # --- Аутентификация ---
    # ldap | local. Переключение не требует правок кода
    auth_provider: str = "local"
    # Секрет подписи токенов сессии — значения по умолчанию быть не должно.
    # 32 байта: короче минимума, рекомендованного RFC 7518 для HMAC-SHA256
    jwt_secret: str = Field(min_length=32)
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_hours: int = 12
    # Блокировка после N неудачных попыток (только локальный провайдер:
    # для каталога политика блокировок живёт на его стороне)
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    # Secure-флаг cookie: выключается только для локальной отладки по http
    cookie_secure: bool = True

    # --- Каталог (LDAP/AD) ---
    ldap_host: str = ""
    ldap_port: int = 636
    ldap_use_ssl: bool = True
    ldap_base_dn: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    # sAMAccountName в AD, uid в OpenLDAP
    ldap_username_attribute: str = "sAMAccountName"
    # objectGUID в AD, entryUUID в OpenLDAP: стабилен при переименовании и переносе
    ldap_id_attribute: str = "objectGUID"
    ldap_group_object_class: str = "group"
    ldap_ca_certs_file: str = ""
    ldap_timeout_s: int = 10
    ldap_sync_interval_minutes: int = 60
    # Атрибуты каталога, из которых берутся адреса доставки. В БД не сохраняются
    ldap_email_attribute: str = "mail"
    ldap_trueconf_attribute: str = "ipPhone"

    # --- Доставка ---
    router_batch_size: int = 100
    router_poll_interval_s: float = 1.0
    delivery_batch_size: int = 50
    delivery_poll_interval_s: float = 1.0
    # Задержки повторов в минутах; после последней попытки статус failed
    delivery_retry_delays_minutes: str = "1,5,30"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_sender: str = "alerts@example.local"
    smtp_use_tls: bool = True

    # --- Языковая модель (ВМ №4, OpenAI-совместимый endpoint) ---
    llm_base_url: str = ""
    llm_model: str = "qwen3-14b"
    llm_api_key: str = ""
    # Qwen3-14B-AWQ на Tesla T4 выдаёт ~15 ток/с: сводка занимает 7–15 секунд
    llm_timeout_s: float = 60.0
    llm_max_attempts: int = 2
    llm_verify_tls: bool = True
    # Отключает блок рассуждений у гибридных reasoning-моделей (Qwen3)
    llm_disable_thinking: bool = True
    # Пачка обрабатывается одной транзакцией, а сводка занимает секунды:
    # при большом размере блокировки висят минутами, а рестарт воркера
    # выбрасывает всю незакоммиченную пачку
    llm_batch_size: int = 5
    llm_poll_interval_s: float = 5.0
    llm_prompt_version: str = "v1"
    # Маскирование выключено: модель работает внутри контура (FR-AI-6)
    llm_mask_pii: bool = False

    # Квитирование заложено структурно, но в прототипе выключено (FR-FUT-1)
    feature_ack: bool = False

    trueconf_base_url: str = ""
    trueconf_client_id: str = ""
    trueconf_client_secret: str = ""
    trueconf_verify_tls: bool = True

    @property
    def retry_delays(self) -> list[int]:
        return [int(part) for part in self.delivery_retry_delays_minutes.split(",") if part]

    # --- Нормализация ---
    normalizer_batch_size: int = 100
    normalizer_poll_interval_s: float = 1.0
    # Окно подавления повторов: повтор внутри него инкрементит счётчик,
    # но нового уведомления не порождает (FR-INC-4)
    suppress_window_minutes: int = 15
    # Потолок глушения добровольной подписки: она не должна замолкать навсегда
    max_mute_hours: int = 24

    # --- Приём вебхуков ---
    # Тело больше лимита отклоняется с 413 (nginx режет на том же значении)
    ingest_max_body_bytes: int = 1_048_576
    # Ограничение частоты на источник; счётчик в памяти процесса
    ingest_rate_limit_rps: int = 200
    # IP клиента берётся из X-Real-IP: бэкенд стоит только за nginx,
    # его порт наружу не публикуется
    trust_proxy_headers: bool = True


@lru_cache
def get_settings() -> Settings:
    # Поля читаются из окружения (pydantic-settings), аргументы не передаются
    return Settings()
