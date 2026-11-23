from redis import __version__ as redis_version
from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.driver_info import DriverInfo

from app.settings import settings


def create_redis(url: str) -> Redis:
    # Таймауты и повторы — явно. Значения по умолчанию у redis-py зависят от способа создания
    # клиента и версии: прямой конструктор при отказе соединения повторяет команду ~4 с, а
    # зависший Redis держит запрос 5 с. Здесь «недоступно» становится ясно не дольше чем за 2 с
    pool = BlockingConnectionPool.from_url(
        url,
        # Все соединения заняты — команда ждёт свободное до 1 с. Обычный пул сразу отвечает
        # ошибкой: при всплеске запросов пароли получали бы 503, а загрузки уходили бы на
        # запасной лимит в памяти
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        timeout=1,
        socket_connect_timeout=1,
        socket_timeout=1,
        retry=Retry(ExponentialBackoff(cap=0.1, base=0.01), retries=1),
        # Версию передаём сами: иначе redis-py ищет её в метаданных пакетов на каждое новое
        # соединение — читает диск прямо в цикле событий
        driver_info=DriverInfo(lib_version=redis_version),
    )
    return Redis.from_pool(pool)
