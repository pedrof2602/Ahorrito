"""Verificar que un request al skill lo mandó Amazon y no cualquiera.

`/alexa/skill` es un endpoint público, sin cookie ni clave, que escribe en la
lista de compras de un usuario. Lo único que lo separa de un `curl` es lo que se
comprueba acá.

Amazon firma el cuerpo de cada request con una clave privada y publica el
certificado en S3. La verificación son cuatro cosas, y **las cuatro hacen falta**:

1. El certificado se bajó de una URL que solo Amazon controla. Sin esto, el
   atacante firma con su propia clave y adjunta su propio certificado.
2. El certificado es de Amazon (`echo-api.amazon.com` en los SAN), está vigente,
   y encadena hasta una raíz de confianza.
3. La firma cierra contra el cuerpo exacto que llegó.
4. El `timestamp` del cuerpo es de recién. Sin esto, un request legítimo
   capturado hoy se puede reproducir mañana, firma y todo: la firma sigue
   siendo válida porque el cuerpo no cambió.

El chequeo del `applicationId` no está acá sino en `web/skill.py`, porque no es
criptografía: la firma de Amazon es la misma para todos los skills del mundo, y
sin comparar el ID cualquier otro developer podría postear acá con una firma
perfectamente válida.
"""

from __future__ import annotations

import base64
import logging
import posixpath
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.x509.oid import ExtensionOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

from app.db.tables import as_aware, utcnow

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "Signature-256"
"""SHA-256. Existe también `Signature`, que es SHA-1 y está desaconsejado por
Amazon; no se acepta a propósito, porque aceptar los dos deja al atacante elegir
el más débil."""

CERT_CHAIN_HEADER = "SignatureCertChainUrl"

CERT_HOSTNAME = "s3.amazonaws.com"
CERT_PATH_PREFIX = "/echo.api/"
CERT_SAN = "echo-api.amazon.com"

TIMESTAMP_TOLERANCE_S = 150
"""Los 150 segundos que pide Amazon. Es la ventana en la que un request
capturado todavía se puede reproducir, así que no conviene agrandarla "por las
dudas del reloj": si los relojes están mal, lo que hay que arreglar es el
reloj."""

_TIMEOUT_S = 10.0

_cert_cache: dict[str, rsa.RSAPublicKey] = {}
"""Clave pública por URL de certificado.

Sin caché sería una descarga desde S3 por cada frase que diga el usuario, con
una persona esperando la respuesta. Amazon rota el certificado cada tanto y la
URL cambia cuando lo hace, así que la entrada vieja deja de usarse sola; el
diccionario no crece porque las URLs posibles son un puñado.

Se cachea la clave y no el certificado: la validez ya se comprobó al bajarlo, y
guardar el objeto entero invitaría a re-chequearla en otro lado.
"""


@lru_cache(maxsize=1)
def _trusted_roots() -> list[x509.Certificate]:
    """Las CA públicas, del bundle de `certifi`.

    Se parsea una sola vez: son unas 150 y hacerlo por request costaría más que
    todo el resto de la verificación junta. `certifi` y no el almacén del sistema
    porque la imagen de producción es un `python:slim` donde el almacén puede
    estar vacío, y ahí esto fallaría en el deploy y no acá.
    """
    return x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes())


class SignatureError(Exception):
    """El request no se pudo verificar. Nunca dice cuál de los pasos falló en la
    respuesta HTTP: eso va al log, no al que golpea la puerta."""


def _validate_cert_url(url: str) -> str:
    """Las reglas de Amazon para la URL del certificado, en orden.

    Se normaliza **antes** de comparar porque
    `https://s3.amazonaws.com/echo.api/../../elsewhere/cert.pem` empieza con
    `/echo.api/` si uno mira el string pelado, y apunta a otro lado si uno
    resuelve los `..`. Es el bug clásico de este chequeo.
    """
    parts = urlsplit(url)

    if parts.scheme.lower() != "https":
        raise SignatureError("El certificado no viene por https.")

    if parts.hostname is None or parts.hostname.lower() != CERT_HOSTNAME:
        raise SignatureError("El certificado no viene de S3.")

    # `parts.port` levanta ValueError si el puerto no es un número.
    try:
        port = parts.port
    except ValueError as exc:
        raise SignatureError("Puerto inválido en la URL del certificado.") from exc
    if port is not None and port != 443:
        raise SignatureError("El certificado no viene del puerto 443.")

    # `normpath` resuelve `..` y colapsa las barras duplicadas. Le come la barra
    # final, que acá no importa: lo que se compara es el prefijo.
    path = posixpath.normpath(parts.path)
    if not path.startswith(CERT_PATH_PREFIX):
        # Case-sensitive, al revés que el esquema y el host: así lo especifica
        # Amazon, y S3 efectivamente distingue mayúsculas en la clave del objeto.
        raise SignatureError("El certificado no está bajo /echo.api/.")

    return f"https://{CERT_HOSTNAME}{path}"


