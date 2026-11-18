"""Redis: cache, locks distribuídos e filas RQ."""
from __future__ import annotations

import json
from typing import Any

import redis
from rq import Queue

from app.core.config import settings

# Duas conexões de propósito diferente sobre o mesmo Redis:
#
#   redis_client — decode_responses=True. É o nosso cache e os contadores:
#                  queremos str de volta, não bytes.
#   rq_redis     — SEM decode_responses. O RQ serializa os jobs com pickle
#                  (binário); com decodificação automática o worker morre
#                  com UnicodeDecodeError ao ler a fila.
redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
rq_redis = redis.Redis.from_url(settings.redis_url, decode_responses=False)

# Filas por prioridade. O worker consome nesta ordem.
queue_high = Queue("high", connection=rq_redis, default_timeout=1800)
queue_default = Queue("default", connection=rq_redis, default_timeout=3600)
queue_low = Queue("low", connection=rq_redis, default_timeout=7200)

# E4.6 — duas filas para dois workers: um job longo de produção nunca atrasa uma
# publicação, uma aprovação ou um email.
#   content — pesquisa, redação, revisão e tradução (minutos a dezenas de minutos)
#   fast    — publicação, aprovações, emails, tarefas de segundos
queue_content = Queue("content", connection=rq_redis, default_timeout=7200)
queue_fast = Queue("fast", connection=rq_redis, default_timeout=600)

QUEUES = {"high": queue_high, "default": queue_default, "low": queue_low,
          "content": queue_content, "fast": queue_fast}


def cache_get(key: str) -> Any | None:
    raw = redis_client.get(key)
    return json.loads(raw) if raw else None


def cache_set(key: str, value: Any, ttl: int = 3600) -> None:
    redis_client.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=ttl)


def acquire_lock(name: str, ttl: int = 3600) -> bool:
    """Lock simples para impedir que a mesma fase rode duas vezes em paralelo
    (o n8n e o scheduler interno podem disparar o mesmo job)."""
    return bool(redis_client.set(f"lock:{name}", "1", nx=True, ex=ttl))


def release_lock(name: str) -> None:
    redis_client.delete(f"lock:{name}")


# Renova só se o lock ainda for de quem o pede; liberta idem (compare-and-set em Lua).
_RENEW = "if redis.call('get', KEYS[1]) == ARGV[1] then " \
         "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then " \
           "return redis.call('del', KEYS[1]) else return 0 end"


class ShortLock:
    """Lock com TTL curto e renovação (E4.6).

    Um TTL longo deixava a fase presa durante uma hora se o processo morresse.
    Aqui o TTL é de minutos e quem trabalha chama `renew()` periodicamente; se
    morrer, o lock cai sozinho ao fim do TTL. O dono é identificado por um token,
    por isso ninguém liberta nem renova o lock de outro.
    """

    def __init__(self, name: str, ttl: int = 120) -> None:
        import secrets

        self.key = f"lock:{name}"
        self.ttl = ttl
        self.token = secrets.token_hex(8)

    def acquire(self) -> bool:
        return bool(redis_client.set(self.key, self.token, nx=True, ex=self.ttl))

    def renew(self) -> bool:
        return bool(redis_client.eval(_RENEW, 1, self.key, self.token, self.ttl))

    def release(self) -> bool:
        return bool(redis_client.eval(_RELEASE, 1, self.key, self.token))


def incr_daily(counter: str, amount: int = 1) -> int:
    """Contador diário — usado nas cotas (artigos/dia)."""
    from datetime import date

    key = f"counter:{counter}:{date.today().isoformat()}"
    value = redis_client.incrby(key, amount)
    redis_client.expire(key, 172800)
    return int(value)


def get_daily(counter: str) -> int:
    from datetime import date

    return int(redis_client.get(f"counter:{counter}:{date.today().isoformat()}") or 0)
