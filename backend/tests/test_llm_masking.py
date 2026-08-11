"""Маскирование значений перед отправкой в модель."""

from app.adapters.llm import Masker


def test_ip_addresses_are_masked_and_restored() -> None:
    masker = Masker()

    masked = masker.mask("Узел 10.0.3.14 не отвечает, шлюз 10.0.3.1")

    assert "10.0.3.14" not in masked
    assert "10.0.3.1" not in masked
    # Обратная подстановка возвращает исходный текст
    assert masker.unmask(masked) == "Узел 10.0.3.14 не отвечает, шлюз 10.0.3.1"


def test_same_value_gets_same_placeholder() -> None:
    masker = Masker()

    masked = masker.mask("10.0.3.14 и снова 10.0.3.14")

    assert masked.count("<IP_1>") == 2


def test_known_host_names_are_masked() -> None:
    masker = Masker()
    masker.add("db-prod-03", "HOST")

    masked = masker.mask("Диск на db-prod-03 заполнен")

    assert "db-prod-03" not in masked
    assert masker.unmask(masked) == "Диск на db-prod-03 заполнен"


def test_fqdn_is_masked() -> None:
    masker = Masker()

    masked = masker.mask("Сервис недоступен на portal.gpn.local")

    assert "portal.gpn.local" not in masked
    assert masker.unmask(masked) == "Сервис недоступен на portal.gpn.local"


def test_model_answer_is_restored() -> None:
    """Дежурный должен увидеть нормальный текст, а не плейсхолдеры."""
    masker = Masker()
    masker.add("db-prod-03", "HOST")
    masker.mask("Объект: db-prod-03, адрес 10.0.3.14")

    answer = "На <HOST_1> закончилось место, проверьте <IP_1>."

    assert masker.unmask(answer) == "На db-prod-03 закончилось место, проверьте 10.0.3.14."


def test_masker_reports_whether_it_was_used() -> None:
    empty = Masker()
    empty.mask("Свободного места мало")
    assert empty.used is False

    used = Masker()
    used.mask("Узел 10.0.3.14")
    assert used.used is True


def test_short_values_are_not_registered() -> None:
    """Слишком короткие значения замаскировали бы половину текста."""
    masker = Masker()
    masker.add("db", "HOST")

    assert masker.used is False
