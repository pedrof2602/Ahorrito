import json
import logging
from pathlib import Path
from typing import Annotated, Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

log = logging.getLogger(__name__)

# Anclamos el .env al directorio de backend/ para que la config no dependa
# de desde dónde se lanzó uvicorn.
BACKEND_DIR = Path(__file__).resolve().parents[2]

Lista = Annotated[list[str], NoDecode]
"""Una lista de strings que se puede escribir de varias formas en el entorno.

`NoDecode` apaga el `json.loads()` que pydantic-settings le hace a los tipos
complejos **adentro del source**, antes de que corra ningún validador. Sin eso no
hay forma de atajar el error: un valor que no sea JSON levanta `SettingsError`
mientras se construyen los settings, y como `settings = Settings()` corre al
importar este módulo, la excepción mata al proceso antes de que exista la app.

Eso ya pasó: un `ALEXA_LINK_REDIRECT_URIS` cargado separado por comas dejó el
sitio entero devolviendo 502 —incluidos `/robots.txt` y el health check de Fly—
durante horas. La variable era de una feature opcional. **Ninguna variable de
entorno mal escrita tiene que poder hacer eso**, y menos una que sólo enciende
algo accesorio, así que el parseo de acá abajo no falla nunca: interpreta lo que
puede y avisa por log de lo que no.
"""


