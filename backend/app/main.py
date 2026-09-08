import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.router import router as api_v1_router
from app.core.config import settings
from app.core.db import dispose_engine, upgrade_schema
from app.core.gate import access_gate
from app.services.http import ProviderError, ProviderUnavailable
from app.services.providers.cache import purge_expired
from app.services.providers.registry import ProviderRegistry
from app.web.alexa import router as alexa_web_router
from app.web.privacy import router as privacy_router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def _check_auth_config() -> None:
    """Avisa de las combinaciones de config que rompen el login en silencio.

    Los tres casos son de los que se depuran mal, porque no fallan al arrancar:
    fallan en la request siguiente al login, con un 401 que parece un bug de la
    app y no de la configuración.
    """
    if settings.COOKIE_SAMESITE == "none" and not settings.COOKIE_SECURE:
        # El navegador rechaza esta combinación sin decir nada útil.
        log.error(
            "COOKIE_SAMESITE=none exige COOKIE_SECURE=true: el navegador va a "
            "descartar la cookie de sesión y nadie va a poder entrar."
        )
    if "*" in settings.CORS_ORIGINS:
        # Con `allow_credentials=True`, un origen comodín significa que
        # cualquier sitio puede hacer requests autenticadas con la cookie del
        # usuario. Los navegadores lo prohíben, pero el que lo escribió creía
        # otra cosa y conviene decírselo.
        log.error(
            "CORS_ORIGINS tiene '*' junto a credenciales: enumerá los orígenes "
            "reales o ninguna request autenticada del navegador va a funcionar."
        )
    if not settings.COOKIE_SECURE:
        log.warning(
            "COOKIE_SECURE=false: la cookie de sesión viaja sin cifrar. "
            "Correcto en desarrollo sobre http://localhost, nunca en producción."
        )


async def _purge_expired_sessions() -> None:
    """Higiene, no seguridad: una sesión vencida ya no autentica —`expires_at` se
    compara en cada request—, pero sin barrerlas la tabla acumula una fila por
    login para siempre."""
    from app.core.db import get_session_factory
    from app.db.repositories import AuthSessionRepository

    async with get_session_factory()() as session:
        removed = await AuthSessionRepository(session).purge_expired()
        await session.commit()
    if removed:
        log.info("Sesiones vencidas borradas: %d", removed)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Los clientes HTTP viven tanto como la app.

    Crearlos por request tiraría el pool de conexiones en cada llamada, que es
    justo lo que hace lento un fan-out a varios supermercados.

    El esquema se migra antes de aceptar la primera request: es una app de un
    solo usuario y correr `alembic upgrade head` a mano después de cada `git
    pull` es exactamente el paso que uno se olvida.
    """
    await upgrade_schema()
    _check_auth_config()
    # Al arranque y no por tarea periódica: la caché no crece durante la
    # ejecución más de lo que crece el uso, y una app de escritorio se reinicia
    # bastante más seguido que la ventana de retención.
    await purge_expired()
    await _purge_expired_sessions()
    app.state.registry = ProviderRegistry.build()
    try:
        yield
    finally:
        await app.state.registry.aclose()
        await dispose_engine()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
"""El mismo contenido que `frontend/public/robots.txt`.

