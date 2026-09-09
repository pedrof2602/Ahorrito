"""La lista manejada desde afuera con un token personal (el Atajo de Siri).

Dos cosas se prueban con más insistencia que el resto, porque son las que
convierten un token pegado en un atajo del iPhone en algo con lo que se puede
convivir:

* **El token alcanza para la lista y para nada más.** Es un secreto que vive en
  texto plano dentro de un atajo, se sincroniza por iCloud y se comparte sin
  querer al compartir el atajo. Que no sirva para ver domicilios ni para emitir
  otro token es lo que hace que perderlo sea un incordio y no una emergencia.
* **La puerta de acceso (`ACCESS_KEY`) no lo tapa.** Un atajo no puede presentar
  la clave del sitio, y sin la exención el síntoma sería un 401 mudo del que no
  queda rastro en el teléfono.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.db.repositories import ApiTokenRepository, ShoppingListRepository

pytestmark = pytest.mark.asyncio


@pytest.fixture
def auth():
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    return _auth


async def emitir(client, name: str = "iPhone") -> str:
    """Un token nuevo, por el mismo camino que usa la pantalla de configuración."""
    response = await client.post("/api/v1/voz/tokens", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["token"]


def dicho(response) -> str:
    return response.json()["dicho"]


# --------------------------------------------------------- emitir y revocar


async def test_crear_token_lo_devuelve_una_sola_vez(client, session, user):
    response = await client.post("/api/v1/voz/tokens", json={"name": "iPhone"})

    assert response.status_code == 201
    body = response.json()
    assert body["token"] and body["name"] == "iPhone"
    assert body["last_used_at"] is None

    # En la base queda el hash, no el token.
    row = (await ApiTokenRepository(session).for_user(user.id))[0]
    assert row.token_hash != body["token"]

    # Y el listado nunca lo vuelve a mostrar: si se perdió, se emite otro.
    listado = await client.get("/api/v1/voz/tokens")
    assert "token" not in listado.json()[0]


async def test_listar_tokens(client):
    await emitir(client, "iPhone")
    await emitir(client, "tablet de la cocina")

    body = (await client.get("/api/v1/voz/tokens")).json()

    assert {t["name"] for t in body} == {"iPhone", "tablet de la cocina"}


async def test_revocar_token(client, auth):
    token = await emitir(client)
    token_id = (await client.get("/api/v1/voz/tokens")).json()[0]["id"]

    assert (await client.delete(f"/api/v1/voz/tokens/{token_id}")).status_code == 204
    assert (await client.get("/api/v1/voz/tokens")).json() == []

    # Y deja de servir en el acto.
    response = await client.post(
        "/api/v1/voz/agregar", json={"producto": "leche"}, headers=auth(token)
    )
    assert response.status_code == 401


async def test_revocar_dos_veces_no_es_error(client):
    """El resultado pedido —que ese token no sirva— se cumple igual."""
    assert (await client.delete("/api/v1/voz/tokens/999")).status_code == 204


async def test_no_se_puede_revocar_el_token_de_otro(client, session, user):
    from app.core.security import hash_password
    from app.db.repositories import UserRepository

    otro = await UserRepository(session).create("otro@ejemplo.com", hash_password("x" * 12))
    tokens = ApiTokenRepository(session)
    await tokens.issue(otro.id, name="el de otro")
    await session.commit()

    ajeno = (await tokens.for_user(otro.id))[0]
    assert (await client.delete(f"/api/v1/voz/tokens/{ajeno.id}")).status_code == 204

    # 204 por idempotencia, pero la fila del otro sigue entera.
    assert len(await tokens.for_user(otro.id)) == 1


async def test_administrar_tokens_pide_sesion(client):
    client.cookies.delete(settings.COOKIE_NAME)

    assert (await client.get("/api/v1/voz/tokens")).status_code == 401
    assert (await client.post("/api/v1/voz/tokens", json={})).status_code == 401


async def test_un_token_no_puede_emitir_otro(client, auth):
    """Si pudiera, el "sólo sirve para la lista" duraría hasta que alguien lo
    usara para escalar a un token con más permisos."""
    token = await emitir(client)
    client.cookies.delete(settings.COOKIE_NAME)

    response = await client.post(
        "/api/v1/voz/tokens", json={"name": "escalada"}, headers=auth(token)
    )

    assert response.status_code == 401


# ----------------------------------------------------------- autenticación


async def test_sin_header_no_entra(client):
    response = await client.post("/api/v1/voz/agregar", json={"producto": "leche"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer ", "inventado", "Basic dXNlcjpwYXNz", "Bearer no-existe"],
    ids=["vacio", "sin-token", "token-vacio", "sin-esquema", "basic", "desconocido"],
)
async def test_headers_invalidos(client, header):
    response = await client.post(
        "/api/v1/voz/agregar",
        json={"producto": "leche"},
        headers={"Authorization": header},
    )
    assert response.status_code == 401


async def test_el_token_no_abre_el_resto_de_la_cuenta(client, auth):
    """Lo que hace que un token pegado en un atajo sea tolerable."""
    token = await emitir(client)
    client.cookies.delete(settings.COOKIE_NAME)

    for path in ("/api/v1/auth/me", "/api/v1/profile", "/api/v1/addresses"):
        response = await client.get(path, headers=auth(token))
        assert response.status_code == 401, f"{path} aceptó el token de voz"


async def test_se_marca_cuando_se_usa(client, session, user, auth):
    token = await emitir(client)
    await client.post("/api/v1/voz/agregar", json={"producto": "pan"}, headers=auth(token))

    row = (await ApiTokenRepository(session).for_user(user.id))[0]
    assert row.last_used_at is not None


async def test_la_puerta_de_acceso_no_tapa_la_voz(client, monkeypatch, auth):
    """Un Atajo de Siri no tiene cómo presentar la `ACCESS_KEY`.

    Sin la exención el atajo recibe 401 y lo único que se escucha es "no pude
    hacerlo", sin ninguna pista de por qué.
    """
    token = await emitir(client)
    monkeypatch.setattr(settings, "ACCESS_KEY", "una-clave-de-acceso")

    response = await client.post(
        "/api/v1/voz/agregar", json={"producto": "leche"}, headers=auth(token)
    )

    assert response.status_code == 200


# ------------------------------------------------------------- las órdenes


async def test_agregar(client, session, user, auth):
    token = await emitir(client)

    response = await client.post(
        "/api/v1/voz/agregar", json={"producto": "leche descremada"}, headers=auth(token)
    )

    assert response.status_code == 200
    assert dicho(response) == "Listo, agregué leche descremada."

    row = await ShoppingListRepository(session, user.id).default_list()
    assert [line.query for line in row.lines] == ["leche descremada"]


async def test_agregar_sin_producto_contesta_hablando(client, auth):
    """El dictado a veces manda vacío. Un 422 le haría decir al atajo "no pude
    hacerlo", que no le explica nada a quien está cocinando."""
    token = await emitir(client)

    response = await client.post(
        "/api/v1/voz/agregar", json={"producto": ""}, headers=auth(token)
    )

    assert response.status_code == 200
    assert "no te entendí" in dicho(response).lower()


