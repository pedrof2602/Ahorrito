"""Configuración del proveedor Coto (Constructor.io).

Coto no es VTEX: su catálogo se sirve por la API pública de Constructor.io, el
motor de búsqueda que tiene montado Coto Digital. Eso cambia todo lo que importa
respecto de Carrefour y Disco:

* No hay checkout consultable, así que no hay simulación de canasta ni precio
  autoritativo. Lo único disponible es el catálogo.
* En cambio **sí publica el precio de todas sus sucursales en cada producto**,
  en el array `data.price`. Es más de lo que da VTEX: Coto es la primera cadena
  soportada que puede devolver `PriceScope.STORE` de verdad.

Como en `VTEXStoreConfig`, los flags no son preferencias: cada uno lleva al lado
la medición que lo justifica.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.catalog import PriceScope, Store


@dataclass(frozen=True, slots=True)
class CotoConfig:
    slug: str = "coto-ar"
    display_name: str = "Coto"

    base_url: str = "https://ac.cnstrc.com"
    api_key: str = "key_r6xzz4IAoTWcipni"
    """Clave pública de Coto, la misma que usa cotodigital.com.ar desde el
    navegador. Va en el query string; no es un secreto ni identifica a nadie."""

    site_base_url: str = "https://www.cotodigital.com.ar"
    """Sólo para armar el `link` del producto: `data.url` viene relativo."""

    default_store: str = "200"
    """Sucursal cuyo precio se usa cuando no se pide ninguna.

    Medido: el sufijo de `data.url` (`_/R-00508911-00508911-200`) es `200` en
    250/250 productos de cinco búsquedas distintas, y el `listPrice` de la
    sucursal 200 coincide con el `product_list_price` del producto en 245/250.
    Es la sucursal de referencia del sitio, así que es el piso nacional honesto."""

    max_page_size: int = 200
    """Medido: `num_results_per_page=200` devuelve 200 resultados. No se probó
    más arriba porque no hace falta; 200 ya cubre cualquier búsqueda de la app."""

    max_pages: int = 25
    """Tope de paginación al recorrer una categoría, para no barrer 1800
    productos sin querer. Al cortar por acá se avisa, igual que en VTEX."""

    drop_unmatched_results: bool = True
    """Descarta los resultados que no matchearon ningún término de la búsqueda.

    **Medido, y no es cosmético.** Constructor.io, cuando no hay match léxico,
    cae a búsqueda por embeddings y devuelve HTTP 200 con productos cualesquiera:
    `zzzqqxnotaproduct` devuelve 20 resultados encabezados por "Remera Niño/A
    Estampada Skate". Esos resultados vienen con `matched_terms: []`, mientras
    que en las cinco búsquedas reales medidas los 250 resultados traen
    `matched_terms` no vacío. Filtrar por ese campo separa las dos cosas sin
    perder nada legítimo.

    Sin esto, buscar "leche" en la comparación federada mezcla remeras de Coto
    con la leche de Carrefour."""

    # --- Cordura de datos, medida contra la API ---
    min_unit_price_ratio: float = 0.04
    max_unit_price_ratio: float = 200.0
    """Banda en la que `formatPrice` es creíble como precio por unidad de medida.

    `formatPrice` es el precio por Litro/Kilo (ver `mapper`), así que su cociente
    contra el precio de góndola es 1/tamaño del envase: 0,67 para 1,5 L, 10 para
    100 g. La banda cubre desde un bidón de 25 L hasta un sobre de 5 g.

    Existe porque **la sucursal 133 lo publica roto**: `listPrice=2495` con
    `formatPrice=29,05` para una leche de 1 L (cociente 0,012, mediana 0,015 en
    sus 55 entradas), contra 2,0 de mediana en las otras 34 sucursales. Su
    `listPrice` sí es plausible, así que se descarta el precio por unidad y se
    conserva el de góndola en lugar de tirar la oferta entera."""

    max_concurrency: int = 4
    min_interval_ms: int = 250
    timeout_s: float = 20.0

    @property
    def supports_store_prices(self) -> bool:
        """Coto publica el precio de cada sucursal en el propio producto."""
        return True

    def resolve_price_scope(self, store: Store | None = None) -> tuple[str, PriceScope]:
        """Sucursal a leer del array de precios, y qué precisión da eso.

        A diferencia de VTEX no hay canal de venta: se elige una sucursal del
        array o se cae a la de referencia. Pedir una sucursal de otra cadena no
        rompe nada, cae a nacional; es la misma regla que en VTEX y por el mismo
        motivo: pedir una sucursal de Carrefour no puede dejarte sin los precios
        de Coto.
        """
        if store is not None and store.chain_slug == self.slug and store.external_id:
            return store.external_id, PriceScope.STORE
        return self.default_store, PriceScope.NATIONAL


COTO_AR = CotoConfig()
