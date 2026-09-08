"""El vínculo con Alexa: flujo OAuth, cifrado y refresco.

El test que más importa de este archivo es `test_callback_con_state_ajeno_no_canjea`.
Todo lo demás verifica que la función anda; ese verifica que no se pueda usar
para atar la cuenta de un usuario a la cuenta de Amazon de otro.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from app.core.config import settings
from app.db import tables as t
from app.db.repositories import AlexaLinkRepository
from app.services.alexa import lwa

pytestmark = pytest.mark.asyncio

# Una clave Fernet fija para los tests. No protege nada —está en un archivo
# versionado— y ese es justo el punto: que sea obvio que no es la de producción.
TEST_KEY = "JrpKlwD45WFDUylVCg5NyH5i-diqqtcD9s9pkCuci1Q="
OTRA_KEY = "kG3l2H2bryXh6BVh2McEn3vWK4QpcVAvF2BNdTvvyUo="

ACCESS = "Atza|TEST-ACCESS-TOKEN"
REFRESH = "Atzr|TEST-REFRESH-TOKEN"


@pytest.fixture(autouse=True)
def lwa_configurado(monkeypatch):
    """LWA encendido para todos los tests del archivo.

    `autouse` porque sin esto cada endpoint contestaría "sin_config" y los tests
    pasarían sin probar nada — el modo de fallo más caro de un test de seguridad.
    """
    monkeypatch.setattr(settings, "LWA_CLIENT_ID", "amzn1.application-oa2-client.test")
    monkeypatch.setattr(settings, "LWA_CLIENT_SECRET", "secreto-de-test")
    monkeypatch.setattr(
        settings, "LWA_REDIRECT_URI", "https://ahorrito.fly.dev/auth/alexa/callback"
    )
    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", TEST_KEY)

    # `_fernet()` está cacheado con `lru_cache`: sin limpiarlo, el primer test
    # que corra fija la clave para todos los demás del proceso.
    from app.core import crypto

    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


def token_response(*, access=ACCESS, refresh=REFRESH, expires_in=3600):
    return httpx.Response(
        200,
        json={
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in": expires_in,
            "scope": " ".join(lwa.SCOPES),
        },
    )


async def start_login(client) -> str:
    """Hace el `GET /auth/alexa/login` y devuelve el `state` que quedó puesto."""
    response = await client.get("/auth/alexa/login", follow_redirects=False)
    assert response.status_code == 307
    return client.cookies["alexa_oauth_state"]


# ------------------------------------------------------------------- el arranque


async def test_login_redirige_a_amazon_con_los_scopes(client):
    response = await client.get("/auth/alexa/login", follow_redirects=False)

    assert response.status_code == 307
    destino = httpx.URL(response.headers["location"])
    assert str(destino).startswith(lwa.AUTHORIZE_URL)

    params = destino.params
    assert params["client_id"] == settings.LWA_CLIENT_ID
    assert params["response_type"] == "code"
    # Carácter por carácter: es lo que Amazon compara contra el Security Profile.
    assert params["redirect_uri"] == settings.LWA_REDIRECT_URI
    assert set(params["scope"].split()) == set(lwa.SCOPES)

    # El `state` que viajó en la URL es el que quedó en la cookie: sin esa
    # correspondencia el control del callback no verifica nada.
    assert params["state"] == client.cookies["alexa_oauth_state"]


async def test_login_sin_sesion_vuelve_a_la_app(client):
    """Sin sesión no hay a quién vincular, y el usuario ve la app, no un 401 JSON."""
    client.cookies.delete(settings.COOKIE_NAME)

    response = await client.get("/auth/alexa/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/?alexa=sesion"


async def test_login_sin_configurar_no_manda_a_amazon(client, monkeypatch):
    monkeypatch.setattr(settings, "LWA_CLIENT_ID", "")

    response = await client.get("/auth/alexa/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/?alexa=sin_config"


# -------------------------------------------------------------------- el callback


@respx.mock
async def test_callback_guarda_el_vinculo_cifrado(client, session, user):
    state = await start_login(client)
    ruta = respx.post(lwa.TOKEN_URL).mock(return_value=token_response())

    response = await client.get(
        "/auth/alexa/callback",
        params={"code": "codigo-de-un-solo-uso", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?alexa=ok"
    assert ruta.called

    # El canje mandó el mismo `redirect_uri` que el arranque, que es lo que ata
    # el código a esta app.
    enviado = dict(httpx.QueryParams(ruta.calls.last.request.content.decode()))
    assert enviado["grant_type"] == "authorization_code"
    assert enviado["redirect_uri"] == settings.LWA_REDIRECT_URI

    row = await AlexaLinkRepository(session, user.id).get()
    assert row is not None
    # Lo que importa: en la base no está el token, está el ciphertext.
    assert ACCESS not in row.access_token_enc
    assert REFRESH not in row.refresh_token_enc
    assert AlexaLinkRepository(session, user.id).tokens(row) == (ACCESS, REFRESH)


@respx.mock
async def test_callback_con_state_ajeno_no_canjea(client, session, user):
    """El control que sostiene todo el flujo.

    Sin él, un atacante consigue un `code` con su propia cuenta de Amazon, hace
    que el navegador de un usuario logueado visite el callback con ese código, y
    el usuario queda vinculado a la cuenta de Amazon del atacante. Acá se
    verifica lo único que lo impide: que un `state` que no salió de esta sesión
    no llega ni a hablar con Amazon.
    """
    await start_login(client)
    ruta = respx.post(lwa.TOKEN_URL).mock(return_value=token_response())

    response = await client.get(
        "/auth/alexa/callback",
        params={"code": "codigo-del-atacante", "state": "state-inventado"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?alexa=error"
    assert not ruta.called, "se canjeó un código con un `state` que no coincide"
    assert await AlexaLinkRepository(session, user.id).get() is None


@respx.mock
async def test_callback_sin_cookie_de_state_no_canjea(client, session, user):
    """La otra mitad del mismo control: sin cookie no hay nada contra qué
    comparar, y "no hay nada" no puede significar "pasa"."""
    ruta = respx.post(lwa.TOKEN_URL).mock(return_value=token_response())

    response = await client.get(
        "/auth/alexa/callback",
        params={"code": "codigo", "state": "cualquiera"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/?alexa=error"
    assert not ruta.called
    assert await AlexaLinkRepository(session, user.id).get() is None


@respx.mock
async def test_callback_sin_sesion_descarta_el_codigo(client, session, user):
    """La sesión venció durante el paseo por Amazon: no hay a qué usuario atar
    el token, así que el código se tira sin gastarlo."""
    state = await start_login(client)
    client.cookies.delete(settings.COOKIE_NAME)
    ruta = respx.post(lwa.TOKEN_URL).mock(return_value=token_response())

    response = await client.get(
        "/auth/alexa/callback",
        params={"code": "codigo", "state": state},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/?alexa=sesion"
    assert not ruta.called
    assert await AlexaLinkRepository(session, user.id).get() is None


@respx.mock
async def test_callback_cancelado_no_es_un_error(client, session, user):
    """El usuario apretó "Cancelar" en Amazon. Es una respuesta, no un fallo."""
    state = await start_login(client)
    ruta = respx.post(lwa.TOKEN_URL).mock(return_value=token_response())

    response = await client.get(
        "/auth/alexa/callback",
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/?alexa=cancelado"
    assert not ruta.called
    assert await AlexaLinkRepository(session, user.id).get() is None


@respx.mock
async def test_vincular_dos_veces_pisa_la_fila(client, session, user):
    """Volver a vincular es normal —corregir con qué cuenta quedaste atado— y el
    índice único sobre `user_id` obliga a que sea un UPDATE."""
    for access in ("Atza|PRIMERO", "Atza|SEGUNDO"):
        state = await start_login(client)
        respx.post(lwa.TOKEN_URL).mock(return_value=token_response(access=access))
        await client.get(
            "/auth/alexa/callback",
            params={"code": "codigo", "state": state},
            follow_redirects=False,
        )

    links = AlexaLinkRepository(session, user.id)
    row = await links.get()
    assert links.tokens(row)[0] == "Atza|SEGUNDO"


# ------------------------------------------------------------------- el refresco


async def guardar_vinculo(session, user_id, *, expires_in: int) -> None:
    await AlexaLinkRepository(session, user_id).upsert(
        access_token=ACCESS,
        refresh_token=REFRESH,
        expires_at=t.utcnow() + timedelta(seconds=expires_in),
        scope=" ".join(lwa.SCOPES),
    )
    await session.commit()


@respx.mock
async def test_access_token_vigente_no_llama_a_amazon(session, user):
    await guardar_vinculo(session, user.id, expires_in=3600)
    ruta = respx.post(lwa.TOKEN_URL).mock(return_value=token_response())

    assert await lwa.access_token(session, user.id) == ACCESS
    assert not ruta.called


@respx.mock
async def test_access_token_vencido_se_refresca_y_se_guarda(session, user):
    await guardar_vinculo(session, user.id, expires_in=-10)
    nuevo = "Atza|TOKEN-REFRESCADO"
    ruta = respx.post(lwa.TOKEN_URL).mock(return_value=token_response(access=nuevo))

    assert await lwa.access_token(session, user.id) == nuevo
    assert dict(httpx.QueryParams(ruta.calls.last.request.content.decode()))[
        "grant_type"
    ] == "refresh_token"

    # Guardado, no solo devuelto: si no, cada llamada gastaría un refresco.
    links = AlexaLinkRepository(session, user.id)
    row = await links.get()
    assert links.tokens(row)[0] == nuevo
    assert row.refreshed_at is not None


@respx.mock
async def test_access_token_por_vencer_se_refresca(session, user):
    """Dentro del margen: un token que vence en 10 segundos llegaría vencido a
    Amazon, y el error aparecería lejos de acá como un 401 confuso."""
    await guardar_vinculo(session, user.id, expires_in=lwa.REFRESH_SKEW_S - 10)
    ruta = respx.post(lwa.TOKEN_URL).mock(return_value=token_response(access="Atza|X"))

    assert await lwa.access_token(session, user.id) == "Atza|X"
    assert ruta.called


@respx.mock
async def test_revocado_desde_amazon_borra_el_vinculo(session, user):
    """La promesa de la política de privacidad: si el usuario desvincula desde su
    cuenta de Amazon, acá no queda una credencial muerta."""
    await guardar_vinculo(session, user.id, expires_in=-10)
    respx.post(lwa.TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )

    with pytest.raises(lwa.AlexaLinkRevoked):
        await lwa.access_token(session, user.id)

    assert await AlexaLinkRepository(session, user.id).get() is None


@respx.mock
async def test_amazon_caido_no_borra_el_vinculo(session, user):
    """Un 503 no es una revocación: el vínculo sigue valiendo y hay que poder
    reintentar sin obligar al usuario a vincular de nuevo."""
    await guardar_vinculo(session, user.id, expires_in=-10)
    respx.post(lwa.TOKEN_URL).mock(return_value=httpx.Response(503, json={}))

    with pytest.raises(lwa.AlexaUnavailable):
        await lwa.access_token(session, user.id)

    assert await AlexaLinkRepository(session, user.id).get() is not None


async def test_access_token_sin_vinculo(session, user):
    with pytest.raises(lwa.AlexaNotLinked):
        await lwa.access_token(session, user.id)


# ------------------------------------------------------------------ el estado

# Los tests de `GET /api/v1/alexa/status` y `DELETE /api/v1/alexa/link` NO están
# acá: esos endpoints dejaron de mirar `alexa_links` y ahora informan el vínculo
# del skill, que vive en `alexa_skill_tokens`. Están en `test_alexa_skill.py`.
#
# Lo que sigue en este archivo prueba el flujo de Login with Amazon, que quedó
# sin uso cuando Amazon apagó la List Management REST API el 1 de julio de 2024.
# Se borra junto con `services/alexa/lwa.py`.


async def test_status_pide_sesion(client):
    client.cookies.delete(settings.COOKIE_NAME)
    assert (await client.get("/api/v1/alexa/status")).status_code == 401


# ------------------------------------------------------------------- el cifrado


async def test_lo_guardado_no_se_lee_con_otra_clave(session, user, monkeypatch):
    """Cambiar `TOKEN_ENCRYPTION_KEY` vuelve ilegibles los vínculos.

    Está documentado en el setting y se verifica acá: es la consecuencia de rotar
    la clave, y conviene que sea un test y no una nota de pie que alguien lea
    después de haberla rotado.
    """
    from app.core import crypto

    await guardar_vinculo(session, user.id, expires_in=3600)
    row = await AlexaLinkRepository(session, user.id).get()

    monkeypatch.setattr(settings, "TOKEN_ENCRYPTION_KEY", OTRA_KEY)
    crypto._fernet.cache_clear()

    with pytest.raises(crypto.DecryptionFailed):
        AlexaLinkRepository(session, user.id).tokens(row)