async def test_agregar_no_deduplica(client, session, user, auth):
    token = await emitir(client)
    for _ in range(2):
        await client.post(
            "/api/v1/voz/agregar", json={"producto": "leche"}, headers=auth(token)
        )

    row = await ShoppingListRepository(session, user.id).default_list()
    assert len(row.lines) == 2


async def test_lista_llena(client, session, user, auth):
    token = await emitir(client)
    lists = ShoppingListRepository(session, user.id)
    row = await lists.default_list()
    await lists.replace_lines(
        row, [{"query": f"producto {n}", "quantity": 1} for n in range(50)]
    )
    await session.commit()

    response = await client.post(
        "/api/v1/voz/agregar", json={"producto": "leche"}, headers=auth(token)
    )

    assert "llena" in dicho(response)
    assert len((await lists.default_list()).lines) == 50


async def test_leer_lista_vacia(client, auth):
    token = await emitir(client)
    response = await client.get("/api/v1/voz/lista", headers=auth(token))

    assert dicho(response) == "Tu lista está vacía."


async def test_leer_lista(client, session, user, auth):
    token = await emitir(client)
    lists = ShoppingListRepository(session, user.id)
    row = await lists.default_list()
    await lists.replace_lines(
        row, [{"query": q, "quantity": 1} for q in ("leche", "pan", "huevos")]
    )
    await session.commit()

    response = await client.get("/api/v1/voz/lista", headers=auth(token))

    # La "y" antes del último es lo que hace que suene a una lista.
    assert dicho(response) == "Tenés 3 productos: leche, pan y huevos."


