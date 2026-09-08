"""El endpoint del skill: verificación de firma y despacho de intents.

`/alexa/skill` es la puerta más expuesta del deploy —pública, sin cookie, sin
clave de acceso— y escribe en la lista de compras de un usuario. Todo lo que la
protege está en la primera mitad de este archivo.

Los tests firman de verdad, con un certificado autofirmado que se genera al
arrancar la suite y una respuesta de S3 mockeada con `respx`. Es más trabajo que
parchear el verificador, y es lo que hace que estos tests sirvan de algo: un
mock del verificador probaría que el mock funciona.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from app.core.config import settings
from app.db.repositories import AlexaSkillTokenRepository, ShoppingListRepository
from app.services.alexa import signature

pytestmark = pytest.mark.asyncio

SKILL_ID = "amzn1.ask.skill.test-0000"
CERT_URL = "https://s3.amazonaws.com/echo.api/echo-api-cert-test.pem"


def _ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Una CA de test, que hace de raíz de confianza.

    Existe porque la verificación exige que la cadena llegue hasta una raíz
    conocida: un certificado autofirmado ya no alcanza, que es justamente lo que
    ese chequeo protege. Los tests la inyectan monkeypatcheando `_trusted_roots`,
    así que lo que se prueba es la verificación de verdad y no una versión floja.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CA de test")])
    now = datetime.now(UTC)

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


CA_KEY, CA_CERT = _ca()


def _certificado(
    *, san: str = signature.CERT_SAN, dias: int = 30, firmada_por_la_ca: bool = True
) -> tuple[rsa.RSAPrivateKey, bytes]:
    """Un certificado de hoja que se hace pasar por el de Amazon.

    Con `firmada_por_la_ca=False` sale autofirmado, que es como se prueba que la
    verificación de cadena rechaza lo que no encadena.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, san)])

    firmante_key, issuer, firmante_pub = (
        (CA_KEY, CA_CERT.subject, CA_KEY.public_key())
        if firmada_por_la_ca
        else (key, subject, key.public_key())
    )

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=dias))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        # El perfil RFC 5280 lo exige en la hoja, y los certificados reales de
        # Amazon lo traen. Sin él la verificación falla con "missing required
        # extension" y parece un problema del código, no de la fixture.
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(firmante_pub),
            critical=False,
        )
        .sign(firmante_key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM)


AMAZON_KEY, AMAZON_PEM = _certificado()
OTRA_KEY, _ = _certificado()


def firmar(body: bytes, key: rsa.RSAPrivateKey = AMAZON_KEY) -> str:
    return base64.b64encode(
        key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    ).decode()


def sobre(
    *,
    tipo: str = "IntentRequest",
    intent: str | None = None,
    slots: dict | None = None,
    token: str | None = "un-token",
    app_id: str = SKILL_ID,
    timestamp: str | None = None,
) -> bytes:
    """El JSON que manda Alexa, serializado a los bytes que se van a firmar."""
    request: dict = {
        "type": tipo,
        "requestId": "amzn1.echo-api.request.test",
        "timestamp": timestamp or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "locale": "es-MX",
    }
    if intent is not None:
        request["intent"] = {"name": intent, "slots": slots or {}}

    return json.dumps(
        {
            "version": "1.0",
            "context": {
                "System": {
                    "application": {"applicationId": app_id},
                    "user": {"userId": "amzn1.ask.account.test"}
                    | ({"accessToken": token} if token else {}),
                }
            },
            "request": request,
        }
    ).encode()


@pytest_asyncio.fixture(autouse=True)
async def skill_configurado(monkeypatch):
    monkeypatch.setattr(settings, "ALEXA_SKILL_ID", SKILL_ID)
    # La CA de test hace de raíz de confianza en lugar de las de `certifi`. Es la
    # única concesión de este archivo: todo lo demás —la firma, la cadena, los
    # SAN, las fechas— se verifica de verdad.
    monkeypatch.setattr(signature, "_trusted_roots", lambda: [CA_CERT])
    # El caché es global al proceso y sobreviviría entre tests, tapando el caso
    # en que la descarga del certificado tendría que volver a pasar.
    signature._cert_cache.clear()
    yield
    signature._cert_cache.clear()


