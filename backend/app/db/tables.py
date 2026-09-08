"""Esquema de la base de datos.

El dinero se guarda como entero en centavos: SQLite no tiene decimal nativo y
los float acumulan drift a lo largo del histórico.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_aware(value: datetime) -> datetime:
    """Le devuelve el `tzinfo` a un datetime que salió de SQLite.

    SQLite no tiene tipo con zona horaria: guarda el texto y descarta el offset,
    así que un `DateTime(timezone=True)` vuelve naive. Compararlo contra un
    `utcnow()` aware tira `TypeError`, y esas comparaciones son justo las que
    deciden si un precio caducó o si una sesión venció.

    Se asume UTC porque todo lo que se escribe sale de `utcnow()`. Vive acá y no
    en quien lo necesita porque es una propiedad del almacenamiento, no de la
    lógica que lo consume.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


OWNER_USER_ID = 1
"""El dueño original, de cuando la app no tenía login.

Todo lo que se cargó antes de la autenticación quedó con `user_id = 1`. La
migración que crea `users` reserva ese id para la cuenta del dueño en vez de
dejar autoincrementar, y así los domicilios, tarjetas y listas que ya existían
siguen teniendo dueño sin migrar una sola fila de datos.

No es un default: ningún repositorio cae acá si te olvidás de pasar el usuario.
Existe únicamente para que la migración sepa a qué id atar la cuenta vieja."""


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class User(Base):
    """Una cuenta.

    El email se guarda siempre en minúsculas y con los espacios comidos: quien
    se registró como `Pedro@Gmail.com ` espera poder entrar escribiendo
    `pedro@gmail.com`, y si la normalización viviera solo en el endpoint de
    login, el índice único dejaría crear las dos cuentas.

    `password_hash` guarda un hash de Argon2id, que ya trae la sal adentro. Una
    cuenta sin contraseña usable —la del dueño después de la migración— lleva el
    centinela `!`, que no es un hash válido y por lo tanto nunca verifica.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")
    """`user` o `admin`. Hoy todas las cuentas son `user` y ningún endpoint pide
    `admin`; la columna existe para que separar permisos más adelante no obligue
    a migrar el esquema ni a invalidar las sesiones abiertas."""

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    """Suspender sin borrar. Un borrado dejaría huérfanos los domicilios y las
    listas, que cuelgan de `user_id` sin FK."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


