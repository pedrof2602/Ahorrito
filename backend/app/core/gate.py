"""Puerta de clave compartida delante de todo el deploy.

Esto **no** es el login. El login dice quién sos y protege tus datos; esto dice
si esta URL existe para vos. Son capas distintas y la de acá va primero: sin la
clave no hay ni formulario de login que tantear, ni `/docs` que enumerar, ni
`/search` que usar como proxy gratis contra los supermercados.

Vive en la app y no en el hosting porque la protección por contraseña de Vercel
o Netlify cubre las páginas servidas y deja la API abierta. Acá pasa por el mismo
lugar todo lo que el server contesta.

Con `ACCESS_KEY` vacía la puerta no existe, que es lo que querés en desarrollo.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.config import settings
from app.web.oauth_alexa import OAUTH_ALEXA_PATHS
from app.web.privacy import PRIVACY_PATHS
from app.web.skill import SKILL_PATHS

logger = logging.getLogger(__name__)

EXEMPT_PATHS = (
    frozenset({"/robots.txt", "/api/v1/health"})
    | PRIVACY_PATHS
    | OAUTH_ALEXA_PATHS
    | SKILL_PATHS
)
"""Lo único que se contesta sin clave.

`robots.txt` porque un crawler que no puede leerlo no se entera de que no debe
indexar —y no revela nada que no diga ya su propio contenido—. El health check
porque lo consulta la infraestructura de Fly, que no tiene cómo presentar una
clave, y contestar 401 ahí haría que el deploy se considere caído y se reinicie
en loop.

La política de privacidad porque tiene que ser pública para servir de algo: la
abre Amazon durante el "Login with Amazon", que no tiene cómo presentar la
clave, y la abre cualquiera que quiera saber qué guardamos de él antes de
registrarse. Los paths se importan de donde están definidas las rutas y no se
copian acá: si mañana cambia la URL, la exención la sigue sola.

Los dos de Alexa por el mismo motivo y con el mismo cuidado. `/alexa/skill` lo
llaman los servidores de Amazon, y `/oauth/alexa/*` el navegador embebido de la
app de Alexa: ninguno de los dos es un cliente nuestro al que se le pueda haber
dado la clave alguna vez. **Estar exentos de la puerta no los deja abiertos**:
al skill lo protege la firma de Amazon —una defensa bastante más fuerte que una
clave compartida—, al `authorize` el email y la contraseña, y al `token` el
`client_secret`. La puerta es la capa que decide si una URL existe para vos, y
para estas tres la respuesta tiene que ser sí.

Ninguna de las exenciones revela nada: no listan datos de nadie sin autenticar y
no sirven de proxy contra los supermercados, que es lo que la puerta cuida.
"""


def _authorized(request: Request, key: str) -> bool:
    """Si la request trae la clave, por cookie o por header.

    `compare_digest` y no `==`: comparar strings corta en el primer byte
    distinto, y ese tiempo distinto es medible. Es una defensa barata contra un
    ataque poco probable, pero cuesta una línea.
    """
    presented = request.cookies.get(settings.ACCESS_COOKIE_NAME) or request.headers.get(
        "X-Access-Key", ""
    )
    return bool(presented) and secrets.compare_digest(presented, key)


def _remember(response: Response, key: str) -> None:
    """Guarda la clave en una cookie para que el link haga falta una sola vez.

    `httponly` para que no la lea el JavaScript de la página, y `max_age` de un
    año: es un permiso de acceso al sitio, no una sesión, y hacer que venza cada
    tanto solo obligaría a volver a pasar el link sin proteger de nada.
    """
    response.set_cookie(
        settings.ACCESS_COOKIE_NAME,
        key,
        max_age=365 * 24 * 3600,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        path="/",
    )


async def access_gate(request: Request, call_next):
    """Middleware: nada pasa sin la clave, salvo `EXEMPT_PATHS`.

    Tres formas de presentarla, en orden de cómo se usan:

    1. `?k=<clave>` en la URL. Es el link que compartís: instala la cookie y
       redirige a la misma URL sin el parámetro, así no queda pegado en el
       historial ni se propaga si alguien copia la barra de direcciones.
    2. La cookie, que es como sigue funcionando de ahí en adelante.
    3. El header `X-Access-Key`, para pegarle con `curl` o desde un script.

    El 401 no dice que exista una clave ni cómo se manda. Quien tiene el link no
    lo necesita, y quien no lo tiene no merece el dato.
    """
    key = settings.ACCESS_KEY
    if not key or request.url.path in EXEMPT_PATHS:
        return await call_next(request)

    offered = request.query_params.get(settings.ACCESS_KEY_PARAM)
    if offered is not None and secrets.compare_digest(offered, key):
        # Se redirige en lugar de contestar acá para sacar la clave de la URL: si
        # la página quedara servida en `?k=…`, cada link que el usuario copie y
        # mande a otro lado se lleva la clave puesta.
        clean = request.url.remove_query_params(settings.ACCESS_KEY_PARAM)
        response = RedirectResponse(str(clean), status_code=status.HTTP_303_SEE_OTHER)
        _remember(response, key)
        return response

    if _authorized(request, key):
        return await call_next(request)

    logger.info("Sin clave de acceso: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "No encontrado."},
    )