def parse_lista(value: Any) -> list[str]:
    """Un array JSON, o algo separado por comas o saltos de línea. Nunca levanta.

    El orden importa: primero JSON, porque es el formato que documenta
    `.env.example` y el que ya usa `fly.toml`; después la forma que la gente
    escribe cuando tipea una variable a mano.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        # El default declarado en código, que llega ya parseado.
        return [str(item).strip() for item in value if str(item).strip()]

    texto = str(value).strip()
    if not texto:
        return []

    if texto[0] in "[{":
        # Arranca con un delimitador de JSON: la intención era JSON. Si está roto
        # se devuelve vacío y **no** se intenta partir por comas, porque de un
        # array mal cerrado salen pedazos con corchetes y comillas pegados. Una
        # de estas listas es la whitelist de `redirect_uri` del proveedor OAuth:
        # vale más que quede vacía —y la feature apagada, que se nota— antes que
        # llena de entradas inservibles que aparentan estar configuradas.
        try:
            cargado = json.loads(texto)
        except ValueError:
            log.error("Config: el valor parece JSON pero no lo es: %r", texto)
            return []
        if not isinstance(cargado, list):
            log.error("Config: el valor es JSON pero no es una lista: %r", texto)
            return []
        return [str(item).strip() for item in cargado if str(item).strip()]

    return [pedazo.strip() for pedazo in texto.replace("\n", ",").split(",") if pedazo.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        case_sensitive=True,
        extra="ignore",
    )

    @field_validator("CORS_ORIGINS", "ENABLED_CHAINS", "ALEXA_LINK_REDIRECT_URIS", mode="before")
    @classmethod
    def _listas_tolerantes(cls, value: Any) -> list[str]:
        return parse_lista(value)

    PROJECT_NAME: str = "Compras API"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"

    # Vacío = SQLite en backend/compras.db. Se pisa para mover la base a un
    # servidor sin tocar código: postgresql+asyncpg://usuario:pass@host/compras
    DATABASE_URL: str = ""

    # CORS Origins permitidos
    CORS_ORIGINS: Lista = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
    ]

    # --- Proveedores de precios (VTEX y futuros) ---
    # Cadenas habilitadas por slug. Vacío = todas las registradas.
    ENABLED_CHAINS: Lista = []
    # Código postal por defecto para resolver sucursales cercanas.
    DEFAULT_POSTAL_CODE: str = "1425"
    HTTP_TIMEOUT_S: float = 20.0
    HTTP_USER_AGENT: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
    # Espaciado mínimo entre requests al mismo host (ms). Ser buen ciudadano:
    # medimos que aguantan 30 concurrentes, pero no hay motivo para exprimirlos.
    HTTP_MIN_INTERVAL_MS: int = 250
    HTTP_MAX_RETRIES: int = 3

    # --- Caché de precios ---------------------------------------------------
    PRICE_CACHE_ENABLED: bool = True

    PRICE_CACHE_TTL_S: int = 6 * 3600
    """Cuánto vale un precio antes de volver a preguntar.

    Seis horas y no doce: las promos bancarias son *por día de la semana* (ver
    `data/payment_promos.yaml`), así que una ventana larga puede servir a las 8
    del jueves un precio capturado el miércoles a la tarde, con una promo que ya
    no corre. Con seis horas la ventana nunca cruza dos días promocionales.
    """

    PRICE_CACHE_STALE_MAX_S: int = 7 * 24 * 3600
    """Techo del precio vencido que se sirve cuando la cadena no responde.

    Pasado esto se prefiere perder la cadena a mostrar un número que ya no
    significa nada: un precio de hace dos semanas no es una comparación, es una
    adivinanza con cara de dato.
    """

    # --- Depuración de candidatos (services/matching) -----------------------

    NAME_MATCH_MIN_SCORE: float = 85.0
    """Cuánto del término pedido tiene que aparecer en el nombre, de 0 a 100.

    Medido sobre pares reales del rubro, los matcheos correctos puntúan 99-100 y
    los equivocados 50-80, así que 85 cae en el hueco y no en una pendiente.
    Bajarlo a ~75 empieza a dejar pasar la otra variedad de la misma marca
    ("Leche Descremada" cuando se pidió entera): más candidatos, más ruido.
    """

    PRICE_OUTLIER_TOLERANCE_PCT: float = 20.0
    """Desvío tolerado respecto de la mediana antes de sospechar del precio."""

    PRICE_OUTLIER_MIN_SAMPLE: int = 3
    """Candidatos mínimos para animarse a descartar por precio.

    Con dos, la mediana es el punto medio y los dos se desvían lo mismo en
    direcciones opuestas: no distingue al que está mal. Por debajo de este
    número el filtro avisa pero no descarta.
    """

    STATIC_DIR: str = ""
    """Carpeta del frontend compilado, para servirlo desde el mismo server.

    Vacío = no se sirve nada estático, que es lo correcto en desarrollo: ahí el
    frontend lo sirve Vite con hot reload y este server es solo la API. En el
    deploy apunta al `dist/` que dejó el build.
    """

    # --- Puerta de acceso al deploy -----------------------------------------

    ACCESS_KEY: str = ""
    """Clave compartida que hay que presentar para que el server conteste algo.

    Vacío = apagada, que es lo correcto en desarrollo. Con valor, **todo** pide
    la clave: las páginas, la API y `/docs`.

    Es una capa distinta del login y no lo reemplaza. El login dice *quién sos* y
    protege tus datos; esto dice *si esta URL existe para vos* y es lo que hace
    que el deploy sea "por link directo": sin la clave no hay nada que mirar, ni
    formulario de login que tantear, ni `/docs` que enumerar, ni `/search` que
    usar como proxy gratis contra los supermercados.

    Ese último punto es el motivo real de que esto exista en el backend y no en
    el hosting: la protección por contraseña de Vercel o Netlify cubre las
    páginas y deja la API abierta.
    """

    ACCESS_COOKIE_NAME: str = "compras_access"
    """Dónde se recuerda la clave después de entrar por el link.

    Se separa de `COOKIE_NAME` a propósito: son dos permisos distintos y
    conviene poder revocar uno sin tirar las sesiones de nadie. Cambiar
    `ACCESS_KEY` invalida las dos cosas de una.
    """

    ACCESS_KEY_PARAM: str = "k"
    """Query param que instala la cookie: `https://…/?k=<clave>`.

    Es lo que hace que compartir el proyecto sea mandar un link y nada más.
    Contra la barra de direcciones de quien lo abre no protege —queda en su
    historial—, y eso está bien: la clave es para que no entre cualquiera, no
    para esconderse de la persona a la que se la diste.
    """

    # --- Autenticación ------------------------------------------------------

    REGISTRATION_OPEN: bool = True
    """Si cualquiera puede crearse una cuenta desde `POST /auth/register`.

    En false el endpoint contesta 403 y las cuentas las crea el admin. Es un
    interruptor y no una migración a propósito: si el día de mañana el deploy
    público junta gente que no esperabas, se cierra sin tocar código.
    """

    SESSION_TTL_S: int = 30 * 24 * 3600
    """Cuánto vive una sesión sin usarse, en segundos. 30 días.

    Es expiración deslizante: cada request corre el vencimiento hacia adelante,
    así que el reloj cuenta inactividad y no antigüedad. Quien entra todos los
    días no vuelve a ver el login nunca; quien desaparece un mes tiene que
    loguearse de nuevo.
    """

    SESSION_ABSOLUTE_TTL_S: int = 180 * 24 * 3600
    """Techo duro desde que la sesión se creó, en segundos. 180 días.

    Sin esto, la expiración deslizante nunca vence para un cliente que la toca
    seguido —incluido el que se robó la cookie—. El techo garantiza que toda
    sesión muere alguna vez.
    """

    COOKIE_NAME: str = "compras_session"

    COOKIE_SECURE: bool = True
    """`Secure`: la cookie solo viaja por HTTPS.

    Va en true por default porque el deploy es público. **En desarrollo hay que
    ponerlo en false** o el navegador no manda la cookie a `http://localhost` y
    el login parece andar (200 en `/auth/login`) pero la request siguiente da
    401. Es el error más molesto de este archivo, por eso el aviso al arrancar.
    """

    COOKIE_SAMESITE: str = "lax"
    """`lax`, `strict` o `none`.

    `lax` es el default correcto cuando el frontend se sirve del mismo sitio que
    la API, y de paso es lo que bloquea el CSRF: el navegador no manda la cookie
    en un POST que originó otro sitio. `none` (obligatorio si el frontend vive
    en otro dominio) apaga esa defensa y exige `COOKIE_SECURE=true`.
    """

    COOKIE_DOMAIN: str | None = None
    """Solo si el frontend está en un subdominio distinto de la API."""

    LOGIN_MAX_ATTEMPTS: int = 10
    LOGIN_ATTEMPT_WINDOW_S: int = 300
    """Intentos fallidos por IP antes de contestar 429, y en qué ventana.

    Diez en cinco minutos no molesta a nadie que se equivoque tipeando y sí
    arruina la fuerza bruta, que necesita miles. El contador vive en memoria del
    proceso: alcanza para un uvicorn solo, que es este deploy. Con varios
    workers cada uno cuenta por su lado y el techo real se multiplica; ahí hay
    que mover esto a Redis.
    """

    PASSWORD_MIN_LENGTH: int = 10
    """Largo mínimo. Sin reglas de "una mayúscula y un símbolo" a propósito:
    empujan a `Password1!` y el NIST las desaconseja desde 2017. El largo es lo
    que realmente cuesta romper."""

    # --- Login with Amazon (vínculo con Alexa) ------------------------------

    LWA_CLIENT_ID: str = ""
    """`Client ID` del Security Profile de Login with Amazon.

    Vacío = el vínculo con Alexa está apagado y el botón de la app avisa que
    falta configurarlo, en vez de mandar al usuario a una pantalla de Amazon que
    le va a dar error. Mismo criterio que `ACCESS_KEY`: la feature se enciende
    poniendo el valor, no tocando código.
    """

    LWA_CLIENT_SECRET: str = ""
    """`Client Secret` del mismo Security Profile.

    Nunca en `fly.toml`, que está versionado:

        fly secrets set LWA_CLIENT_SECRET="..."
    """

    LWA_REDIRECT_URI: str = ""
    """A dónde vuelve Amazon con el `code`, por ejemplo
    `https://ahorrito.fly.dev/auth/alexa/callback`.

    **Tiene que coincidir carácter por carácter con el `Allowed Return URL` del
    Security Profile**, incluido el esquema y la barra final. Amazon lo compara
    literal y ante la mínima diferencia contesta un `invalid_client` que no
    explica cuál de las dos cosas está mal.

    Es un setting y no una constante porque el valor correcto depende del
    dominio donde corra esto, y porque probar el flujo contra otro deploy no
    tiene que ser un cambio de código.
    """

    TOKEN_ENCRYPTION_KEY: str = ""
    """Clave Fernet con la que se cifran los tokens de Amazon en la base.

    Se genera una vez y se guarda como secret:

        fly secrets set TOKEN_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

    **Perderla o rotarla vuelve ilegibles todos los vínculos** y obliga a cada
    usuario a vincular de nuevo. No hay forma de recuperarlos: ese es el punto
    de que los tokens no estén en claro. Si alguna vez hay que rotarla, el
    camino es descifrar con la vieja y volver a cifrar con la nueva antes de
    cambiar el secret, no cambiarlo y ver qué pasa.

    Vacío = el vínculo con Alexa está apagado, igual que con `LWA_CLIENT_ID`.
    """

    # --- Skill de Alexa (Ahorrito como proveedor OAuth) ---------------------
    #
    # Acá el OAuth va **al revés** que en el bloque de arriba. En `LWA_*` esta
    # app es el cliente y Amazon el proveedor; en `ALEXA_*` esta app es el
    # proveedor y Alexa el cliente que viene a pedirle tokens.
    #
    # El motivo del cambio: la List Management REST API —la que dejaba leer y
    # escribir la lista de compras de Alexa desde afuera— la apagó Amazon el
    # 1 de julio de 2024. La única forma que queda de que "Alexa, agregá leche"
    # termine en esta base es un skill propio que Amazon invoca acá.

    ALEXA_SKILL_ID: str = ""
    """`amzn1.ask.skill.…`, el ID del skill en la consola de desarrollador.

    Se compara contra el `applicationId` que viene en cada request: sin eso,
    cualquier otro skill con una firma válida de Amazon —y la firma de Amazon es
    la misma para todos— podría postear acá y hablar como nuestros usuarios.

    Vacío = el skill está apagado. En ese estado `/alexa/skill` contesta 404 en
    vez de aceptar requests sin verificar, que es lo que pasaría si el chequeo
    de firma se salteara "porque no está configurado".
    """

    ALEXA_LINK_CLIENT_ID: str = ""
    """`client_id` que Alexa usa para identificarse contra nuestro `/token`.

    Lo inventamos nosotros —no lo da Amazon— y lo pegamos en la consola, en
    Account Linking. Puede ser cualquier string estable; no es secreto.
    """

    ALEXA_LINK_CLIENT_SECRET: str = ""
    """El secreto del par anterior. Este sí es secreto:

        fly secrets set ALEXA_LINK_CLIENT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

    Es lo único que separa a Alexa de cualquiera que descubra un `code`: sin él,
    un código robado del `redirect_uri` se canjea por un token de la cuenta.
    """

    ALEXA_LINK_REDIRECT_URIS: Lista = []
    """Las URLs de Amazon a las que se puede devolver el `code`, tal cual las
    muestra la consola en *Account Linking → Alexa Redirect URLs*. Son tres, una
    por región, y todas terminan en `/api/skill/link/<vendorId>`:

        ["https://layla.amazon.com/api/skill/link/XXXXXXXX",
         "https://pitangui.amazon.com/api/skill/link/XXXXXXXX",
         "https://alexa.amazon.co.jp/api/skill/link/XXXXXXXX"]

    **Es una whitelist y se compara literal.** Sin ella, `/oauth/alexa/authorize`
    aceptaría cualquier `redirect_uri` y alcanzaría con mandarle a un usuario un
    link con el `redirect_uri` del atacante para que el `code` —y con él la
    cuenta— termine en otro lado. Es el agujero clásico de un proveedor OAuth y
    la única defensa es no aceptar destinos que no estén en esta lista.

    **Va en `fly.toml`, no en `fly secrets`.** No son credenciales —son URLs de
    Amazon que sólo llevan el vendor ID— y tenerlas versionadas es lo que permite
    compararlas contra lo que manda Alexa cuando el vínculo falla. Como secret son
    un digest opaco que no se puede leer ni para depurar. Ojo con el orden de
    precedencia: **un secret con este nombre pisa al valor de `fly.toml`**, así
    que si alguna vez se cargó como secret hay que hacer `fly secrets unset`.

    Vacía = el account linking está apagado, y el sitio arranca igual. Ver
    `parse_lista()` arriba: el día que esto se cargó con un formato que no era
    JSON, el sitio entero se cayó.
    """

    ALEXA_TOKEN_TTL_S: int = 30 * 24 * 3600
    """Cuánto vale el `access_token` que le damos a Alexa. 30 días.

    Largo a propósito y compensado con un `refresh_token`: cada vencimiento es
    una ida y vuelta más contra este server, y del otro lado no hay una persona
    esperando sino un dispositivo que quiere contestar rápido.
    """

    ALEXA_CODE_TTL_S: int = 300
    """Cuánto vive el `code` de autorización. Cinco minutos.

    El código viaja en una URL —queda en logs, en el historial, en el Referer— y
    solo tiene que sobrevivir el salto del navegador a los servidores de Amazon,
    que son segundos. Es de un solo uso además de corto.
    """


settings = Settings()
