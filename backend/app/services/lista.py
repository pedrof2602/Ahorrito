"""Agregar, leer y borrar productos de la lista, en una frase.

Las tres funciones reciben la sesión y el `user_id` y devuelven **el texto que se
le va a decir a la persona**. No arman JSON, no saben quién las llama y no
distinguen si del otro lado hay un asistente de voz, un bot o un `curl`.

Esa separación no es teórica: este módulo nació adentro de una integración con
Alexa que terminó descartada —Amazon apagó la API de listas en julio de 2024— y
sobrevivió intacto al borrado justamente porque nunca supo nada de Alexa. Hoy lo
usa el Atajo de Siri. Mañana, lo que venga.

Todo lo que se contesta acá está pensado para **escucharse una sola vez y sin
poder volver atrás**. De ahí que las frases sean cortas, que confirmen qué se
entendió —"agregué *leche*", no "listo"— y que un problema se cuente en lugar de
callarse: si la persona no escucha nada, no tiene forma de saber si el producto
quedó anotado o se perdió.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import ShoppingListRepository

log = logging.getLogger(__name__)

MAX_ITEMS_DICHOS = 15
"""Cuántos productos se leen en voz alta antes de cortar.

Una lista de cincuenta cosas dicha de corrido no es información: para cuando el
asistente llega a la mitad la persona ya perdió el hilo, y no puede rebobinar. Se
leen los primeros y se dice cuántos quedan."""


async def agregar_producto(session: AsyncSession, user_id: int, producto: str) -> str:
    """Anota un producto en la lista del usuario. Devuelve qué decirle.

    No deduplica a propósito: "agregá leche" dos veces suele significar dos
    leches, y quien se equivocó tiene la lista en el teléfono para arreglarlo.
    Adivinar acá saldría mal en silencio.
    """
    producto = producto.strip()
    if not producto:
        # Pasa cuando el dictado no entendió el audio del medio.
        return "No te entendí qué producto. Probá de nuevo."

    lists = ShoppingListRepository(session, user_id)
    row = await lists.default_list()

    if not await lists.add_line(row, producto):
        return (
            f"Tu lista está llena, tiene {ShoppingListRepository.MAX_LINES} productos. "
            "Sacá alguno desde la app y volvé a intentar."
        )

    await session.commit()
    log.info("Lista: el usuario %d agregó un producto por voz", user_id)
    return f"Listo, agregué {producto}."


async def leer_lista(session: AsyncSession, user_id: int) -> str:
    """Lee la lista del usuario en voz alta."""
    row = await ShoppingListRepository(session, user_id).default_list()
    # `default_list` crea la lista si no había ninguna, así que hay que commitear
    # incluso en la operación de lectura.
    await session.commit()

    productos = [line.query for line in row.lines]
    if not productos:
        return "Tu lista está vacía."

    total = len(productos)
    if total > MAX_ITEMS_DICHOS:
        visibles = enumerar(productos[:MAX_ITEMS_DICHOS])
        faltan = total - MAX_ITEMS_DICHOS
        return f"Tenés {total} productos. Los primeros son: {visibles}, y {faltan} más."

    if total == 1:
        return f"Tenés una sola cosa: {productos[0]}."
    return f"Tenés {total} productos: {enumerar(productos)}."


async def borrar_producto(session: AsyncSession, user_id: int, producto: str) -> str:
    """Saca un producto de la lista."""
    producto = producto.strip()
    if not producto:
        return "No te entendí qué producto. Probá de nuevo."

    lists = ShoppingListRepository(session, user_id)
    row = await lists.default_list()

    removed = await lists.remove_line(row, producto)
    await session.commit()

    if removed is None:
        return f"No encontré {producto} en tu lista."
    return f"Listo, saqué {removed}."


def enumerar(productos: list[str]) -> str:
    """`["a", "b", "c"]` → `"a, b y c"`.

    La "y" antes del último es lo que hace que suene a una lista y no a un
    volcado: sin ella se lee todo con la misma entonación y no se sabe cuándo
    terminó.
    """
    if len(productos) == 1:
        return productos[0]
    return f"{', '.join(productos[:-1])} y {productos[-1]}"