async def test_lista_larga_se_corta(client, session, user, auth):
    """Cuarenta productos dichos de corrido no son información: para la mitad
    quien escucha ya perdió el hilo, y no puede rebobinar."""
    token = await emitir(client)
    lists = ShoppingListRepository(session, user.id)
    row = await lists.default_list()
    await lists.replace_lines(
        row, [{"query": f"producto {n}", "quantity": 1} for n in range(40)]
    )
    await session.commit()

    texto = dicho(await client.get("/api/v1/voz/lista", headers=auth(token)))

    assert "Tenés 40 productos" in texto
    assert "y 25 más" in texto
    assert "producto 39" not in texto


async def test_borrar(client, session, user, auth):
    token = await emitir(client)
    lists = ShoppingListRepository(session, user.id)
    row = await lists.default_list()
    await lists.replace_lines(
        row, [{"query": q, "quantity": 1} for q in ("leche", "pan")]
    )
    await session.commit()

    response = await client.post(
        "/api/v1/voz/borrar", json={"producto": "leche"}, headers=auth(token)
    )

    assert "leche" in dicho(response)
    row = await lists.default_list()
    assert [line.query for line in row.lines] == ["pan"]
    # Renumerado: el frontend ordena por `position` y un hueco lo confunde.
    assert [line.position for line in row.lines] == [0]


async def test_borrar_ignora_mayusculas_y_espacios(client, session, user, auth):
    """Del otro lado hay un dictado: quien escribió "Leche" dijo "leche", y no
    tiene forma de saber por qué no coincidieron."""
    token = await emitir(client)
    lists = ShoppingListRepository(session, user.id)
    row = await lists.default_list()
    await lists.replace_lines(row, [{"query": "Leche", "quantity": 1}])
    await session.commit()

    response = await client.post(
        "/api/v1/voz/borrar", json={"producto": "  leche "}, headers=auth(token)
    )

    assert "saqué" in dicho(response).lower()
    assert (await lists.default_list()).lines == []


async def test_borrar_algo_que_no_esta(client, auth):
    token = await emitir(client)
    response = await client.post(
        "/api/v1/voz/borrar", json={"producto": "caviar"}, headers=auth(token)
    )

    assert "no encontré" in dicho(response).lower()


async def test_cada_token_escribe_en_su_lista(client, session, user, auth):
    """El `user_id` sale del token, nunca del cuerpo del request."""
    from app.core.security import hash_password
    from app.db.repositories import UserRepository

    otro = await UserRepository(session).create("otro@ejemplo.com", hash_password("x" * 12))
    ajeno = await ApiTokenRepository(session).issue(otro.id)
    await session.commit()

    await client.post(
        "/api/v1/voz/agregar", json={"producto": "leche"}, headers=auth(ajeno)
    )

    assert (await ShoppingListRepository(session, user.id).default_list()).lines == []
    lista_ajena = await ShoppingListRepository(session, otro.id).default_list()
    assert [line.query for line in lista_ajena.lines] == ["leche"]
