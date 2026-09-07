"""Contrato de la comparación de una lista de compra completa.

Separado de `catalog.py` a propósito: ahí viven los modelos de dominio que todo
proveedor mapea, acá el contrato de un endpoint. Que la canasta cambie no debería
tocar lo que devuelve un provider.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.models.catalog import Freshness, Offer, PriceScope, Product, Store


class MatchConfidence(StrEnum):
    """Cuánto se puede confiar en que se está comparando el mismo producto."""

    PINNED = "pinned"
    """El usuario fijó el EAN: no hubo heurística que equivocar."""

    EXACT_EAN = "exact_ean"
    """Mismo EAN en todas las cadenas. Comparación exacta."""

    NAME = "name"
    """Sin EAN común: cada cadena aportó su mejor coincidencia por nombre. Pueden
    diferir en marca o gramaje, así que el total de esa línea es orientativo."""


class ChainErrorOut(BaseModel):
    """Fallo de una cadena, informado sin disfrazarlo de 'sin resultados'."""

    chain_slug: str
    message: str


def first_per_chain(errors: Iterable[ChainErrorOut]) -> list[ChainErrorOut]:
    """Un error por cadena, el primero, respetando el orden de aparición.

    Una cadena caída falla en *cada* consulta que se le haga: una lista de quince
    líneas devuelve quince veces el mismo fallo, y la interfaz los lista uno por
    uno. Lo que hay para contar es una cadena caída, no quince errores.

    Se queda con el primero porque es el más fundamental: si la búsqueda ya
    falló, que además no responda el checkout es una consecuencia, no una noticia
    aparte.
    """
    seen: dict[str, ChainErrorOut] = {}
    for error in errors:
        seen.setdefault(error.chain_slug, error)
    return list(seen.values())


class BasketLine(BaseModel):
    """Una línea de la lista de compra."""

    model_config = ConfigDict(frozen=True)

    query: str = Field(..., min_length=2, examples=["leche entera 1L"])
    quantity: int = Field(1, ge=1, le=99)
    ean: str | None = Field(
        None,
        description="Fija el producto exacto y saltea la heurística de matcheo.",
        examples=["7790895000997"],
    )


class BasketCompareRequest(BaseModel):
    lines: list[BasketLine] = Field(..., min_length=1, max_length=50)
    postal_code: str | None = Field(
        None,
        description=(
            "Resuelve la sucursal de las cadenas que regionalizan. Sin esto, los "
            "precios son del canal por defecto de cada cadena."
        ),
        examples=["1425"],
    )
    sales_channel: int | None = Field(
        None,
        description=(
            "Canal de venta a consultar. Es la palanca de precio real de VTEX: en "
            "Carrefour el canal 1 y el 3 publican el mismo producto con hasta 33% "
            "de diferencia. Consultá los canales disponibles en `/chains`. Una "
            "cadena que no lo soporta cae a su precio nacional en lugar de fallar."
        ),
        examples=[1],
    )
    chain_slugs: list[str] | None = Field(
        None, description="Limitar la comparación a estas cadenas."
    )
    candidates_per_chain: int = Field(
        20,
        ge=5,
        le=50,
        description="Cuántos resultados mirar por línea y cadena para buscar un EAN común.",
    )
    persist: bool = Field(
        True, description="Guardar los precios elegidos en el histórico."
    )
    verify: bool = Field(
        False,
        description=(
            "Confirmar el total contra el checkout de cada cadena. Es el precio "
            "real con promociones aplicadas, pero duplica el tiempo de respuesta."
        ),
    )
    fresh: bool = Field(
        False,
        description=(
            "Ignorar la caché y consultar a todas las cadenas en vivo. Cuesta "
            "`líneas x cadenas` requests, así que conviene reservarlo para el "
            "momento de decidir la compra. `verify` ya es siempre en vivo."
        ),
    )


class LineMatch(BaseModel):
    """Lo que una cadena ofrece para una línea de la lista."""

    model_config = ConfigDict(frozen=True)

    chain_slug: str
    product: Product
    offer: Offer
    line_total_cents: int = Field(..., description="price_cents x quantity.")
    freshness: Freshness | None = Field(
        None, description="Cuándo se consultó este precio y si puede estar vencido."
    )


class BasketLineResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    quantity: int
    confidence: MatchConfidence
    ean: str | None = Field(
        None, description="EAN común, cuando la línea se resolvió por EAN."
    )
    matches: list[LineMatch] = Field(
        default_factory=list, description="Una por cadena que tenga el producto."
    )
    missing_in: list[str] = Field(
        default_factory=list, description="Cadenas sin coincidencia para esta línea."
    )
    comparable: bool = Field(
        ...,
        description=(
            "Si la línea está disponible en todas las cadenas del ranking y por "
            "lo tanto suma al total comparable."
        ),
    )
    cheapest_chain: str | None = None
    saving_cents: int = Field(
        0, description="Diferencia entre la cadena más cara y la más barata en esta línea."
    )


class ChainTotal(BaseModel):
    """Cuánto sale la canasta en una cadena."""

    model_config = ConfigDict(frozen=True)

    chain_slug: str
    display_name: str
    comparable_total_cents: int = Field(
        ...,
        description=(
            "Total sobre las líneas que TODAS las cadenas tienen. Es el número que "
            "ordena el ranking: comparar totales completos dejaría ganar a la "
            "cadena que gasta menos por tener menos."
        ),
    )
    total_cents: int = Field(
        ..., description="Total de todo lo que esta cadena sí resolvió. Informativo."
    )
    verified_total_cents: int | None = Field(
        None, description="Total confirmado contra checkout, si se pidió `verify`."
    )
    matched_lines: int
    missing_lines: list[str] = Field(
        default_factory=list, description="Líneas para las que la cadena no tiene producto."
    )
    unavailable_lines: list[str] = Field(
        default_factory=list,
        description=(
            "Líneas donde la cadena tiene el producto pero sin stock. Cuentan "
            "como no resueltas igual que las faltantes: un producto que no podés "
            "llevarte no está en tu changuito."
        ),
    )
    price_scope: PriceScope = Field(
        ...,
        description=(
            "`national` significa que el total NO es el de tu sucursal: la cadena "
            "no expone precios regionalizados."
        ),
    )
    freshness: Freshness | None = Field(
        None,
        description=(
            "La del precio menos fresco del total. Con `stale` en true, esta "
            "cadena no respondió y su total se armó con los últimos precios "
            "conocidos: **sigue compitiendo en el ranking**, porque sacarla "
            "también sería un resultado equivocado, pero con esta marca puesta."
        ),
    )
    store: Store | None = None

    @property
    def unresolved_lines(self) -> list[str]:
        """Líneas que esta cadena no resuelve, por no tenerlas o por no haber stock."""
        return [*self.missing_lines, *self.unavailable_lines]

    @property
    def complete(self) -> bool:
        """Si la cadena resuelve la lista entera."""
        return not self.unresolved_lines


class BasketComparison(BaseModel):
    chains: list[ChainTotal] = Field(
        default_factory=list,
        description=(
            "Ranking: primero las cadenas que resuelven más líneas de la lista y "
            "recién dentro de ese grupo por `comparable_total_cents`. Una cadena "
            "a la que le faltan productos no puede salir primera aunque sea la "
            "más barata: el total que no incluye lo que te falta no es el precio "
            "de tu compra, porque ese ítem lo vas a terminar pagando en otro lado."
        ),
    )
    lines: list[BasketLineResult] = Field(default_factory=list)
    comparable_line_count: int = Field(
        ..., description="Cuántas líneas respaldan el ranking."
    )
    total_line_count: int
    winner: str | None = Field(
        None,
        description=(
            "Primera del ranking. None si no hubo líneas comparables. Puede tener "
            "faltantes si **ninguna** cadena resolvió la lista entera: en ese caso "
            "`winner_complete` es false y el número no es comparable contra una "
            "compra completa."
        ),
    )
    winner_complete: bool = Field(
        False,
        description=(
            "Si la cadena ganadora resuelve todas las líneas. En false, ganó "
            "siendo la menos incompleta."
        ),
    )
    partial: bool = Field(
        ..., description="True si alguna cadena falló y su total está incompleto."
    )
    stale: bool = Field(
        False,
        description=(
            "Alguna cadena no respondió y compitió con sus últimos precios "
            "conocidos. El ranking está completo pero mezcla datos de distinta "
            "frescura; cuál es cuál está en el `freshness` de cada cadena."
        ),
    )
    errors: list[ChainErrorOut] = Field(default_factory=list)
