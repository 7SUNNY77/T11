"""Инфраструктурные ошибки. Прогон, прерванный такой ошибкой, НЕ считается завершённым
и будет перезапущен при следующем запуске раннера (в отличие от ошибок модели)."""

from __future__ import annotations


class InfraError(Exception):
    pass


class RetryableLLMError(InfraError):
    """429 / 5xx / сетевые ошибки — повторяем с экспоненциальной паузой."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class DailyQuotaExceeded(InfraError):
    """Исчерпан суточный лимит провайдера: раннер останавливается, продолжаем на следующий день."""


class RetriesExhausted(InfraError):
    pass


class LLMRequestError(InfraError):
    """Неповторяемая ошибка запроса (400/401/403/404...): обычно ошибка конфигурации."""
