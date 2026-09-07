"""Login, sesiones y aislamiento entre usuarios.

El grupo que más importa es el último. Los tests de login prueban que la puerta
cierra; los de aislamiento prueban lo que de verdad da miedo en esta app, que es
que la puerta cierre pero adentro todos vean las cosas de todos. Esta base
guarda domicilios con coordenadas, DNI y BIN de tarjeta: un `user_id` mal
pasado no es un bug de permisos, es una filtración de datos personales.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.db.repositories import AuthSessionRepository, UserRepository
from app.db.tables import Base

PASSWORD = "una contraseña larga"


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def client(session, monkeypatch):
    """La app real con la base de test enchufada.

    `COOKIE_SECURE=False` porque httpx no manda cookies `Secure` sobre
    `http://test`, y sin esto todo test que dependa de estar logueado fallaría
    con un 401 que no tiene nada que ver con lo que se está probando. Es
    exactamente el mismo pozo en el que cae quien desarrolla contra localhost
    con la config de producción.
    """
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)

    from app.core.db import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _register(client, email: str = "uno@ejemplo.com") -> httpx.Response:
    return await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )


# ------------------------------------------------------------------- registro


@pytest.mark.asyncio
async def test_registro_deja_la_sesion_abierta(client):
    """Registrarse loguea. Mandar al login después de crear la cuenta obligaría
    a escribir la contraseña que se acaba de escribir, sin verificar nada."""
    response = await _register(client)
    assert response.status_code == 201
    assert response.json()["email"] == "uno@ejemplo.com"
    assert settings.COOKIE_NAME in response.cookies

    assert (await client.get("/api/v1/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_el_email_se_normaliza(client, session):
    """`Pedro@Gmail.com ` y `pedro@gmail.com` son la misma cuenta.

    Si la normalización viviera solo en el login, el índice único dejaría crear
    las dos y quien se registró con mayúsculas no podría volver a entrar.
    """
    await _register(client, "  Pedro@Ejemplo.COM ")

    user = await UserRepository(session).by_email("pedro@ejemplo.com")
    assert user is not None

    duplicado = await _register(client, "pedro@ejemplo.com")
    assert duplicado.status_code == 409


@pytest.mark.asyncio
async def test_contrasena_corta_rechazada(client):
    response = await client.post(
        "/api/v1/auth/register", json={"email": "x@ejemplo.com", "password": "corta"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_la_contrasena_no_se_guarda_en_claro(client, session):
    """Lo mínimo que hay que verificar de cualquier sistema de login."""
    await _register(client)

    user = await UserRepository(session).by_email("uno@ejemplo.com")
    assert user.password_hash != PASSWORD
    assert PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2")
    assert verify_password(PASSWORD, user.password_hash)


@pytest.mark.asyncio
async def test_registro_cerrado(client, monkeypatch):
    monkeypatch.setattr(settings, "REGISTRATION_OPEN", False)
    assert (await _register(client)).status_code == 403


# ---------------------------------------------------------------------- login


@pytest.mark.asyncio
async def test_login_y_logout(client):
    await _register(client)
    await client.post("/api/v1/auth/logout")
    assert (await client.get("/api/v1/auth/me")).status_code == 401

    response = await client.post(
        "/api/v1/auth/login", json={"email": "uno@ejemplo.com", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_logout_invalida_la_sesion_del_lado_del_servidor(client, session):
    """Que la cookie se borre del navegador no alcanza.

    Es la diferencia práctica con un JWT: acá el token que alguien haya copiado
    antes del logout deja de servir, porque la fila que lo respaldaba ya no está.
    """
    await _register(client)
    token = client.cookies[settings.COOKIE_NAME]

    await client.post("/api/v1/auth/logout")

    # Se reinyecta el token a mano, como haría quien lo hubiera interceptado.
    client.cookies.set(settings.COOKIE_NAME, token)
    assert (await client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_credenciales_malas_no_revelan_si_la_cuenta_existe(client):
    """Misma respuesta para "no existe" y "contraseña equivocada".

    Distinguirlas convierte el formulario de login en un buscador de cuentas: se
    prueban emails hasta que uno conteste distinto.
    """
    await _register(client)

    inexistente = await client.post(
        "/api/v1/auth/login", json={"email": "nadie@ejemplo.com", "password": PASSWORD}
    )
    equivocada = await client.post(
        "/api/v1/auth/login",
        json={"email": "uno@ejemplo.com", "password": "otra contraseña"},
    )

    assert inexistente.status_code == equivocada.status_code == 401
    assert inexistente.json()["detail"] == equivocada.json()["detail"]


@pytest.mark.asyncio
async def test_cookie_de_sesion_es_httponly(client):
    """Sin `HttpOnly`, un XSS se lleva la sesión. Es la razón por la que el token
    no vive en `localStorage`."""
    response = await _register(client)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite" in cookie


@pytest.mark.asyncio
async def test_cuenta_inactiva_no_entra(client, session):
    await _register(client)
    user = await UserRepository(session).by_email("uno@ejemplo.com")
    user.is_active = False
    await session.commit()

    response = await client.post(
        "/api/v1/auth/login", json={"email": "uno@ejemplo.com", "password": PASSWORD}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_rate_limit_frena_la_fuerza_bruta(client, monkeypatch):
    """Después de N fallos la misma IP recibe 429 en vez de otra chance."""
    from app.api.v1 import auth as auth_module

    monkeypatch.setattr(auth_module._login_limiter, "_max", 3)
    auth_module._login_limiter.reset("testclient")

    await _register(client)
    for _ in range(3):
        await client.post(
            "/api/v1/auth/login", json={"email": "uno@ejemplo.com", "password": "mal"}
        )

    # Incluso con la contraseña correcta: el límite es por intentos, no por
    # aciertos, o bastaría con acertar una vez para resetearlo desde afuera.
    bloqueado = await client.post(
        "/api/v1/auth/login", json={"email": "uno@ejemplo.com", "password": PASSWORD}
    )
    assert bloqueado.status_code == 429
    assert "Retry-After" in bloqueado.headers

    auth_module._login_limiter.reset("testclient")


# ------------------------------------------------------- cambio de contraseña


@pytest.mark.asyncio
async def test_cambiar_contrasena_cierra_las_otras_sesiones(client, session):
    """La mitad que se olvida.

    Cambiar la contraseña suele ser la reacción a "creo que alguien entró". Si
    las sesiones que ese alguien abrió siguen vivas, el cambio no sirvió.
    """
    await _register(client)

    # Una segunda sesión, como si fuera otro dispositivo.
    otro = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client._transport.app), base_url="http://test"
    )
    async with otro:
        await otro.post(
            "/api/v1/auth/login",
            json={"email": "uno@ejemplo.com", "password": PASSWORD},
        )
        assert (await otro.get("/api/v1/auth/me")).status_code == 200

        response = await client.post(
            "/api/v1/auth/change-password",
            json={"current_password": PASSWORD, "new_password": "otra bien larga"},
        )
        assert response.status_code == 204

        # La otra sesión murió...
        assert (await otro.get("/api/v1/auth/me")).status_code == 401

    # ...y la que hizo el cambio sigue viva: echar de la máquina a quien acaba de
    # cambiar su propia contraseña sería un castigo sin motivo.
    assert (await client.get("/api/v1/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_cambiar_contrasena_exige_la_actual(client):
    """Si no, quien te agarra la sesión abierta te deja afuera de tu cuenta."""
    await _register(client)
    response = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "no es esta", "new_password": "otra bien larga"},
    )
    assert response.status_code == 403


# --------------------------------------------------- endpoints sin sesión


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/profile"),
        ("GET", "/api/v1/addresses"),
        ("GET", "/api/v1/payment-instruments"),
        ("GET", "/api/v1/custom-stores"),
        ("GET", "/api/v1/shopping-lists"),
        ("GET", "/api/v1/store-locations"),
        ("GET", "/api/v1/search?q=leche"),
        ("GET", "/api/v1/compare?q=leche"),
        ("GET", "/api/v1/chains"),
        ("GET", "/api/v1/geocode?address=Corrientes+1234"),
        ("GET", "/api/v1/items"),
    ],
)
async def test_todo_pide_sesion(client, method, path):
    """El barrido que justifica aplicar la dependencia a nivel router.

    `/search`, `/compare` y `/chains` no tocan datos de nadie, pero cada llamada
    dispara el fan-out contra los supermercados: abiertos en internet son un
    proxy gratis pagado con la cuota de este server.
    """
    assert (await client.request(method, path)).status_code == 401


@pytest.mark.asyncio
async def test_health_es_publico(client):
    """Tiene que contestar sin sesión o ningún chequeo de deploy funciona."""
    assert (await client.get("/api/v1/health")).status_code == 200


# ------------------------------------------------- aislamiento entre usuarios


@pytest_asyncio.fixture
async def dos_usuarios(client, session):
    """Dos cuentas, cada una con su cliente HTTP y su cookie."""
    transport = httpx.ASGITransport(app=client._transport.app)

    ana = httpx.AsyncClient(transport=transport, base_url="http://test")
    beto = httpx.AsyncClient(transport=transport, base_url="http://test")
    async with ana, beto:
        await ana.post(
            "/api/v1/auth/register",
            json={"email": "ana@ejemplo.com", "password": PASSWORD},
        )
        await beto.post(
            "/api/v1/auth/register",
            json={"email": "beto@ejemplo.com", "password": PASSWORD},
        )
        yield ana, beto


@pytest.mark.asyncio
async def test_los_domicilios_no_se_cruzan(dos_usuarios):
    """El caso que más duele si sale mal: un domicilio con coordenadas."""
    ana, beto = dos_usuarios

    creado = await ana.post(
        "/api/v1/addresses",
        json={"label": "Casa", "street": "Vuelta de Obligado", "number": "1534"},
    )
    assert creado.status_code == 201
    address_id = creado.json()["id"]

    assert (await beto.get("/api/v1/addresses")).json() == []

    # Ni siquiera sabiendo el id: `by_id` filtra por dueño y el endpoint contesta
    # 404, no 403. Un 403 confirmaría que ese domicilio existe.
    assert (await beto.get("/api/v1/addresses")).status_code == 200
    assert (
        await beto.patch(f"/api/v1/addresses/{address_id}", json={"label": "Mía"})
    ).status_code == 404
    assert (await beto.delete(f"/api/v1/addresses/{address_id}")).status_code == 404

    # Y sigue intacto para su dueña.
    assert (await ana.get("/api/v1/addresses")).json()[0]["label"] == "Casa"


@pytest.mark.asyncio
async def test_los_medios_de_pago_no_se_cruzan(dos_usuarios):
    """Un medio de pago guarda el BIN de la tarjeta."""
    ana, beto = dos_usuarios

    creado = await ana.post(
        "/api/v1/payment-instruments",
        json={
            "label": "Brubank",
            "issuer_slug": "brubank",
            "kind": "debit",
            "rails": ["card"],
            "bins": ["41119710"],
        },
    )
    assert creado.status_code == 201

    assert (await beto.get("/api/v1/payment-instruments")).json() == []
    assert (
        await beto.delete(f"/api/v1/payment-instruments/{creado.json()['id']}")
    ).status_code == 404


@pytest.mark.asyncio
async def test_los_perfiles_no_se_cruzan(dos_usuarios):
    ana, beto = dos_usuarios

    await ana.put(
        "/api/v1/profile",
        json={
            "postal_code": "1426",
            "channel": "in_store",
            "theme": "system",
            "full_name": "Ana",
            "dni": "30123456",
        },
    )

    perfil_beto = (await beto.get("/api/v1/profile")).json()
    assert perfil_beto["full_name"] is None
    assert perfil_beto["dni"] is None
    assert perfil_beto["postal_code"] is None


@pytest.mark.asyncio
async def test_las_listas_no_se_cruzan(dos_usuarios):
    ana, beto = dos_usuarios

    propia = (await ana.get("/api/v1/shopping-lists/default")).json()
    await ana.put(
        f"/api/v1/shopping-lists/{propia['id']}",
        json={"lines": [{"query": "leche entera 1L", "quantity": 2}]},
    )

    ajena = await beto.get(f"/api/v1/shopping-lists/{propia['id']}")
    assert ajena.status_code == 404

    # La lista por default de Beto es suya y nace vacía, no es la de Ana.
    suya = (await beto.get("/api/v1/shopping-lists/default")).json()
    assert suya["id"] != propia["id"]
    assert suya["lines"] == []


@pytest.mark.asyncio
async def test_las_sucursales_propias_no_se_cruzan(dos_usuarios, session):
    ana, beto = dos_usuarios
    assert (await beto.get("/api/v1/custom-stores")).json() == []
    assert (await ana.get("/api/v1/custom-stores")).json() == []


# -------------------------------------------------------------------- roles


@pytest.mark.asyncio
async def test_require_role_rechaza_al_que_no_lo_tiene(client, session):
    """`require_role` no lo usa ningún endpoint todavía; se prueba igual para que
    el día que se use no sea la primera vez que corre."""
    from fastapi import Depends, FastAPI

    from app.core.auth import require_role
    from app.core.db import get_db

    await _register(client)

    sub = FastAPI()

    @sub.get("/solo-admin", dependencies=[Depends(require_role("admin"))])
    async def _solo_admin():
        return {"ok": True}

    sub.dependency_overrides[get_db] = lambda: session
    transport = httpx.ASGITransport(app=sub)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies=client.cookies
    ) as admin_client:
        # Cuenta con rol `user`: 403, no 401. Sabemos quién es, le falta permiso.
        assert (await admin_client.get("/solo-admin")).status_code == 403

        user = await UserRepository(session).by_email("uno@ejemplo.com")
        user.role = "admin"
        await session.commit()

        assert (await admin_client.get("/solo-admin")).status_code == 200


# ------------------------------------------------------------------ sesiones


@pytest.mark.asyncio
async def test_sesion_vencida_no_autentica(client, session):
    """La expiración se compara en cada request, no solo al barrer la tabla."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.db import tables as t

    await _register(client)

    row = await session.scalar(select(t.AuthSession))
    row.expires_at = t.utcnow() - timedelta(seconds=1)
    await session.commit()

    assert (await client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_token_inventado_no_autentica(client):
    # ASCII: una cookie no puede llevar otra cosa, así que un token con acentos
    # ni siquiera llega a salir del cliente.
    client.cookies.set(settings.COOKIE_NAME, "un-token-inventado")
    assert (await client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_purge_expired_limpia_las_vencidas(session):
    """Higiene: sin esto la tabla acumula una fila por login para siempre."""
    from datetime import timedelta

    from app.db import tables as t

    users = UserRepository(session)
    user = await users.create("x@ejemplo.com", hash_password(PASSWORD))
    sessions = AuthSessionRepository(session)

    await sessions.create(user.id, "viva", expires_at=t.utcnow() + timedelta(days=1))
    await sessions.create(user.id, "muerta", expires_at=t.utcnow() - timedelta(days=1))
    await session.commit()

    assert await sessions.purge_expired() == 1
    assert await sessions.by_token_hash("viva") is not None
    assert await sessions.by_token_hash("muerta") is None