@pytest.fixture
def s3():
    """S3 devolviendo el certificado con el que firman los tests."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(CERT_URL).mock(
            return_value=httpx.Response(200, content=AMAZON_PEM)
        )
        yield mock


@pytest_asyncio.fixture
async def vinculado(session, user):
    """Un usuario con el skill ya vinculado. Devuelve el `access_token`."""
    access, _ = await AlexaSkillTokenRepository(session).issue(user.id)
    await session.commit()
    return access


async def hablar(client, body: bytes, *, firma: str | None = None, url: str = CERT_URL):
    return await client.post(
        "/alexa/skill",
        content=body,
        headers={
            "Content-Type": "application/json",
            signature.SIGNATURE_HEADER: firma if firma is not None else firmar(body),
            signature.CERT_CHAIN_HEADER: url,
        },
    )


def dijo(response) -> str:
    return response.json()["response"]["outputSpeech"]["text"]


# --------------------------------------------------------- verificación


async def test_firma_valida_entra(client, s3, vinculado):
    body = sobre(intent="LeerListaIntent", token=vinculado)
    response = await hablar(client, body)

    assert response.status_code == 200, response.text
    assert "vacía" in dijo(response)


async def test_firma_de_otra_clave_no_entra(client, s3, vinculado):
    body = sobre(intent="LeerListaIntent", token=vinculado)
    response = await hablar(client, body, firma=firmar(body, OTRA_KEY))

    assert response.status_code == 400


async def test_cuerpo_alterado_despues_de_firmar(client, s3, vinculado):
    """El caso real que la firma frena: alguien captura un request legítimo y le
    cambia el producto antes de reenviarlo."""
    original = sobre(
        intent="AgregarProductoIntent",
        slots={"producto": {"name": "producto", "value": "leche"}},
        token=vinculado,
    )
    firma = firmar(original)
    alterado = original.replace(b"leche", b"vodka")

    response = await hablar(client, alterado, firma=firma)
    assert response.status_code == 400


async def test_sin_cabeceras_de_firma(client, s3, vinculado):
    response = await client.post(
        "/alexa/skill", content=sobre(intent="LeerListaIntent", token=vinculado)
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "url",
    [
        "https://malicioso.example/echo.api/cert.pem",
        "http://s3.amazonaws.com/echo.api/cert.pem",
        "https://s3.amazonaws.com/otra-carpeta/cert.pem",
        "https://s3.amazonaws.com:8443/echo.api/cert.pem",
        "https://s3.amazonaws.com/Echo.API/cert.pem",
    ],
)
async def test_url_de_certificado_invalida(client, vinculado, url):
    """Sin `respx`: si alguno de estos pasara, el test fallaría al intentar salir
    a internet de verdad, que es exactamente lo que no tiene que pasar."""
    body = sobre(intent="LeerListaIntent", token=vinculado)
    response = await hablar(client, body, url=url)

    assert response.status_code == 400


async def test_url_de_certificado_con_salto_de_directorio(client, vinculado):
    """`..` resuelto: el string empieza con `/echo.api/` y apunta a otro lado.

    Es el bug clásico de este chequeo, y el motivo de que se normalice antes de
    comparar en vez de hacer un `startswith` sobre el path crudo.
    """
    body = sobre(intent="LeerListaIntent", token=vinculado)
    response = await hablar(
        client, body, url="https://s3.amazonaws.com/echo.api/../evil/cert.pem"
    )

    assert response.status_code == 400


async def test_certificado_de_otro_dominio(client, vinculado, monkeypatch):
    """Un certificado válido y bien firmado, pero que no es de Amazon."""
    key, pem = _certificado(san="malicioso.example")

    with respx.mock(assert_all_called=False) as mock:
        mock.get(CERT_URL).mock(return_value=httpx.Response(200, content=pem))
        body = sobre(intent="LeerListaIntent", token=vinculado)
        response = await hablar(client, body, firma=firmar(body, key))

    assert response.status_code == 400


async def test_certificado_que_no_encadena(client, vinculado):
    """Un autofirmado con `echo-api.amazon.com` en los SAN.

    Pasa el chequeo de dominio y el de fechas: lo único que lo frena es que no
    llega hasta una raíz de confianza. Es el ataque que quedaría vivo si alguien
    lograra escribir un archivo bajo `s3.amazonaws.com/echo.api/`.
    """
    key, pem = _certificado(firmada_por_la_ca=False)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(CERT_URL).mock(return_value=httpx.Response(200, content=pem))
        body = sobre(intent="LeerListaIntent", token=vinculado)
        response = await hablar(client, body, firma=firmar(body, key))

    assert response.status_code == 400


async def test_certificado_vencido(client, vinculado):
    key, pem = _certificado(dias=-1)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(CERT_URL).mock(return_value=httpx.Response(200, content=pem))
        body = sobre(intent="LeerListaIntent", token=vinculado)
        response = await hablar(client, body, firma=firmar(body, key))

    assert response.status_code == 400


async def test_request_viejo_no_se_puede_reproducir(client, s3, vinculado):
    """La firma de un cuerpo capturado sigue siendo válida para siempre.

    Lo único que impide reenviarlo mañana es el `timestamp`.
    """
    viejo = (datetime.now(UTC) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    body = sobre(intent="LeerListaIntent", token=vinculado, timestamp=viejo)

    response = await hablar(client, body)
    assert response.status_code == 400


async def test_applicationid_de_otro_skill(client, s3, vinculado):
    """La firma de Amazon es la misma para todos los skills del mundo.

    Sin este chequeo, cualquier otro developer podría postear acá con una firma
    perfectamente válida y escribir en las listas de nuestros usuarios.
    """
    body = sobre(
        intent="LeerListaIntent", token=vinculado, app_id="amzn1.ask.skill.de-otro"
    )
    response = await hablar(client, body)

    assert response.status_code == 400


async def test_skill_apagado_es_404(client, s3, vinculado, monkeypatch):
    monkeypatch.setattr(settings, "ALEXA_SKILL_ID", "")
    response = await hablar(client, sobre(intent="LeerListaIntent", token=vinculado))

    assert response.status_code == 404


async def test_el_certificado_se_baja_una_sola_vez(client, s3, vinculado):
    """Sin caché sería una descarga desde S3 por cada frase del usuario."""
    for _ in range(3):
        assert (
            await hablar(client, sobre(intent="LeerListaIntent", token=vinculado))
        ).status_code == 200

    assert s3.get(CERT_URL).call_count == 1


# --------------------------------------------------------------- vínculo


async def test_sin_token_pide_vincular(client, s3):
    response = await hablar(client, sobre(intent="LeerListaIntent", token=None))

    assert response.status_code == 200
    # 200 y no 401: el usuario tiene que **escuchar** qué hacer, y la card es lo
    # que le pone el botón en la app de Alexa.
    assert response.json()["response"]["card"] == {"type": "LinkAccount"}
    assert "vincular" in dijo(response).lower()


async def test_token_desconocido_pide_vincular(client, s3):
    response = await hablar(client, sobre(intent="LeerListaIntent", token="inventado"))

    assert response.json()["response"]["card"] == {"type": "LinkAccount"}


async def test_token_vencido_pide_vincular(client, s3, session, user):
    access, _ = await AlexaSkillTokenRepository(session).issue(user.id)
    row = (await AlexaSkillTokenRepository(session).for_user(user.id))[0]
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    response = await hablar(client, sobre(intent="LeerListaIntent", token=access))
    assert response.json()["response"]["card"] == {"type": "LinkAccount"}


async def test_el_token_vencido_no_se_borra(client, s3, session, user):
    """La fila es lo que le permite a Alexa refrescar.

    Borrarla desvincularía al usuario por el solo hecho de que pasaron treinta
    días sin que le hablara al Echo.
    """
    access, _ = await AlexaSkillTokenRepository(session).issue(user.id)
    row = (await AlexaSkillTokenRepository(session).for_user(user.id))[0]
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    await hablar(client, sobre(intent="LeerListaIntent", token=access))

    assert len(await AlexaSkillTokenRepository(session).for_user(user.id)) == 1


async def test_se_marca_cuando_se_uso(client, s3, session, user, vinculado):
    await hablar(client, sobre(intent="LeerListaIntent", token=vinculado))

    row = (await AlexaSkillTokenRepository(session).for_user(user.id))[0]
    assert row.last_used_at is not None


# ---------------------------------------------------------------- intents


async def test_agregar_escribe_en_la_lista(client, s3, session, user, vinculado):
    response = await hablar(
        client,
        sobre(
            intent="AgregarProductoIntent",
            slots={"producto": {"name": "producto", "value": "leche descremada"}},
            token=vinculado,
        ),
    )

    assert "leche descremada" in dijo(response)

    row = await ShoppingListRepository(session, user.id).default_list()
    assert [line.query for line in row.lines] == ["leche descremada"]


async def test_agregar_dos_veces_no_deduplica(client, s3, session, user, vinculado):
    """Decir "agregá leche" dos veces suele significar dos leches."""
    for _ in range(2):
        await hablar(
            client,
            sobre(
                intent="AgregarProductoIntent",
                slots={"producto": {"name": "producto", "value": "leche"}},
                token=vinculado,
            ),
        )

    row = await ShoppingListRepository(session, user.id).default_list()
    assert len(row.lines) == 2


async def test_agregar_con_slot_vacio(client, s3, vinculado):
    """Alexa manda el slot presente y sin `value` cuando no entendió el audio."""
    response = await hablar(
        client,
        sobre(
            intent="AgregarProductoIntent",
            slots={"producto": {"name": "producto"}},
            token=vinculado,
        ),
    )

    assert "no te entendí" in dijo(response).lower()


async def test_lista_llena_avisa_y_no_escribe(client, s3, session, user, vinculado):
    lists = ShoppingListRepository(session, user.id)
    row = await lists.default_list()
    await lists.replace_lines(
        row, [{"query": f"producto {n}", "quantity": 1} for n in range(50)]
    )
    await session.commit()

    response = await hablar(
        client,
        sobre(
            intent="AgregarProductoIntent",
            slots={"producto": {"name": "producto", "value": "leche"}},
            token=vinculado,
        ),
    )

    assert "llena" in dijo(response)
    row = await lists.default_list()
    assert len(row.lines) == 50


async def test_leer_lista_vacia(client, s3, vinculado):
    response = await hablar(client, sobre(intent="LeerListaIntent", token=vinculado))
    assert dijo(response) == "Tu lista está vacía."


async def test_leer_lista_con_productos(client, s3, session, user, vinculado):
    lists = ShoppingListRepository(session, user.id)
    row = await lists.default_list()
    await lists.replace_lines(
        row, [{"query": q, "quantity": 1} for q in ("leche", "pan", "huevos")]
    )
    await session.commit()

    response = await hablar(client, sobre(intent="LeerListaIntent", token=vinculado))

    # La "y" antes del último es lo que hace que suene a una lista.
    assert dijo(response) == "Tenés 3 productos: leche, pan y huevos."


async def test_leer_una_sola_cosa(client, s3, session, user, vinculado):
    lists = ShoppingListRepository(session, user.id)
    row = await lists.default_list()
    await lists.replace_lines(row, [{"query": "leche", "quantity": 1}])
    await session.commit()

    response = await hablar(client, sobre(intent="LeerListaIntent", token=vinculado))
    assert dijo(response) == "Tenés una sola cosa: leche."


async def test_lista_larga_se_corta(client, s3, session, user, vinculado):
    """Cuarenta productos dichos de corrido no son información: para la mitad el
    usuario ya perdió el hilo, y no puede rebobinar."""
    lists = ShoppingListRepository(session, user.id)
    row = await lists.default_list()
    await lists.replace_lines(
        row, [{"query": f"producto {n}", "quantity": 1} for n in range(40)]
    )
    await session.commit()

    response = await hablar(client, sobre(intent="LeerListaIntent", token=vinculado))
    texto = dijo(response)

    assert "Tenés 40 productos" in texto
    assert "y 25 más" in texto
    assert "producto 39" not in texto


async def test_borrar_saca_el_producto(client, s3, session, user, vinculado):
    lists = ShoppingListRepository(session, user.id)
    row = await lists.default_list()
    await lists.replace_lines(
        row, [{"query": q, "quantity": 1} for q in ("leche", "pan")]
    )
    await session.commit()

    response = await hablar(
        client,
        sobre(
            intent="BorrarProductoIntent",
            slots={"producto": {"name": "producto", "value": "leche"}},
            token=vinculado,
        ),
    )

    assert "leche" in dijo(response)
    row = await lists.default_list()
    assert [line.query for line in row.lines] == ["pan"]
    # Renumerado: el frontend ordena por `position` y un hueco lo confunde.
    assert [line.position for line in row.lines] == [0]


async def test_borrar_ignora_mayusculas(client, s3, session, user, vinculado):
    """Del otro lado hay un transcriptor de voz: el usuario escribió "Leche" y
    dijo "leche", y no tiene forma de saber por qué no coincidieron."""
    lists = ShoppingListRepository(session, user.id)
    row = await lists.default_list()
    await lists.replace_lines(row, [{"query": "Leche", "quantity": 1}])
    await session.commit()

    response = await hablar(
        client,
        sobre(
            intent="BorrarProductoIntent",
            slots={"producto": {"name": "producto", "value": "  leche "}},
            token=vinculado,
        ),
    )

    assert "saqué" in dijo(response).lower()
    assert (await lists.default_list()).lines == []


async def test_borrar_algo_que_no_esta(client, s3, vinculado):
    response = await hablar(
        client,
        sobre(
            intent="BorrarProductoIntent",
            slots={"producto": {"name": "producto", "value": "caviar"}},
            token=vinculado,
        ),
    )

    assert "no encontré" in dijo(response).lower()


async def test_cada_uno_escribe_en_su_lista(client, s3, session, user, vinculado):
    """El `user_id` sale del token, no del cuerpo del request."""
    from app.core.security import hash_password
    from app.db.repositories import UserRepository

    otro = await UserRepository(session).create("otro@ejemplo.com", hash_password("x" * 12))
    otro_token, _ = await AlexaSkillTokenRepository(session).issue(otro.id)
    await session.commit()

    await hablar(
        client,
        sobre(
            intent="AgregarProductoIntent",
            slots={"producto": {"name": "producto", "value": "leche"}},
            token=otro_token,
        ),
    )

    assert (await ShoppingListRepository(session, user.id).default_list()).lines == []
    ajena = await ShoppingListRepository(session, otro.id).default_list()
    assert [line.query for line in ajena.lines] == ["leche"]


# ------------------------------------------------------- protocolo de Alexa


async def test_launch_deja_la_sesion_abierta(client, s3, vinculado):
    """"Alexa, abrí Ahorrito", sin decir qué quiere: hay que esperar respuesta."""
    response = await hablar(client, sobre(tipo="LaunchRequest", token=vinculado))

    assert response.json()["response"]["shouldEndSession"] is False
    assert "agregá" in dijo(response)


async def test_una_orden_cierra_la_sesion(client, s3, vinculado):
    """Dejarla abierta enciende el micrófono y el Echo se queda escuchando."""
    response = await hablar(
        client,
        sobre(
            intent="AgregarProductoIntent",
            slots={"producto": {"name": "producto", "value": "pan"}},
            token=vinculado,
        ),
    )

    assert response.json()["response"]["shouldEndSession"] is True


async def test_session_ended_no_habla(client, s3, vinculado):
    """Amazon exige responder pero ignora el contenido."""
    response = await hablar(client, sobre(tipo="SessionEndedRequest", token=vinculado))

    assert response.status_code == 200
    assert response.json()["response"] == {}


async def test_stop(client, s3, vinculado):
    response = await hablar(client, sobre(intent="AMAZON.StopIntent", token=vinculado))
    assert response.json()["response"]["shouldEndSession"] is True


async def test_ayuda(client, s3, vinculado):
    response = await hablar(client, sobre(intent="AMAZON.HelpIntent", token=vinculado))
    assert "ahorrito" in dijo(response).lower()


async def test_intent_desconocido_no_rompe(client, s3, vinculado):
    response = await hablar(client, sobre(intent="IntentQueNoExiste", token=vinculado))

    assert response.status_code == 200
    assert "no entendí" in dijo(response).lower()


async def test_un_error_interno_se_cuenta_hablando(client, s3, vinculado, monkeypatch):
    """Un 500 le hace decir a Alexa "hubo un problema con la skill solicitada".

    El usuario está parado en la cocina esperando que le confirmen que anotó la
    leche: tiene que escuchar algo que se entienda.
    """
    async def explota(*args, **kwargs):
        raise RuntimeError("se rompió algo adentro")

    monkeypatch.setattr("app.web.skill.skill.handle", explota)

    response = await hablar(client, sobre(intent="LeerListaIntent", token=vinculado))

    assert response.status_code == 200
    assert "complic" in dijo(response)


async def test_la_puerta_de_acceso_no_tapa_el_skill(client, s3, vinculado, monkeypatch):
    """Amazon no tiene cómo presentar la `ACCESS_KEY`.

    Sin la exención el skill contesta 401 a todo y el síntoma es mudo: Alexa dice
    "hubo un problema" y no hay nada en los logs que lo explique.
    """
    monkeypatch.setattr(settings, "ACCESS_KEY", "una-clave-de-acceso")

    response = await hablar(client, sobre(intent="LeerListaIntent", token=vinculado))

    assert response.status_code == 200


async def test_la_puerta_de_acceso_no_tapa_el_authorize(client, monkeypatch):
    monkeypatch.setattr(settings, "ACCESS_KEY", "una-clave-de-acceso")

    response = await client.get("/oauth/alexa/authorize")

    # 400 porque falta el client_id, no 401 por la puerta.
    assert response.status_code == 400


# --------------------------------------- el estado que ve la app (GET /status)


@pytest_asyncio.fixture
def linking_configurado(monkeypatch):
    """`/status` mira los dos lados: sin account linking no hay cómo vincular."""
    monkeypatch.setattr(settings, "ALEXA_LINK_CLIENT_ID", "id")
    monkeypatch.setattr(settings, "ALEXA_LINK_CLIENT_SECRET", "secreto")
    monkeypatch.setattr(settings, "ALEXA_LINK_REDIRECT_URIS", ["https://layla.amazon.com/x"])


async def test_status_sin_vinculo(client, linking_configurado):
    response = await client.get("/api/v1/alexa/status")

    assert response.status_code == 200
    assert response.json() == {
        "configured": True,
        "linked": False,
        "linked_at": None,
        "last_used_at": None,
        "devices": 0,
    }


async def test_status_con_vinculo_no_devuelve_tokens(
    client, linking_configurado, vinculado
):
    response = await client.get("/api/v1/alexa/status")

    body = response.json()
    assert body["linked"] is True
    assert body["devices"] == 1
    # Con zona horaria explícita: un ISO sin ella lo lee el navegador como hora
    # local y corre la fecha para quien vinculó cerca de la medianoche.
    assert body["linked_at"].endswith(("Z", "+00:00"))
    assert vinculado not in response.text


async def test_status_sin_configurar(client, session, user):
    """Sin los secrets no hay nada que explicarle al usuario: la pantalla dice
    que no está disponible en vez de mandarlo a la app de Alexa a buscar un skill
    que no va a poder vincular."""
    response = await client.get("/api/v1/alexa/status")

    assert response.json()["configured"] is False


async def test_status_cuenta_los_vinculos(client, linking_configurado, session, user):
    tokens = AlexaSkillTokenRepository(session)
    await tokens.issue(user.id)
    await tokens.issue(user.id)
    await session.commit()

    assert (await client.get("/api/v1/alexa/status")).json()["devices"] == 2


async def test_desvincular_borra_todos(client, session, user, vinculado):
    tokens = AlexaSkillTokenRepository(session)
    await tokens.issue(user.id)
    await session.commit()

    assert (await client.delete("/api/v1/alexa/link")).status_code == 204
    assert await tokens.for_user(user.id) == []

    # Idempotente: apretar dos veces no es un error, el resultado pedido ya está.
    assert (await client.delete("/api/v1/alexa/link")).status_code == 204


async def test_desvincular_no_toca_a_otro_usuario(client, session, user, vinculado):
    from app.core.security import hash_password
    from app.db.repositories import UserRepository

    otro = await UserRepository(session).create("otro@ejemplo.com", hash_password("x" * 12))
    await AlexaSkillTokenRepository(session).issue(otro.id)
    await session.commit()

    await client.delete("/api/v1/alexa/link")

    assert len(await AlexaSkillTokenRepository(session).for_user(otro.id)) == 1


async def test_despues_de_desvincular_alexa_pide_vincular(
    client, s3, session, user, vinculado
):
    """El circuito completo: el usuario desvincula desde la app y el Echo se
    entera en la frase siguiente."""
    await client.delete("/api/v1/alexa/link")

    response = await hablar(client, sobre(intent="LeerListaIntent", token=vinculado))
    assert response.json()["response"]["card"] == {"type": "LinkAccount"}
