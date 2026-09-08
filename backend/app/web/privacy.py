"""La política de privacidad, servida como HTML plano y sin clave.

Tres decisiones que parecen detalles y no lo son:

* **La sirve el backend, no la SPA.** Amazon la abre durante el flujo de "Login
  with Amazon" y no hay garantía de que ejecute JavaScript. Una ruta de React
  llega como un `index.html` vacío que se llena después de montar; esto llega ya
  escrito en la primera respuesta.
* **Se registra antes del `mount` del frontend.** Starlette resuelve en orden y
  el `StaticFiles(html=True)` de `/` se queda con todo lo que nadie reclamó
  antes, así que registrarla después la volvería inalcanzable.
* **Los paths viven acá y `core.gate` los importa.** La página no sirve de nada
  si la puerta de acceso la tapa, y una lista de exenciones copiada en el otro
  archivo se desincroniza el día que alguien renombre la ruta. Que la excepción
  salga del mismo lugar que la ruta hace que no puedan separarse.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

PRIVACY_PATHS = frozenset({"/privacidad", "/privacidad/", "/privacy", "/privacy/"})
"""Las URLs que contestan esta página, y que la puerta de acceso deja pasar.

Dos idiomas porque el sitio está en español pero la URL se la damos a Amazon, y
un `/privacy` que da 404 es el tipo de fricción que aparece en una revisión de
la skill y no antes. Ninguna redirige a la otra: las dos contestan 200 con el
mismo HTML, porque un validador automático que no siga redirects es más probable
que uno que no entienda un 200.

Con y sin barra final porque este set se compara contra el path *crudo*, antes
de que FastAPI normalice: `/privacidad/` nunca llegaría a la ruta que lo redirige
si la puerta lo cortara con un 401 primero.
"""

_PAGE = (Path(__file__).with_name("privacy.html")).read_text(encoding="utf-8")
"""Leída una vez al importar, no por request.

Es un archivo que cambia cuando lo editás vos y no cuando corre la app; releerlo
en cada request sería un `open()` por visita para servir siempre lo mismo.

**En desarrollo eso significa reiniciar el server para ver un cambio del HTML.**
No alcanza con `--reload`: uvicorn vigila los `.py` y este archivo no lo es. Si
te está por volver loco, `--reload-include '*.html'`.
"""


@router.get("/privacidad", include_in_schema=False)
@router.get("/privacy", include_in_schema=False)
async def privacy() -> HTMLResponse:
    return HTMLResponse(_PAGE)
