"""Medios de pago del usuario y reglas de promoción bancaria.

Separado de `catalog.py` porque no describe lo que publica una cadena sino lo que
el usuario tiene en la billetera y qué beneficios lo alcanzan.

La distinción que hace o rompe todo el módulo: **el emisor, el riel y el plástico
son tres cosas distintas.** Una promo de MODO no es "pagar con MODO", es pagar
con la tarjeta de un banco puntual *a través de* MODO; una de Cuenta DNI es
dinero en cuenta del Banco Provincia, sin plástico ni BIN. Sin separarlos, la
mitad de las promos argentinas no se pueden ni expresar.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.basket import BasketComparison, BasketLine
from app.models.catalog import Promotion


class PaymentRail(StrEnum):
    """Por dónde se enruta el pago. No es lo mismo que el emisor."""

    CARD = "card"
    """Plástico o checkout directo: el comercio ve la tarjeta."""

    MODO = "modo"
    """Billetera interbancaria. Por debajo hay una tarjeta de un banco."""

    MERCADO_PAGO = "mercado_pago"
    CUENTA_DNI = "cuenta_dni"
    """App del Banco Provincia. Paga con saldo, no con tarjeta."""


class CardKind(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"
    PREPAID = "prepaid"
    ACCOUNT_BALANCE = "account_balance"
    """Dinero en cuenta. No tiene BIN, así que nunca va a la simulación."""


class BinSource(StrEnum):
    """De dónde salió el BIN, que es cuánto se le puede creer."""

    CATALOG = "catalog"
    """De nuestra tabla de emisores. Puede no ser el de *tu* plástico."""

    USER = "user"
    """Lo cargó el usuario desde su tarjeta. Sin verificar todavía."""

    VERIFIED = "verified"
    """Se probó contra la API y activó una promo. Es el único medido."""


class BenefitType(StrEnum):
    DISCOUNT = "discount"
    """Descuento en el acto: pagás menos en la caja."""

    REINTEGRO = "reintegro"
    """Pagás todo y te devuelven después. Mismo ahorro, distinto desembolso hoy;
    confundirlos le arruina el presupuesto de la semana a quien lo usa."""


class CapPeriod(StrEnum):
    """Sobre qué ventana se cuenta el tope. Un tope mensual no es por compra."""

    PER_TRANSACTION = "per_transaction"
    DAILY = "daily"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"


class Channel(StrEnum):
    IN_STORE = "in_store"
    ONLINE = "online"


class PaymentConfidence(StrEnum):
    """Cuánto vale el número. Mismo criterio que `PriceScope` y `MatchConfidence`."""

    MEASURED = "measured"
    """Lo devolvió el checkout de la cadena simulando con este BIN."""

    ESTIMATED = "estimated"
    """Sale de una regla curada a mano. Es un cálculo nuestro, no un precio."""

    MIXED = "mixed"
    """Parte medido, parte estimado: una promo del super confirmada más una
    bancaria calculada encima."""


MIN_BIN_LENGTH = 6
MAX_BIN_LENGTH = 8
_BIN_RE = re.compile(rf"^\d{{{MIN_BIN_LENGTH},{MAX_BIN_LENGTH}}}$")

_WEEKDAY_NAMES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (0, ("mon", "monday", "lun", "lunes")),
    (1, ("tue", "tuesday", "mar", "martes")),
    (2, ("wed", "wednesday", "mie", "miercoles", "miércoles")),
    (3, ("thu", "thursday", "jue", "jueves")),
    (4, ("fri", "friday", "vie", "viernes")),
    (5, ("sat", "saturday", "sab", "sabado", "sábado")),
    (6, ("sun", "sunday", "dom", "domingo")),
)

_WEEKDAYS = {
    name: number for number, names in _WEEKDAY_NAMES for name in names
}
"""Nombres aceptados, completos y exactos.

