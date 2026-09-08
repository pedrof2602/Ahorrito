"""La página de política de privacidad.

Lo que se prueba acá no es que el HTML esté lindo, sino las tres cosas que la
harían inútil sin fallar: que llegue servida (y no como un `index.html` vacío
que espera JavaScript), que diga cómo pedir la baja, y que no se haya perdido en
el camino la afirmación más comprometida de todo el texto —que no guardamos
números de tarjeta—, que es la que hay que poder sostener.

Que se conteste sin clave se prueba en `test_access_gate.py`, junto al resto de
la puerta.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

CONTACTO = "pedrofreimeyer@gmail.com"


@pytest_asyncio.fixture
async def anon():
    """Sin login y sin clave de acceso: como la ve Amazon."""
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_llega_como_html_ya_escrito(anon):
    """Sin JavaScript de por medio: el validador de Amazon puede no ejecutarlo."""
    response = await anon.get("/privacidad")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>Política de privacidad</h1>" in response.text
    assert "<script" not in response.text


@pytest.mark.asyncio
async def test_dice_a_quien_escribirle_para_que_borren_tus_datos(anon):
    """Sin una dirección de contacto, el derecho a la baja no existe en la
    práctica."""
    body = (await anon.get("/privacidad")).text

    assert f"mailto:{CONTACTO}" in body
    assert "borre" in body.lower()


@pytest.mark.asyncio
async def test_sostiene_que_no_guardamos_el_numero_de_tarjeta(anon):
    """Es verdad —`PaymentInstrument` no tiene columna para el PAN— y es lo más
    fuerte que afirma la página. Si alguien alguna vez agrega esa columna, que
    este test lo obligue a pasar por acá."""
    body = (await anon.get("/privacidad")).text.lower()

    assert "nunca el número de tu tarjeta" in body


@pytest.mark.asyncio
async def test_tiene_fecha_de_actualizacion(anon):
    body = (await anon.get("/privacidad")).text

    assert "Última actualización:" in body


@pytest.mark.asyncio
async def test_la_version_en_ingles_sirve_la_misma_pagina(anon):
    """Sin redirect: un validador que no los siga tiene que encontrar el texto
    igual."""
    es = await anon.get("/privacidad")
    en = await anon.get("/privacy")

    assert en.status_code == 200
    assert en.text == es.text