Duplicado a propósito: si el backend y el frontend terminan en dominios
distintos, el robots.txt del frontend no dice nada sobre el del backend. Un
crawler que llegue a la API pregunta por *este*.
"""


async def _noindex(request: Request, call_next):
    """`X-Robots-Tag: noindex` en todas las respuestas de la API.

    Es la única forma de sacar del índice lo que no tiene `<head>`: `/docs` y
    `/redoc` son HTML pero los genera FastAPI, y las respuestas JSON se pueden
    indexar igual. La cabecera vale para cualquier content-type, así que cubre a
    los tres de una.

    Este proyecto es personal y educativo y se comparte por link directo; que
    aparezca en un buscador no le suma a nadie.
    """
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


# El orden importa y no es el de lectura: `add_middleware` apila hacia afuera,
# así que el **último agregado es el primero** en ver la request. Queda:
#
#     CORS  ->  noindex  ->  puerta de acceso  ->  rutas
#
# La puerta va *adentro* del noindex para que hasta sus 401 salgan con la
# cabecera. Y CORS va afuera de todo para que un preflight `OPTIONS` se conteste
# sin clave: el navegador manda el preflight sin cookies ni headers propios, así
# que si lo atendiera la puerta contestaría 401 y el navegador cancelaría la
# request real antes de mandarla.
app.add_middleware(BaseHTTPMiddleware, dispatch=access_gate)
app.add_middleware(BaseHTTPMiddleware, dispatch=_noindex)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/robots.txt", include_in_schema=False)
async def robots() -> PlainTextResponse:
    return PlainTextResponse(ROBOTS_TXT)


@app.exception_handler(ProviderUnavailable)
async def _provider_unavailable(request: Request, exc: ProviderUnavailable) -> JSONResponse:
    """Red de contención: una cadena caída nunca es un 500 nuestro.

    Los caminos que pueden seguir sin esa cadena ya la sortean antes de llegar
    acá —la búsqueda federada, la caché stale—. Esto es para los que no pueden, y
    existe para que el que quede sin manejar salga como lo que es: un 503 que
    invita a reintentar, y no un "Internal Server Error" que manda a revisar el
    código propio.

    `ProviderRateLimited` hereda de esta y cae acá: para el usuario es lo mismo,
    la cadena no lo está atendiendo. Si dijo cuánto esperar, se le pasa.
    """
    log.warning("%s %s: cadena no disponible: %s", request.method, request.url.path, exc)
    retry_after = getattr(exc, "retry_after", None)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": f"El supermercado no está respondiendo: {exc}"},
        headers={"Retry-After": str(int(retry_after))} if retry_after else None,
    )


@app.exception_handler(ProviderError)
async def _provider_error(request: Request, exc: ProviderError) -> JSONResponse:
    """502: la cadena contestó algo con lo que no se puede trabajar.

    A diferencia del 503, reintentar no lo arregla: o cambiaron su API
    (`ProviderBadRequest`) o nos están respondiendo un challenge de bot
    (`ProviderBlocked`). Se registra en `error` porque casi siempre hay que ir a
    mirarlo, aunque el status señale hacia afuera.
    """
    log.error("%s %s: respuesta inutilizable: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": f"El supermercado respondió algo inesperado: {exc}"},
    )


# Registrar rutas
app.include_router(api_v1_router, prefix=settings.API_V1_STR)

# La política de privacidad va acá y no en la SPA: la abre Amazon durante el
# "Login with Amazon" y no hay garantía de que ejecute JavaScript, así que tiene
# que llegar escrita en la primera respuesta. Antes de `_mount_spa()` a
# propósito: el `mount` de `/` se queda con todo lo que no reclamó una ruta
# anterior, incluido esto.
app.include_router(privacy_router)

# El flujo de "Vincular cuenta de Alexa". También antes del `mount` y por el
# mismo motivo, pero acá no hay margen para elegir la URL: el Redirect URI está
# registrado en el Security Profile de Amazon como `/auth/alexa/callback`, Amazon
# lo compara literal, y moverlo rompería el vínculo de todos los que ya
# vincularon. La ruta manda sobre dónde vive el código.
app.include_router(alexa_web_router)


def _mount_spa() -> bool:
    """Sirve el frontend compilado desde el mismo server que la API.

    Un solo origen es lo que hace que la puerta de acceso valga para todo con una
    sola implementación, y de paso deja la cookie de sesión en `SameSite=lax`
    —que es lo que bloquea CSRF— en lugar de obligar a `none` por tener el
    frontend en otro dominio.

    Se monta último a propósito: Starlette resuelve las rutas en orden, así que
    `/api/v1/...`, `/docs` y `/robots.txt` ya se registraron y ganan. Lo que cae
    acá es lo que ninguna ruta reclamó.

    Si no hay build —el caso normal en desarrollo, donde el frontend lo sirve
    Vite— no se monta nada y la API sigue funcionando sola.
    """
    if not settings.STATIC_DIR:
        return False
    root = Path(settings.STATIC_DIR)
    if not (root / "index.html").is_file():
        log.warning("STATIC_DIR=%s no tiene index.html: no se sirve el frontend", root)
        return False

    # `html=True` hace dos cosas: sirve `index.html` en `/` y contesta con él
    # cuando el path no existe, que es el fallback que necesita una SPA.
    app.mount("/", StaticFiles(directory=root, html=True), name="spa")
    log.info("Frontend servido desde %s", root)
    return True


if not _mount_spa():

    @app.get("/", summary="Ruta principal de bienvenida")
    async def root():
        return {
            "message": f"Bienvenido a {settings.PROJECT_NAME}",
            "docs": "/docs",
            "health": f"{settings.API_V1_STR}/health"
        }
