"""El account linking del skill, con esta app como proveedor OAuth.

Lo que más se prueba acá no es que el flujo feliz funcione —eso son tres tests—
sino que **el `redirect_uri` no se pueda mover**. Es a donde va el código, o sea
la cuenta: si se acepta uno que no está en la whitelist, alcanza con hacerle
abrir un link al usuario para quedarse con su vínculo. El resto de los tests
cubren lo mismo desde otros ángulos: código de un solo uso, código que vence,
PKCE que tiene que cerrar, y credenciales de cliente que tienen que estar.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.config import settings
from app.db import tables as t
from app.db.repositories import AlexaSkillTokenRepository
from tests.conftest import TEST_PASSWORD

pytestmark = pytest.mark.asyncio

CLIENT_ID = "alexa-test-client"
CLIENT_SECRET = "un-secreto-de-test-bastante-largo"
REDIRECT = "https://layla.amazon.com/api/skill/link/TEST123"
OTRO_REDIRECT = "https://pitangui.amazon.com/api/skill/link/TEST123"

VERIFIER = "un-code-verifier-de-al-menos-43-caracteres-como-pide-pkce"
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .rstrip(b"=")
    .decode()
)


@pytest_asyncio.fixture(autouse=True)
async def linking_configurado(monkeypatch):
    monkeypatch.setattr(settings, "ALEXA_LINK_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(settings, "ALEXA_LINK_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(
        settings, "ALEXA_LINK_REDIRECT_URIS", [REDIRECT, OTRO_REDIRECT]
    )


def authorize_params(**overrides) -> dict[str, str]:
    return {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "state": "estado-de-amazon",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        **overrides,
    }


async def obtener_codigo(client, **overrides) -> str:
    """Hace el login del formulario y devuelve el `code` que volvió en la URL."""
    response = await client.post(
        "/oauth/alexa/authorize",
        data={
            "email": "test@ejemplo.com",
            "password": TEST_PASSWORD,
            **authorize_params(**overrides),
        },
    )
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    return location.split("code=")[1].split("&")[0]


async def canjear(client, code: str, **overrides):
    return await client.post(
        "/oauth/alexa/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            **overrides,
        },
    )


# ------------------------------------------------------------------ authorize


async def test_authorize_muestra_el_login(client):
    response = await client.get("/oauth/alexa/authorize", params=authorize_params())

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # El estado del flujo tiene que volver en el formulario o el POST no sabe a
    # dónde redirigir.
    assert REDIRECT in response.text
    assert "estado-de-amazon" in response.text
    assert CHALLENGE in response.text


async def test_authorize_con_redirect_ajeno_no_redirige(client):
    """El test central de este archivo.

    Un `redirect_uri` que no está en la whitelist no puede producir un redirect
    **ni siquiera para informar el error**: el único destino disponible sería el
    del atacante.
    """
    response = await client.get(
        "/oauth/alexa/authorize",
        params=authorize_params(redirect_uri="https://malicioso.example/roba"),
    )

    assert response.status_code == 400
    assert "location" not in response.headers
    assert "malicioso.example" not in response.text


async def test_authorize_con_client_id_ajeno_no_muestra_el_login(client):
    response = await client.get(
        "/oauth/alexa/authorize", params=authorize_params(client_id="otro")
    )

    assert response.status_code == 400
    assert "password" not in response.text


async def test_authorize_rechaza_pkce_plain(client):
    """`plain` manda el verifier en claro, así que no protege de nada."""
    response = await client.get(
        "/oauth/alexa/authorize",
        params=authorize_params(code_challenge_method="plain"),
    )

    assert response.status_code == 400


async def test_state_no_puede_inyectar_html(client):
    """El `state` sale de quien arma la URL y termina en un atributo HTML.

    Es la página donde el usuario tipea la contraseña: un `<script>` acá es el
    peor XSS posible del proyecto.
    """
    response = await client.get(
        "/oauth/alexa/authorize",
        params=authorize_params(state='"><script>alert(1)</script>'),
    )

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


# ------------------------------------------------------------------- el login


async def test_login_correcto_devuelve_el_codigo(client, session, user):
    response = await client.post(
        "/oauth/alexa/authorize",
        data={
            "email": user.email,
            "password": TEST_PASSWORD,
            **authorize_params(),
        },
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(REDIRECT + "?")
    assert "state=estado-de-amazon" in location

    row = await session.scalar(select(t.AlexaSkillCode))
    assert row is not None
    assert row.user_id == user.id
    # En la base queda el hash, no el código que viajó en la URL.
    assert row.code_hash not in location


async def test_login_incorrecto_no_emite_codigo(client, session, user):
    response = await client.post(
        "/oauth/alexa/authorize",
        data={
            "email": user.email,
            "password": "la que no es",
            **authorize_params(),
        },
    )

    assert response.status_code == 200
    assert "location" not in response.headers
    assert "incorrectos" in response.text
    assert await session.scalar(select(t.AlexaSkillCode)) is None


async def test_el_post_revalida_el_redirect(client, session, user):
    """El `redirect_uri` del formulario lo manda el navegador, no nosotros.

    Validarlo solo en el GET dejaría el agujero entero: alcanza con editar el
    campo oculto antes de enviar.
    """
    response = await client.post(
        "/oauth/alexa/authorize",
        data={
            "email": user.email,
            "password": TEST_PASSWORD,
            **authorize_params(redirect_uri="https://malicioso.example/roba"),
        },
    )

    assert response.status_code == 400
    assert "location" not in response.headers
    assert await session.scalar(select(t.AlexaSkillCode)) is None


# ------------------------------------------------------------------- el canje


async def test_canje_devuelve_los_tokens(client, session, user):
    code = await obtener_codigo(client)
    response = await canjear(client, code)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == settings.ALEXA_TOKEN_TTL_S
    assert body["access_token"] and body["refresh_token"]
    assert body["access_token"] != body["refresh_token"]
    # Sin esto un proxy podría servirle el token de uno al siguiente.
    assert response.headers["cache-control"] == "no-store"

    row = await AlexaSkillTokenRepository(session).by_access_token(body["access_token"])
    assert row is not None
    assert row.user_id == user.id


async def test_lo_guardado_es_el_hash_y_no_el_token(client, session):
    code = await obtener_codigo(client)
    body = (await canjear(client, code)).json()

    row = await session.scalar(select(t.AlexaSkillToken))
    assert row is not None
    assert row.access_token_hash != body["access_token"]
    assert row.refresh_token_hash != body["refresh_token"]


async def test_el_codigo_es_de_un_solo_uso(client):
    code = await obtener_codigo(client)

    assert (await canjear(client, code)).status_code == 200
    segundo = await canjear(client, code)

    assert segundo.status_code == 400
    assert segundo.json()["error"] == "invalid_grant"


async def test_el_codigo_se_quema_aunque_el_canje_falle(client):
    """Un código presentado con un `code_verifier` equivocado no vuelve al pozo.

    Si volviera, el atacante que interceptó el código podría probar verifiers
    hasta acertar.
    """
    code = await obtener_codigo(client)

    fallido = await canjear(client, code, code_verifier="cualquier-otra-cosa")
    assert fallido.status_code == 400

    reintento = await canjear(client, code)
    assert reintento.status_code == 400
    assert reintento.json()["error"] == "invalid_grant"


async def test_codigo_vencido_no_se_canjea(client, session):
    code = await obtener_codigo(client)

    row = await session.scalar(select(t.AlexaSkillCode))
    row.expires_at = t.utcnow() - timedelta(seconds=1)
    await session.commit()

    response = await canjear(client, code)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_canje_con_redirect_distinto_al_del_authorize(client):
    """OAuth exige que coincida, y no es ceremonia: es lo que impide canjear en
    otro destino un código emitido para éste."""
    code = await obtener_codigo(client)

    response = await canjear(client, code, redirect_uri=OTRO_REDIRECT)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_pkce_tiene_que_cerrar(client):
    code = await obtener_codigo(client)

    response = await canjear(client, code, code_verifier="no-es-el-verifier")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_pkce_faltante_no_pasa(client):
    code = await obtener_codigo(client)

    response = await canjear(client, code, code_verifier="")
    assert response.status_code == 400


async def test_secreto_de_cliente_incorrecto(client):
    code = await obtener_codigo(client)

    response = await canjear(client, code, client_secret="no-es")
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


async def test_credenciales_por_basic_auth(client):
    """Amazon manda las credenciales en el header o en el cuerpo según cómo esté
    configurado el skill. Hay que aceptar las dos."""
    code = await obtener_codigo(client)
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

    response = await client.post(
        "/oauth/alexa/token",
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
        },
    )

    assert response.status_code == 200, response.text


async def test_grant_no_soportado(client):
    response = await client.post(
        "/oauth/alexa/token",
        data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_grant_type"


# ---------------------------------------------------------------- el refresco


async def test_refresh_rota_los_dos_tokens(client, session):
    code = await obtener_codigo(client)
    primero = (await canjear(client, code)).json()

    response = await client.post(
        "/oauth/alexa/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": primero["refresh_token"],
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )

    assert response.status_code == 200, response.text
    segundo = response.json()
    assert segundo["access_token"] != primero["access_token"]
    # El refresh también rota: uno robado deja de servir en cuanto el legítimo
    # se usa una vez.
    assert segundo["refresh_token"] != primero["refresh_token"]

    tokens = AlexaSkillTokenRepository(session)
    assert await tokens.by_access_token(primero["access_token"]) is None
    assert await tokens.by_access_token(segundo["access_token"]) is not None


async def test_refresh_de_un_vinculo_borrado(client, session, user):
    code = await obtener_codigo(client)
    primero = (await canjear(client, code)).json()

    await AlexaSkillTokenRepository(session).delete_for_user(user.id)
    await session.commit()

    response = await client.post(
        "/oauth/alexa/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": primero["refresh_token"],
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )

    # `invalid_grant` y no `invalid_client`: es lo que hace que Amazon deje de
    # reintentar y le pida al usuario que vincule de nuevo.
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


async def test_sin_configurar_no_hay_vinculacion(client, monkeypatch):
    monkeypatch.setattr(settings, "ALEXA_LINK_CLIENT_ID", "")

    assert (
        await client.get("/oauth/alexa/authorize", params=authorize_params())
    ).status_code == 400
    assert (await canjear(client, "lo-que-sea")).status_code == 401


async def test_vincular_dos_veces_no_pisa_el_vinculo_anterior(client, session, user):
    """Un usuario puede tener el skill vinculado desde más de una casa."""
    primero = (await canjear(client, await obtener_codigo(client))).json()
    segundo = (await canjear(client, await obtener_codigo(client))).json()

    tokens = AlexaSkillTokenRepository(session)
    assert await tokens.by_access_token(primero["access_token"]) is not None
    assert await tokens.by_access_token(segundo["access_token"]) is not None
    assert len(await tokens.for_user(user.id)) == 2
