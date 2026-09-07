"""Contador de intentos fallidos de login, en memoria del proceso.

Deliberadamente chico. Un rate limiter serio vive en Redis y sobrevive al
reinicio; este no hace ninguna de las dos cosas y alcanza igual para lo único
que tiene que frenar acá: la fuerza bruta contra una contraseña, que necesita
miles de intentos seguidos contra el mismo proceso.

Lo que **no** cubre, y conviene saberlo antes de necesitarlo:

- Con varios workers de uvicorn cada uno lleva su propio contador, así que el
  techo real es `LOGIN_MAX_ATTEMPTS × workers`.
- Un reinicio borra la cuenta. Un atacante que pueda tirarte el proceso puede
  resetear su propio castigo, pero si puede hacer eso ya tenés un problema peor.
- Cuenta por IP. Detrás de un proxy hay que confiar en `X-Forwarded-For`, y
  confiar en ese header sin que el proxy lo reescriba es peor que no limitar:
  cualquiera se inventa una IP por intento. Por eso `client_ip` solo lo lee
  cuando está explícitamente habilitado.
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import Request


class AttemptLimiter:
    """Ventana deslizante de intentos fallidos por clave."""

    def __init__(self, max_attempts: int, window_s: int) -> None:
        self._max = max_attempts
        self._window = window_s
        self._hits: dict[str, list[float]] = defaultdict(list)

    def _prune(self, key: str, now: float) -> list[float]:
        fresh = [t for t in self._hits[key] if now - t < self._window]
        if fresh:
            self._hits[key] = fresh
        else:
            # Sin esto el dict crece para siempre con una entrada por IP que
            # alguna vez falló: es un leak lento pero es un leak.
            self._hits.pop(key, None)
        return fresh

    def is_blocked(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self._max

    def record_failure(self, key: str) -> None:
        self._hits[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        """Un login exitoso borra los fallos previos.

        Si no, quien se equivoca nueve veces, entra bien y vuelve a equivocarse
        una queda bloqueado por errores que ya "pagó" al acertar.
        """
        self._hits.pop(key, None)

    def retry_after_s(self, key: str) -> int:
        """Segundos hasta que se libere el intento más viejo, para el header."""
        hits = self._hits.get(key)
        if not hits:
            return 0
        return max(1, int(self._window - (time.monotonic() - min(hits))))


def client_ip(request: Request, *, trust_forwarded: bool = False) -> str:
    """La IP del cliente.

    `trust_forwarded` está en false por default porque `X-Forwarded-For` lo
    escribe quien llama, y detrás de un deploy sin proxy que lo sobrescriba es
    un campo libre: el atacante manda una IP distinta por intento y el límite no
    limita nada. Se enciende solo cuando hay un reverse proxy adelante que lo
    reescribe (nginx con `proxy_set_header X-Forwarded-For $remote_addr`).
    """
    if trust_forwarded:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "desconocido"