class AuthSession(Base):
    """Una sesión abierta: el equivalente server-side de estar logueado.

    Guarda el **hash** del token, no el token. Es la misma razón por la que no se
    guarda la contraseña en claro: quien consiga leer la base —un backup filtrado
    como los `compras.db.bak` que casi terminan en el repo— no puede usar lo que
    encuentra para hacerse pasar por nadie.

    Se eligió esto en vez de JWT porque acá la revocación importa más que no
    tocar la base: un JWT sigue siendo válido hasta que expira, así que "cerrar
    sesión" no cierra nada y cambiar la contraseña no echa a quien te robó el
    token. El costo es un `SELECT` por request sobre un índice único.
    """

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    """SHA-256 en hexa del token que viaja en la cookie.

    SHA-256 pelado y no Argon2: el token son 256 bits de `secrets`, no una
    contraseña elegida por una persona, así que no hay diccionario que probar y
    un hash lento solo agregaría latencia a cada request."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)
    """Para que "cerrar las otras sesiones" muestre cuáles son. Se trunca: es un
    header que manda el cliente y no hay motivo para guardar 8 KB de él."""


class AlexaLink(Base):
    """La cuenta de Amazon que un usuario vinculó, y las llaves para usarla.

    Es la tabla que más se parece a `AuthSession` y la que más se diferencia de
    ella: las dos guardan credenciales, pero acá los tokens van **cifrados y no
    hasheados**. Un hash sirve para comparar contra lo que alguien presenta; un
    token de Amazon hay que mandarlo tal cual en cada llamada a la API de listas,
    así que tiene que poder volver en claro. Ver `core/crypto.py`.

    Nada de esto entra en un log ni sale por un endpoint: la API contesta si hay
    vínculo y desde cuándo, nunca con qué.
    """

    __tablename__ = "alexa_links"

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    """Una cuenta de la app vincula una cuenta de Amazon, y el índice único lo
    garantiza en la base y no en el código: volver a vincular pisa la fila que
    ya está en lugar de dejar dos tokens vivos para el mismo usuario.

    `CASCADE` al revés que en `Address` o `ShoppingList`, que a propósito no
    tienen FK para no quedar huérfanas si la cuenta se suspende. La diferencia
    es qué es cada cosa: un domicilio huérfano es un dato de más, un token vivo
    sin dueño es un permiso de escritura sobre las listas de alguien que ya no
    está."""

    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    """Cifrados con Fernet. `Text` y no `String(n)` porque el ciphertext crece
    con el largo del token y Amazon no documenta un techo: recortarlo a mano
    sería guardar algo que no descifra."""

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """Cuándo vence el `access_token`, no el vínculo.

    El `refresh_token` no vence salvo que el usuario revoque el permiso, así que
    esta fecha se cruza a cada rato y es normal: dispara un refresco, no un
    "vinculá de nuevo"."""

    scope: Mapped[str] = mapped_column(String(200))
    """Los permisos que Amazon terminó otorgando, que no siempre son los que se
    pidieron. Guardarlo es lo que permite detectar más adelante que un vínculo
    viejo no tiene el scope de escritura, sin ir a preguntárselo a Amazon."""

    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    def __repr__(self) -> str:
        """Sin los tokens, ni siquiera recortados.

        El `__repr__` que genera SQLAlchemy no imprime columnas, pero un
        `log.debug("%r", row)` en el archivo equivocado sí imprimiría este, y
        una línea acá cierra esa puerta para siempre.
        """
        return f"<AlexaLink user_id={self.user_id} expires_at={self.expires_at}>"


class Chain(Base):
    """Cadena de supermercados."""

    __tablename__ = "chains"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    supports_store_prices: Mapped[bool] = mapped_column(Boolean, default=False)

    stores: Mapped[list[Store]] = relationship(back_populates="chain")


class Store(Base):
    """Sucursal o canal de venta de una cadena.

    No toda fila es un lugar físico: en las cadenas que regionalizan por canal de
    venta, una "sucursal" es un canal y no tiene dónde ir. Por eso las
    coordenadas son opcionales y una fila sin ellas no es un error — es una fila
    que no se puede poner en un mapa.
    """

    __tablename__ = "stores"
    __table_args__ = (UniqueConstraint("chain_id", "external_id", name="uq_store_chain_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    sales_channel: Mapped[int | None] = mapped_column(Integer, default=None)
    region_id: Mapped[str | None] = mapped_column(String(512), default=None)
    postal_code: Mapped[str | None] = mapped_column(String(16), default=None, index=True)

    # --- ubicación ---------------------------------------------------------
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    address: Mapped[str | None] = mapped_column(String(300), default=None)
    city: Mapped[str | None] = mapped_column(String(120), default=None)
    province: Mapped[str | None] = mapped_column(String(120), default=None)
    location_postal_code: Mapped[str | None] = mapped_column(
        String(16), default=None, index=True
    )
    """El CP **de la sucursal**, distinto de `postal_code`.

    `postal_code` significa "esta sucursal sirve a esta zona", que es lo que
    contesta la resolución de región y puede abarcar media ciudad. Este es dónde
    está parado el local. Mezclarlos haría que buscar la sucursal más cercana a
    un CP devuelva cualquiera de las que atienden esa zona.

    Sirve además para ubicar al usuario sin geocodificar: el centro del mapa sale
    del promedio de las sucursales que comparten su CP.
    """

    location_source: Mapped[str | None] = mapped_column(String(16), default=None)
    """De dónde salieron las coordenadas: `api` o `geocoded`.

    Mismo criterio que `bin_source` y `price_scope`: una coordenada que publicó
    la cadena y una que salió de geocodificar un texto no valen lo mismo —la
    segunda puede caer a media cuadra o en la esquina equivocada— y la UI tiene
    que poder decirlo en vez de presentarlas como si fueran la misma cosa.
    """
    located_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    chain: Mapped[Chain] = relationship(back_populates="stores")


class GeocodeCache(Base):
    """Direcciones ya resueltas a coordenadas.

    Nominatim admite **un request por segundo** y pide identificarse: no es una
    API para consultar en caliente. Con esta tabla cada dirección se geocodifica
    una sola vez en la vida de la app, y el código postal del usuario también.

    Una consulta que no resolvió se guarda igual, con las coordenadas en null:
    sin eso, una dirección que Nominatim no conoce se reintentaría en cada
    corrida del script, gastando el segundo de espera para volver a no obtener
    nada.
    """

    __tablename__ = "geocode_cache"

    query: Mapped[str] = mapped_column(String(300), primary_key=True)
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    display_name: Mapped[str | None] = mapped_column(String(500), default=None)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Product(Base):
    """Producto canónico, compartido entre cadenas.

    Se identifica por EAN. Los SKU sin EAN confiable (frescos, fraccionables)
    quedan sin vincular hasta que el matcheo por nombre los resuelva.
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    ean: Mapped[str | None] = mapped_column(String(14), unique=True, index=True, default=None)
    name: Mapped[str] = mapped_column(String(400))
    brand: Mapped[str | None] = mapped_column(String(200), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChainSku(Base):
    """Un SKU tal como lo publica una cadena."""

    __tablename__ = "chain_skus"
    __table_args__ = (UniqueConstraint("chain_id", "sku_id", name="uq_sku_chain_sku"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    sku_id: Mapped[str] = mapped_column(String(64))
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id"), index=True, default=None
    )
    external_product_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(400))
    brand: Mapped[str | None] = mapped_column(String(200), default=None)
    ean: Mapped[str | None] = mapped_column(String(14), index=True, default=None)
    image_url: Mapped[str | None] = mapped_column(String(1000), default=None)
    link: Mapped[str | None] = mapped_column(String(1000), default=None)
    measurement_unit: Mapped[str | None] = mapped_column(String(16), default=None)
    unit_multiplier: Mapped[float] = mapped_column(default=1.0)
    category_path: Mapped[list | None] = mapped_column(default=None)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PriceSnapshot(Base):
    """Un precio que rigió durante un intervalo.

    No es un volcado por corrida: solo se inserta una fila cuando el precio, la
    disponibilidad o las promos cambian respecto de la última observación. Cada
    fila significa "esto valió desde `observed_at` hasta `last_seen_at`", que es
    justo el intervalo que necesita un gráfico de evolución, y evita que un
    crawl diario multiplique la base sin agregar información.
    """

    __tablename__ = "price_snapshots"
    __table_args__ = (
        Index("ix_snapshot_lookup", "chain_sku_id", "store_id", "observed_at"),
        Index("ix_snapshot_observed", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chain_sku_id: Mapped[int] = mapped_column(ForeignKey("chain_skus.id"), index=True)
    store_id: Mapped[int | None] = mapped_column(
        ForeignKey("stores.id"), index=True, default=None
    )
    seller_id: Mapped[str | None] = mapped_column(String(64), default=None)

    price_cents: Mapped[int] = mapped_column(Integer)
    reference_price_cents: Mapped[int | None] = mapped_column(Integer, default=None)
    price_per_unit_cents: Mapped[int | None] = mapped_column(Integer, default=None)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    available_quantity: Mapped[int] = mapped_column(Integer, default=0)
    price_scope: Mapped[str] = mapped_column(String(16), default="national")

    promotions: Mapped[list | None] = mapped_column(default=None)
    promo_digest: Mapped[str] = mapped_column(String(64), default="")
    """Huella de las promos, para detectar cambios sin comparar JSON."""

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PriceSearchCache(Base):
    """Respuesta de búsqueda de una cadena, tal como se la va a devolver.

    Es una tabla aparte de `price_snapshots` y no una vista de ella a propósito.
    Las dos guardan precios, pero contestan preguntas distintas: el histórico
    responde "cuánto valió este SKU a lo largo del tiempo" y solo inserta cuando
    algo cambia, así que no sabe *cuándo se preguntó por última vez* ni qué
    productos devolvió una búsqueda. Esta responde "qué contestó la cadena para
    este término", que es lo único que permite saltear el request.

    Guarda las ofertas ya mapeadas y no el JSON crudo de la cadena: si se
    guardara el crudo habría que volver a mapear en cada acierto, y un cambio en
    el mapper dejaría la caché con un formato que el código ya no entiende.
    """

    __tablename__ = "price_search_cache"
    __table_args__ = (Index("ix_search_cache_purge", "chain_slug", "fetched_at"),)

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    """SHA-256 de (cadena, término normalizado, canal, sucursal).

    Hasheado y no compuesto porque el término es texto libre del usuario: como
    clave primaria compuesta obligaría a acotarlo a un largo arbitrario y a
    lidiar con su collation. Las columnas legibles de al lado quedan para poder
    inspeccionar la caché a ojo.
    """

    chain_slug: Mapped[str] = mapped_column(String(64), index=True)
    term: Mapped[str] = mapped_column(String(300))
    sales_channel: Mapped[int | None] = mapped_column(Integer, default=None)
    store_key: Mapped[str | None] = mapped_column(String(128), default=None)

    limit: Mapped[int] = mapped_column(Integer, default=0)
    """Cuántos resultados se pidieron cuando se llenó esta fila.

    Se guarda porque el límite **no** entra en la clave: una entrada de 50
    resultados sirve para responder un pedido de 20 recortándola, y meterlo en la
    clave partiría la caché en una fila por cada límite distinto que use el
    frontend. Al revés no vale, y por eso hay que saberlo: una entrada de 20 no
    puede contestar un pedido de 50 sin inventar los 30 que faltan.
    """

    offers: Mapped[list] = mapped_column(default=list)
    """`ProductOffer` serializados, en el orden de relevancia que dio la cadena.

    El orden importa y por eso se guarda la lista entera: el matcheo de canastas
    usa la posición como señal de relevancia (`basket._index`), así que reordenar
    o deduplicar acá cambiaría qué producto representa a una línea.
    """

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    """Cuándo se le preguntó a la cadena. El TTL se mide contra esto."""


class PaymentInstrument(Base):
    """Un medio de pago que el usuario declaró tener.

    **No guarda el número de tarjeta.** `bins` son los primeros 6 a 8 dígitos, que
    identifican al emisor y no a la persona, y son lo único que la simulación
    necesita. No hay columna para el PAN completo, vencimiento ni titular porque
    ninguno hace falta para calcular un precio.
    """

    __tablename__ = "payment_instruments"
    __table_args__ = (
        UniqueConstraint("user_id", "label", name="uq_instrument_user_label"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    label: Mapped[str] = mapped_column(String(120))
    issuer_slug: Mapped[str] = mapped_column(String(64), index=True)
    brand: Mapped[str | None] = mapped_column(String(32), default=None)
    kind: Mapped[str] = mapped_column(String(24), default="credit")
    rails: Mapped[list] = mapped_column(default=list)
    bins: Mapped[list] = mapped_column(default=list)
    bin_source: Mapped[str] = mapped_column(String(16), default="catalog")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    verified_promotions: Mapped[list | None] = mapped_column(default=None)
    """Promos que el BIN activó la última vez que se probó contra el checkout.
    Es lo que convierte `bin_source` en `verified`."""


class UserProfile(Base):
    """Los datos del usuario: dónde compra, cómo paga y quién es.

    Fila única mientras no haya login —la PK *es* el `user_id`, no hay
    autoincremental—, y el repositorio la crea con defaults la primera vez que
    alguien la pide. Así no hace falta un seed ni una migración de datos: una
    base recién creada y una que ya venía andando se comportan igual.

    El código postal y el canal no son preferencias cosméticas: resuelven la
    sucursal y deciden qué promos bancarias aplican. Vivían en `localStorage`, y
    eso los hacía por dispositivo.
    """

    __tablename__ = "user_profile"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)

    postal_code: Mapped[str | None] = mapped_column(String(16), default=None)
    channel: Mapped[str] = mapped_column(String(16), default="in_store")
    sales_channel: Mapped[int | None] = mapped_column(Integer, default=None)
    theme: Mapped[str] = mapped_column(String(16), default="system")

    full_name: Mapped[str | None] = mapped_column(String(200), default=None)
    dni: Mapped[str | None] = mapped_column(String(20), default=None)
    email: Mapped[str | None] = mapped_column(String(254), default=None)
    phone: Mapped[str | None] = mapped_column(String(40), default=None)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Address(Base):
    """Un domicilio guardado.

    Separado del perfil porque son varios (casa, trabajo) y el perfil es uno
    solo. `postal_code` se repite acá a propósito: el del perfil es el que usa la
    comparación, y el del domicilio es el de esa dirección; unificarlos haría que
    cargar la dirección del trabajo cambie los precios que ves.
    """

    __tablename__ = "addresses"
    __table_args__ = (UniqueConstraint("user_id", "label", name="uq_address_user_label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    label: Mapped[str] = mapped_column(String(120))

    street: Mapped[str | None] = mapped_column(String(200), default=None)
    number: Mapped[str | None] = mapped_column(String(20), default=None)
    """Texto y no entero: existen el "s/n" y el "1234 bis"."""
    floor: Mapped[str | None] = mapped_column(String(20), default=None)
    apartment: Mapped[str | None] = mapped_column(String(20), default=None)
    city: Mapped[str | None] = mapped_column(String(120), default=None)
    province: Mapped[str | None] = mapped_column(String(120), default=None)
    postal_code: Mapped[str | None] = mapped_column(String(16), default=None)
    notes: Mapped[str | None] = mapped_column(String(500), default=None)

    # --- ubicación ---------------------------------------------------------
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    """Dónde queda el domicilio, para ordenar las sucursales por cercanía real.

    Es opcional a propósito: un domicilio sirve igual sin coordenadas —para
    tenerlo anotado— y ubicarlo depende de una red que puede no estar. Sin
    coordenadas el mapa cae al promedio de las sucursales del código postal,
    que es lo que hacía antes de que esto existiera.
    """
    geo_source: Mapped[str | None] = mapped_column(String(16), default=None)
    """`geocoded` si la coordenada salió de buscar la dirección, `manual` si la
    marcaste vos sobre el mapa. La segunda es más confiable que la primera y por
    eso se distinguen: geocodificar una dirección del conurbano puede errarle de
    partido entero."""
    geo_label: Mapped[str | None] = mapped_column(String(300), default=None)
    """La dirección tal como la entendió el geocodificador.

    Se guarda para que puedas darte cuenta de que ubicó otra cosa: «AV BELGRANO
    950, Tres Arroyos» cuando vos querías Vicente López se ve de una.
    """

    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CustomStore(Base):
    """Una sucursal cargada a mano por el usuario.

    Tabla aparte de `stores` y no una fila más con `location_source='manual'`,
    por dos razones concretas:

    * `stores` la escriben los proveedores. `sync_stores` hace upsert por
      `(chain_id, external_id)` y `refresh_store_locations.py` matchea por
      nombre normalizado; una fila cargada a mano ahí puede quedar pisada por
      una corrida del script, y el trabajo manual del usuario es justamente lo
      que no se puede perder.
    * Una fila de `stores` es candidata a resolver precios. Estas no: no tienen
      `sellerId` ni número de sucursal en el catálogo de la cadena, así que
      contestan *dónde queda*, nunca *cuánto sale*.

    Cuelga de una cadena que la app compara. Cargar un súper que la app no sabe
    cotizar dejaría un marker sin precio al lado de otros con precio, y dos
    markers que significan cosas distintas es peor que no tener el marker.
    """

    __tablename__ = "custom_stores"
    __table_args__ = (
        UniqueConstraint("user_id", "chain_id", "name", name="uq_custom_store_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("chains.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))

    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    """Obligatorias, al revés que en `addresses`: una sucursal que no se puede
    poner en el mapa no cumple ninguna función acá."""

    address: Mapped[str | None] = mapped_column(String(300), default=None)
    city: Mapped[str | None] = mapped_column(String(120), default=None)
    province: Mapped[str | None] = mapped_column(String(120), default=None)
    postal_code: Mapped[str | None] = mapped_column(String(16), default=None)
    notes: Mapped[str | None] = mapped_column(String(500), default=None)
    geo_source: Mapped[str] = mapped_column(String(16), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    chain: Mapped[Chain] = relationship()


class ShoppingList(Base):
    """Una lista de compras guardada."""

    __tablename__ = "shopping_lists"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_list_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lines: Mapped[list[ShoppingListLine]] = relationship(
        back_populates="shopping_list",
        cascade="all, delete-orphan",
        order_by="ShoppingListLine.position",
        lazy="selectin",
    )
    """`selectin` y no lazy a secas: con `AsyncSession` un acceso perezoso fuera
    del `await` tira `MissingGreenlet`, y la lista siempre se lee entera."""


class ShoppingListLine(Base):
    """Una línea de la lista.

    Los límites de los campos son los mismos que valida el frontend antes de
    mandar (`sanitize()` en `useShoppingList.js`) y los que acepta `BasketLine`:
    si divergen, la lista se guarda pero la comparación la rechaza, y el error
    aparece lejos de acá disfrazado de "el súper no respondió".
    """

    __tablename__ = "shopping_list_lines"
    __table_args__ = (Index("ix_line_list_position", "list_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int] = mapped_column(
        ForeignKey("shopping_lists.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    query: Mapped[str] = mapped_column(String(120))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    ean: Mapped[str | None] = mapped_column(String(14), default=None)
    pinned_label: Mapped[str | None] = mapped_column(String(400), default=None)
    """Qué producto fijaste, para poder mostrarlo sin volver a buscarlo."""

    shopping_list: Mapped[ShoppingList] = relationship(back_populates="lines")


class PromoUsage(Base):
    """Cuánto se consumió del tope de una promo en una ventana.

    Sin esto, un tope mensual se calcula como si estuviera entero en cada compra
    y el ahorro informado es el de la primera compra del mes, repetido. La ventana
    se guarda por su fecha de inicio para que el vencimiento sea un cambio de
    clave y no un borrado.
    """

    __tablename__ = "promo_usage"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "rule_id", "period_start", name="uq_usage_user_rule_period"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    rule_id: Mapped[str] = mapped_column(String(64), index=True)
    period_start: Mapped[date] = mapped_column(Date)
    used_cents: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