def _public_key(pem: bytes) -> rsa.RSAPublicKey:
    """Valida la cadena que bajó de S3 y devuelve la clave con la que verificar.

    La cadena viene como varios PEM concatenados: primero el certificado que
    firma, después los intermedios hasta la raíz.
    """
    chain = x509.load_pem_x509_certificates(pem)
    if not chain:
        raise SignatureError("La cadena de certificados está vacía.")

    leaf = chain[0]

    now = utcnow()
    if not (as_aware(leaf.not_valid_before_utc) <= now <= as_aware(leaf.not_valid_after_utc)):
        raise SignatureError("El certificado de Amazon está vencido o no vigente.")

    try:
        san = leaf.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        ).value
        names = san.get_values_for_type(x509.DNSName)  # type: ignore[attr-defined]
    except x509.ExtensionNotFound as exc:
        raise SignatureError("El certificado no tiene SAN.") from exc

    if CERT_SAN not in names:
        raise SignatureError(f"El certificado no es de {CERT_SAN}.")

    # La cadena tiene que llegar hasta una raíz de confianza pública.
    #
    # Podría parecer redundante —el certificado se bajó de una URL de Amazon por
    # https, que ya se comprobó arriba— y no lo es: sin este paso, cualquiera que
    # pudiera escribir un archivo bajo `s3.amazonaws.com/echo.api/` serviría un
    # certificado autofirmado con `echo-api.amazon.com` en los SAN y pasaría los
    # dos chequeos anteriores. Con esto tendría además que conseguir que una CA
    # pública se lo firme, que es lo que no puede hacer.
    try:
        verifier = (
            PolicyBuilder()
            .store(Store(_trusted_roots()))
            .build_server_verifier(x509.DNSName(CERT_SAN))
        )
        verifier.verify(leaf, chain[1:])
    except (VerificationError, ValueError) as exc:
        raise SignatureError("La cadena de certificados no cierra.") from exc

    key = leaf.public_key()
    if not isinstance(key, rsa.RSAPublicKey):
        raise SignatureError("El certificado no tiene una clave RSA.")
    return key


async def _fetch_key(url: str) -> rsa.RSAPublicKey:
    cached = _cert_cache.get(url)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("No se pudo bajar el certificado de Alexa (%s)", type(exc).__name__)
        raise SignatureError("No se pudo bajar el certificado.") from exc

    key = _public_key(response.content)
    _cert_cache[url] = key
    return key


def check_timestamp(timestamp: str | None) -> None:
    """El `timestamp` del cuerpo, dentro de la ventana de tolerancia.

    Es lo único que impide reproducir un request viejo: la firma de un cuerpo
    capturado sigue siendo válida para siempre, porque el cuerpo no cambia.

    Se acepta también un timestamp levemente en el futuro —la misma tolerancia
    hacia los dos lados— porque el reloj de Amazon y el nuestro no están
    sincronizados y un par de segundos de adelanto es normal.
    """
    if not timestamp:
        raise SignatureError("El request no trae timestamp.")

    try:
        sent = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SignatureError("Timestamp con formato inválido.") from exc

    if abs(as_aware(sent) - utcnow()) > timedelta(seconds=TIMESTAMP_TOLERANCE_S):
        raise SignatureError("El request es viejo o el reloj está corrido.")


async def verify(body: bytes, *, signature: str | None, cert_url: str | None) -> None:
    """Verifica el request completo. No devuelve nada: o pasa, o levanta.

    `body` tiene que ser **los bytes exactos que llegaron**, no un JSON
    re-serializado a partir del objeto parseado. Cualquier diferencia de espacios
    o de orden de claves cambia el hash y tira todo abajo, con un error que
    parece un problema de claves y no de bytes.
    """
    if not signature or not cert_url:
        raise SignatureError("Faltan las cabeceras de firma.")

    key = await _fetch_key(_validate_cert_url(cert_url))

    try:
        raw_signature = base64.b64decode(signature, validate=True)
    except (ValueError, TypeError) as exc:
        raise SignatureError("La firma no es base64 válido.") from exc

    try:
        key.verify(raw_signature, body, padding.PKCS1v15(), SHA256())
    except InvalidSignature as exc:
        raise SignatureError("La firma no corresponde al cuerpo del request.") from exc