**No se matchea por prefijo.** Truncar a tres letras hacía que `miercolez`
entrara como miércoles: un typo en el YAML quedaba cargado como una regla válida
para el día equivocado, que es la peor forma de fallar de un dato curado a mano.
"""


def normalize_bin(value: str) -> str:
    """Deja el BIN en dígitos y valida el largo.

    Se aceptan 6 a 8 dígitos: medido contra Carrefour, uno de 4 no solo no
    matchea sino que **rompe la simulación** —devuelve la respuesta sin `items`,
    indistinguible de "la sucursal no vende eso"—, así que dejarlo pasar
    convertiría un dato mal cargado en un total silenciosamente vacío.
    """
    digits = re.sub(r"\D", "", value or "")
    if not _BIN_RE.match(digits):
        raise ValueError(
            f"BIN inválido: {value!r}. Se esperan entre {MIN_BIN_LENGTH} y "
            f"{MAX_BIN_LENGTH} dígitos (los primeros de la tarjeta)."
        )
    return digits


def clean_bins(value: object) -> tuple[str, ...]:
    """Normaliza y deduplica una lista de BIN conservando el orden.

    La deduplicación no es cosmética: la propia lista `RestrictionsBins` de
    Carrefour repite BINs dentro de un mismo teaser.
    """
    if not value:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"Se esperaba una lista de BIN, llegó {type(value).__name__}")
    seen: dict[str, None] = {}
    for item in value:
        seen[normalize_bin(str(item))] = None
    return tuple(seen)


def parse_weekdays(values: list[str | int]) -> frozenset[int]:
    """Días como nombre (`mon`, `mie`) o número, a la convención de Python.

    0 es lunes, igual que `date.weekday()`, para que comparar sea directo.
    """
    parsed: set[int] = set()
    for value in values:
        if isinstance(value, int):
            if not 0 <= value <= 6:
                raise ValueError(f"Día fuera de rango: {value}")
            parsed.add(value)
            continue
        key = str(value).strip().lower()
        if key not in _WEEKDAYS:
            raise ValueError(
                f"Día desconocido: {value!r}. Se aceptan "
                f"{', '.join(names[0] for _, names in _WEEKDAY_NAMES)} "
                "(o su nombre completo, en inglés o castellano)."
            )
        parsed.add(_WEEKDAYS[key])
    return frozenset(parsed)


def period_start_for(cap_period: CapPeriod, day: date) -> date:
    """Inicio de la ventana en la que se acumula el tope.

    Guardar el consumo bajo esta fecha hace que el vencimiento del tope sea un
    cambio de clave y no un borrado programado: al cruzar el mes, la consulta
    simplemente no encuentra fila y el tope arranca entero.

    La quincena se corta el 1 y el 16, que es como la manejan los bancos que
    publican topes quincenales.
    """
    match cap_period:
        case CapPeriod.PER_TRANSACTION | CapPeriod.DAILY:
            return day
        case CapPeriod.WEEKLY:
            return day - timedelta(days=day.weekday())
        case CapPeriod.BIWEEKLY:
            return day.replace(day=1 if day.day <= 15 else 16)
        case CapPeriod.MONTHLY:
            return day.replace(day=1)


class PaymentInstrument(BaseModel):
    """Un medio de pago que el usuario declaró tener.

    **No guarda el número de tarjeta.** El BIN son los primeros 6 a 8 dígitos e
    identifica al emisor, no a la persona; es lo único que la API necesita para
    decidir si una promo aplica. No hay campo para el PAN completo, ni para
    vencimiento, ni para titular, porque ninguno hace falta.
    """

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    label: str = Field(..., min_length=1, examples=["Visa Galicia"])
    issuer_slug: str = Field(..., examples=["galicia", "banco-nacion"])
    brand: str | None = Field(None, examples=["visa", "mastercard"])
    kind: CardKind = CardKind.CREDIT
    rails: frozenset[PaymentRail] = frozenset({PaymentRail.CARD})
    bins: tuple[str, ...] = ()
    bin_source: BinSource = BinSource.CATALOG

    _normalize_bins = field_validator("bins", mode="before")(clean_bins)

    @model_validator(mode="after")
    def _balance_has_no_bin(self) -> PaymentInstrument:
        if self.kind is CardKind.ACCOUNT_BALANCE and self.bins:
            raise ValueError(
                "El dinero en cuenta no tiene BIN: no pasa por la red de tarjetas."
            )
        return self

    @property
    def simulatable(self) -> bool:
        """Si se le puede preguntar el precio real al checkout de la cadena."""
        return bool(self.bins)


class PaymentPromoRule(BaseModel):
    """Promo bancaria curada a mano.

    Existe porque **ninguna API las publica**: las financia el banco y se liquidan
    fuera del motor de promociones del super, así que no aparecen ni en los
    teasers del catálogo ni en la simulación del checkout. Son un dato manual, y
    el modelo lo declara —`verified_on`, `source_url`— en vez de disimularlo.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    issuer_slug: str
    percent: float = Field(..., gt=0, le=100)
    valid_from: date
    valid_to: date
    verified_on: date

    chain_slugs: frozenset[str] | None = Field(
        None, description="Cadenas alcanzadas. None = todas las soportadas."
    )
    rails: frozenset[PaymentRail] = frozenset({PaymentRail.CARD})
    card_kinds: frozenset[CardKind] = frozenset(
        {CardKind.CREDIT, CardKind.DEBIT}
    )
    weekdays: frozenset[int] = Field(
        frozenset(range(7)), description="0 = lunes. Vacío no, todos los días."
    )
    channels: frozenset[Channel] = frozenset({Channel.IN_STORE, Channel.ONLINE})

    cap_cents: int | None = Field(
        None, ge=0, description="Tope del ahorro. None = sin tope."
    )
    cap_period: CapPeriod = CapPeriod.MONTHLY
    benefit_type: BenefitType = BenefitType.DISCOUNT
    minimum_purchase_cents: int | None = Field(None, ge=0)

    stacks_with_provider_promos: bool = Field(
        False,
        description=(
            "Si se suma a las promos que la cadena ya aplicó. El default es no "
            "porque es lo que declaran los supers: Carrefour publica que sus "
            "promociones no son acumulables con reintegros bancarios. Asumir que "
            "sí acumula infla el ahorro y manda a comprar donde no conviene."
        ),
    )
    bins: tuple[str, ...] = Field(
        (), description="Si está, restringe a estos BIN además del emisor."
    )
    source_url: str | None = None
    notes: str | None = None
    unverified: bool = Field(
        False,
        description=(
            "La regla se cargó de memoria o de una fuente secundaria y nadie la "
            "contrastó todavía contra la página del banco. Viaja hasta la "
            "respuesta de la API: una promo inventada manda a comprar al super "
            "equivocado, y es peor que no tener promo."
        ),
    )

    _normalize_bins = field_validator("bins", mode="before")(clean_bins)

    @field_validator("weekdays", mode="before")
    @classmethod
    def _parse_weekdays(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return parse_weekdays(list(value))
        return value

    @model_validator(mode="after")
    def _check_dates(self) -> PaymentPromoRule:
        if self.valid_to < self.valid_from:
            raise ValueError(f"{self.id}: valid_to es anterior a valid_from")
        return self

    def is_valid_on(self, day: date) -> bool:
        """Si la regla está vigente y cae en un día que aplica."""
        return (
            self.valid_from <= day <= self.valid_to
            and day.weekday() in self.weekdays
        )

    def matches(
        self,
        instrument: PaymentInstrument,
        *,
        chain_slug: str,
        channel: Channel,
    ) -> bool:
        """Si este medio de pago califica para la regla en esa cadena y canal."""
        if self.chain_slugs is not None and chain_slug not in self.chain_slugs:
            return False
        if instrument.issuer_slug != self.issuer_slug:
            return False
        if instrument.kind not in self.card_kinds:
            return False
        if channel not in self.channels:
            return False
        # El riel importa: la misma Visa puede tener 25% por MODO y nada por POS.
        if not (instrument.rails & self.rails):
            return False
        if self.bins and not (set(instrument.bins) & set(self.bins)):
            return False
        return True


class AppliedBankPromo(BaseModel):
    """La regla curada que se eligió, con su aritmética a la vista."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    name: str
    percent: float
    benefit_type: BenefitType
    saving_cents: int = Field(..., ge=0, description="Ahorro ya acotado por el tope.")
    uncapped_saving_cents: int = Field(
        ..., ge=0, description="Lo que hubiera ahorrado sin tope."
    )
    cap_cents: int | None = None
    cap_remaining_cents: int | None = Field(
        None, description="Tope que quedaba disponible antes de esta compra."
    )
    cap_period: CapPeriod = CapPeriod.MONTHLY
    capped: bool = Field(
        False,
        description=(
            "El tope mordió: a partir de acá, agregar productos a la canasta no "
            "aumenta el descuento."
        ),
    )
    unverified: bool = False


class PaymentOption(BaseModel):
    """Qué pasa si comprás esta canasta en esta cadena con este medio de pago."""

    model_config = ConfigDict(frozen=True)

    chain_slug: str
    chain_display_name: str
    instrument: PaymentInstrument

    reference_total_cents: int = Field(
        ...,
        description=(
            "Lo que costaría sin ningún beneficio de medio de pago. Es la base "
            "contra la que se mide todo ahorro."
        ),
    )
    provider_total_cents: int = Field(
        ..., description="Total tras las promos que aplica la propia cadena."
    )
    provider_saving_cents: int = Field(
        0,
        ge=0,
        description=(
            "Ahorro atribuible a la tarjeta según el checkout de la cadena. Es la "
            "resta de dos simulaciones, no un porcentaje aplicado: las promos de "
            "VTEX no se acumulan y el motor elige una por ítem, así que aplicarle "
            "el porcentaje del teaser al total exagera el ahorro."
        ),
    )
    provider_promotions: tuple["Promotion", ...] = Field(
        (), description="Promos que el checkout activó con esta tarjeta."
    )
    bank_promo: AppliedBankPromo | None = None

    final_total_cents: int = Field(..., description="Lo que termina costando.")
    out_of_pocket_today_cents: int = Field(
        ...,
        description=(
            "Lo que sale del bolsillo en la caja. Difiere del total final cuando "
            "el beneficio es un reintegro: ahorrás igual, pero recién después."
        ),
    )
    total_saving_cents: int = Field(0, ge=0)
    confidence: PaymentConfidence
    caveats: list[str] = Field(default_factory=list)

    @property
    def is_reimbursement(self) -> bool:
        return self.out_of_pocket_today_cents > self.final_total_cents


class BinVerification(BaseModel):
    """Resultado de probar un BIN contra el checkout real de una cadena."""

    model_config = ConfigDict(frozen=True)

    chain_slug: str
    card_bin: str
    probe_sku_id: str = Field(..., description="Producto con el que se probó.")
    probe_product_name: str
    price_without_card_cents: int
    price_with_card_cents: int
    saving_cents: int = Field(..., description="Cuánto bajó por usar esta tarjeta.")
    promotions_unlocked: list[str] = Field(
        default_factory=list, description="Promos que activó el BIN y no estaban."
    )

    @property
    def unlocked_anything(self) -> bool:
        return self.saving_cents > 0 or bool(self.promotions_unlocked)


class BinVerificationResult(BaseModel):
    """Lo que se pudo comprobar del BIN, por cadena.

    Que no active nada **no** significa que el BIN esté mal: puede ser una tarjeta
    sin beneficios en esa cadena, o que la promo no corra hoy. Por eso el
    resultado informa qué se midió y no dictamina si la tarjeta es válida.
    """

    card_bin: str
    verified: bool = Field(
        ...,
        description=(
            "True solo si el BIN activó alguna promo en alguna cadena. Es lo "
            "único que se puede afirmar habiéndolo medido."
        ),
    )
    checks: list[BinVerification] = Field(default_factory=list)
    skipped_chains: list[str] = Field(
        default_factory=list,
        description="Cadenas sin checkout consultable, donde no hay nada que medir.",
    )
    message: str


class BestPaymentRequest(BaseModel):
    """Con qué conviene pagar esta canasta, y qué día."""

    lines: list[BasketLine] = Field(..., min_length=1, max_length=50)
    postal_code: str | None = Field(None, examples=["1425"])
    sales_channel: int | None = None
    chain_slugs: list[str] | None = None
    instrument_ids: list[int] | None = Field(
        None, description="Medios de pago a evaluar. None = todos los cargados."
    )
    on_date: date | None = Field(
        None,
        description=(
            "Día para el que se evalúan las promos bancarias. None = hoy. Las "
            "promos de la cadena, en cambio, salen siempre de simular hoy: el "
            "checkout no acepta consultas a futuro."
        ),
    )
    channel: Channel = Field(
        Channel.ONLINE,
        description=(
            "Muchas promos son solo presenciales o solo online, así que el canal "
            "cambia el resultado."
        ),
    )
    cap_used_cents: dict[str, int] | None = Field(
        None,
        description=(
            "Consumo ya hecho de cada tope, por id de regla. Pisa lo que tenga "
            "registrado la base. Útil mientras no se cargue el consumo real."
        ),
        examples=[{"bna-miercoles-supermercados": 1200000}],
    )
    fresh: bool = Field(
        False,
        description=(
            "Ignorar la caché de precios y consultar a las cadenas en vivo. Es "
            "lo que corresponde cuando el usuario aprieta «actualizar»; el resto "
            "del tiempo la caché es justamente lo que evita repetir el fan-out."
        ),
    )


class ChainPaymentSummary(BaseModel):
    """Cómo le fue a una cadena, con y sin el mejor medio de pago."""

    model_config = ConfigDict(frozen=True)

    chain_slug: str
    display_name: str
    base_total_cents: int
    best_final_total_cents: int
    best_instrument_label: str | None = None
    simulated: bool = Field(
        ...,
        description=(
            "Si la cadena tiene checkout consultable. En `false` —Disco— nada de "
            "su descuento está medido."
        ),
    )


class BestPaymentResult(BaseModel):
    """Ranking de cadena × medio de pago para una canasta."""

    options: list[PaymentOption] = Field(
        default_factory=list, description="Ordenadas por lo que terminás pagando."
    )
    chains: list[ChainPaymentSummary] = Field(default_factory=list)
    on_date: date
    channel: Channel
    winner: PaymentOption | None = Field(
        None, description="La combinación más barata. None si no hubo ninguna."
    )
    baseline_total_cents: int | None = Field(
        None, description="Lo que costaría en la cadena más barata sin usar promos."
    )
    unverified_rules: list[str] = Field(
        default_factory=list,
        description=(
            "Reglas curadas que participaron del cálculo sin estar contrastadas "
            "contra la fuente. Su ahorro es una estimación."
        ),
    )
    notes: list[str] = Field(default_factory=list)
    basket: BasketComparison | None = Field(
        None,
        description=(
            "El detalle línea por línea que respalda estos totales. Viaja acá "
            "porque el cálculo ya lo resolvió: pedirlo aparte a `/basket/compare` "
            "duplicaría el fan-out contra las cadenas y devolvería precios "
            "capturados en otro instante, que podrían no cerrar con estos totales."
        ),
    )
