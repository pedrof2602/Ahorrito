"""Inquilinos VTEX soportados.

Agregar Jumbo, Vea u otro VTEX argentino es agregar una constante acá: no hace
falta código nuevo. Lo único que hay que hacer antes es medir sus flags con
`scripts/probe_store.py`, porque los inquilinos VTEX no se comportan igual.
"""

from __future__ import annotations

from .config import RegionStrategy, VTEXStoreConfig

CARREFOUR_AR = VTEXStoreConfig(
    slug="carrefour-ar",
    display_name="Carrefour",
    base_url="https://www.carrefour.com.ar",
    region_strategy=RegionStrategy.SALES_CHANNEL,
    # Medido: 1, 3 y 5 responden; 2 y 4 -> "sc is inactive"; 6+ -> "sc not found".
    #
    # Los tres publican precios distintos para el mismo SKU (Leche Protein: $2249
    # en el 1, $2999 en el 3 y el 5), pero **solo el 1 surte de verdad**. Medido
    # sobre 6 términos, ofertas con stock: el 1 siempre 20/20; el 3 es errático
    # (20/20 en "leche" y "aceite", 0/20 en "arroz" y "leche protein", 2/20 en
    # "yerba"); el 5 dio 0/20 en todos. Se dejan consultables porque el precio es
    # información real, y las ofertas sin stock quedan fuera de cualquier total
    # comparable por la regla general.
    sales_channels=(1, 3, 5),
    default_sales_channel=1,
    supports_region_discovery=True,
    teaser_backing_fields=True,
    excluded_category_ids=frozenset({1}),  # "Test Category"
)

DISCO_AR = VTEXStoreConfig(
    slug="disco-ar",
    display_name="Disco",
    base_url="https://www.disco.com.ar",
    # Medido: /regions -> HTTP 500 (ORD021.6) y ningún sc de 1..30 está activo.
    # No tiene regionalización pública; sus precios son nacionales.
    region_strategy=RegionStrategy.NONE,
    supports_region_discovery=False,
    # Medido: el checkout rechaza todo SKU con ORD027 ("no encontrado o no
    # disponible") incluso usando el sellerId que el propio catálogo publica
    # ('1'). Sin simulación utilizable, la confirmación de canasta se saltea en
    # lugar de devolver un total incompleto.
    supports_simulation=False,
)

DIA_AR = VTEXStoreConfig(
    slug="dia-ar",
    display_name="Supermercados DIA",
    base_url="https://diaonline.supermercadosdia.com.ar",
    # Medido: 1 y 2 responden; 3+ -> HTTP 404 "sc not found". Pero **el `sc` no
    # mueve el precio en DIA**: sobre 49 SKUs presentes en ambos canales, los 49
    # publican el mismo precio, y omitir el parámetro devuelve exactamente lo
    # mismo que sc=1. El canal 2 además está vacío de stock (0/111 ofertas con
    # stock sobre 6 términos, contra 120/120 en el 1).
    #
    # Por eso va NONE y no SALES_CHANNEL con sc=1: el efecto sobre las consultas
    # es idéntico, pero SALES_CHANNEL haría que la cadena declare
    # `supports_store_prices=True`, que es una afirmación falsa acá y le pondría
    # al usuario un selector de sucursal que no puede cambiar ningún precio.
    region_strategy=RegionStrategy.NONE,
    # `/regions` responde 200 —no 500 como Disco—, pero lo único que devuelve es
    # un seller logístico (`ardiaprod1080`, sin nombre legible) y solo para CP de
    # CABA: 5000, 8300 y 4000 devuelven la región con `sellers` vacío. No hay
    # sucursales con identidad ni precio propio que descubrir, así que queda
    # apagado en lugar de poblar el selector con "Sucursal ardiaprod1080".
    supports_region_discovery=False,
    # Medido: DIA serializa `Teasers` con el mismo mangling de C# que Carrefour
    # (`<Name>k__BackingField`). Sin este flag las promos del catálogo se caen en
    # silencio: `_parse_promotion` no encuentra `Name` y descarta la promo.
    teaser_backing_fields=True,
    # Medido: el checkout confirma los SKU del propio catálogo con seller "1" y
    # corre el motor de promociones — 2 unidades del SKU 297590 ("2x1") vuelven
    # con `Discounts: -567500` y la promo en `ratesAndBenefitsData`. A diferencia
    # de Disco, acá el total de canasta es consultable.
    supports_simulation=True,
)

VTEX_STORES: tuple[VTEXStoreConfig, ...] = (CARREFOUR_AR, DISCO_AR, DIA_AR)
