from redis import Redis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry


def create_redis(url: str) -> Redis:
    # Таймауты и повторы — явно. Значения по умолчанию у redis-py зависят от способа создания
    # клиента и версии: прямой конструктор при отказе соединения повторяет команду ~4 с, а
    # зависший Redis держит запрос 5 с. Здесь «недоступно» становится ясно не дольше чем за 2 с
    return Redis.from_url(
        url,
        socket_connect_timeout=1,
        socket_timeout=1,
        retry=Retry(ExponentialBackoff(cap=0.1, base=0.01), retries=1),
    )
