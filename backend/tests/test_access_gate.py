"""La puerta de clave compartida delante del deploy.

Lo que se prueba acá no es el login —eso vive en `test_auth.py`— sino la capa de
antes: si esta URL existe o no para quien la pide. Los dos extremos importan por
igual: que sin clave no pase nada, y que con clave el link ande de una sola vez.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from app.core.config import settings

KEY = "una-clave-compartida-larga"


@pytest_asyncio.fixture
async def gated(monkeypatch):
    """La app con la puerta activada y sin sesión de usuario.

    Sin el `client` de `conftest` a propósito: ese hace login, y lo que se prueba
    acá es justamente lo que pasa *antes* de poder loguearse.
    """
    monkeypatch.setattr(settings, "ACCESS_KEY", KEY)
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --- nada pasa sin la clave -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/docs",
        "/redoc",
        "/api/v1/openapi.json",
        "/api/v1/search?q=leche",
        "/api/v1/auth/login",
    ],
)
async def test_sin_clave_no_se_contesta_nada(gated, path):
    """Incluido `/auth/login`: sin clave no hay ni formulario que tantear."""
    assert (await gated.get(path)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": "/?k=incorrecta"},
        {"url": "/?k="},
        {"url": "/", "headers": {"X-Access-Key": "incorrecta"}},
        {"url": "/", "headers": {"Cookie": "compras_access=incorrecta"}},
        {"url": f"/?k={KEY[:10]}"},
    ],
    ids=["query-mala", "query-vacía", "header-malo", "cookie-mala", "prefijo"],
)
async def test_una_clave_parecida_no_alcanza(gated, kwargs):
    assert (await gated.get(**kwargs)).status_code == 401


@pytest.mark.asyncio
async def test_el_401_no_cuenta_que_hay_una_clave(gated):
    """No dice que exista una puerta ni cómo se abre.

    Quien tiene el link no necesita el dato, y a quien no lo tiene no hay por qué
    darle el próximo paso.
    """
    body = (await gated.get("/")).json()
    assert body == {"detail": "No encontrado."}


# --- lo que sí pasa ---------------------------------------------------------


@pytest.mark.asyncio
async def test_el_health_check_no_pide_clave(gated):
    """Lo consulta Fly, que no tiene cómo presentarla. Un 401 acá haría que el
    deploy se considere caído y se reinicie en loop."""
    assert (await gated.get("/api/v1/health")).status_code == 200


@pytest.mark.asyncio
async def test_robots_no_pide_clave(gated):
    """Un crawler que no puede leerlo no se entera de que no debe indexar."""
    response = await gated.get("/robots.txt")
    assert response.status_code == 200
    assert "Disallow: /" in response.text


# --- el link que se comparte ------------------------------------------------


@pytest.mark.asyncio
async def test_el_link_con_clave_deja_la_cookie_y_saca_la_clave_de_la_url(gated):
    """La clave no puede quedar en la URL servida.

    Si quedara, cada link que el usuario copie de la barra y mande a otro lado se
    lleva la clave puesta.
    """
    response = await gated.get(f"/?k={KEY}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "http://test/"
    assert settings.ACCESS_COOKIE_NAME in response.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_despues_del_link_ya_no_hace_falta_la_clave(gated):
    await gated.get(f"/?k={KEY}")

    assert (await gated.get("/docs")).status_code == 200
    assert (await gated.get("/api/v1/health")).status_code == 200


@pytest.mark.asyncio
async def test_la_cookie_no_la_puede_leer_el_javascript(gated):
    """`HttpOnly`: un XSS en la página no se lleva la llave del deploy."""
    response = await gated.get(f"/?k={KEY}", follow_redirects=False)
    assert "httponly" in response.headers["set-cookie"].lower()


@pytest.mark.asyncio
async def test_el_header_sirve_para_curl(gated):
    """Para pegarle desde un script sin pasar por el redirect."""
    response = await gated.get("/docs", headers={"X-Access-Key": KEY})
    assert response.status_code == 200


# --- apagada ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_sin_ACCESS_KEY_la_puerta_no_existe(monkeypatch):
    """El default en desarrollo: nadie tiene que configurar nada para trabajar."""
    monkeypatch.setattr(settings, "ACCESS_KEY", "")

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/docs")).status_code == 200


# --- convive con el login ---------------------------------------------------


@pytest.mark.asyncio
async def test_la_clave_abre_la_puerta_pero_no_te_loguea(gated):
    """Son dos capas, no una.

    Pasar la puerta no da acceso a los datos de nadie: la API sigue pidiendo
    sesión. Confundirlas sería convertir una clave compartida por link en la
    llave de las cuentas.
    """
    await gated.get(f"/?k={KEY}")

    assert (await gated.get("/api/v1/search?q=leche")).status_code == 401
