"""Qué contesta el skill cuando el usuario le habla.

Las tres operaciones de negocio —`agregar_producto`, `leer_lista`,
`borrar_producto`— son funciones sueltas que reciben la sesión y el `user_id` y
devuelven **la frase que Alexa va a decir**. No arman JSON ni saben qué es un
intent: eso permite probarlas llamándolas directo, que es la única forma
razonable de iterar sobre algo cuyo otro extremo es un parlante.

Todo lo que se contesta acá está pensado para escucharse una sola vez y sin
poder volver atrás. De ahí que las frases sean cortas, que confirmen qué se
entendió —"agregué *leche*", no "listo"— y que un problema se cuente en lugar de
callarse: si el usuario no escucha nada, no tiene forma de saber si el producto
quedó anotado o se perdió.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import ShoppingListRepository

log = logging.getLogger(__name__)

INVOCATION_NAME = "ahorrito"
"""Cómo se invoca el skill. Tiene que coincidir con el modelo de interacción de
la consola; acá se usa solo para armar las frases de ayuda."""

MAX_ITEMS_SPOKEN = 15
"""Cuántos productos se leen en voz alta antes de cortar.

Una lista de cincuenta cosas dicha de corrido no es información: para cuando
Alexa llega a la mitad el usuario ya perdió el hilo, y no puede rebobinar. Se
leen los primeros y se dice cuántos quedan."""


# ------------------------------------------------------------ las operaciones


async def agregar_producto(
    session: AsyncSession, user_id: int, producto: str
) -> str:
    """Anota un producto en la lista del usuario. Devuelve qué decirle.

    No deduplica a propósito: "agregá leche" dos veces suele significar dos
    leches, y el que se equivocó tiene la lista en el teléfono para arreglarlo.
    Adivinar acá saldría mal en silencio.
    """
    producto = producto.strip()
    if not producto:
        # Alexa manda el slot vacío cuando no entendió el audio del medio.
        return "No te entendí qué producto. Probá de nuevo."

    lists = ShoppingListRepository(session, user_id)
    row = await lists.default_list()

    if not await lists.add_line(row, producto):
        return (
            f"Tu lista está llena, tiene {ShoppingListRepository.MAX_LINES} productos. "
            "Sacá alguno desde la app y volvé a intentar."
        )

    await session.commit()
    log.info("Alexa: usuario %d agregó un producto", user_id)
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
    if total > MAX_ITEMS_SPOKEN:
        visibles = _enumerar(productos[:MAX_ITEMS_SPOKEN])
        faltan = total - MAX_ITEMS_SPOKEN
        return f"Tenés {total} productos. Los primeros son: {visibles}, y {faltan} más."

    if total == 1:
        return f"Tenés una sola cosa: {productos[0]}."
    return f"Tenés {total} productos: {_enumerar(productos)}."


async def borrar_producto(
    session: AsyncSession, user_id: int, producto: str
) -> str:
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


def _enumerar(productos: list[str]) -> str:
    """`["a", "b", "c"]` → `"a, b y c"`.

    La "y" antes del último es lo que hace que suene a una lista y no a un
    volcado: sin ella, Alexa lee todo con la misma entonación y el usuario no
    sabe cuándo terminó.
    """
    if len(productos) == 1:
        return productos[0]
    return f"{', '.join(productos[:-1])} y {productos[-1]}"


# ----------------------------------------------------- el protocolo de Alexa


def speak(text: str, *, end: bool = True) -> dict[str, Any]:
    """La respuesta mínima: Alexa dice `text`.

    `end=True` cierra la sesión, que es lo correcto para casi todo lo de acá: el
    usuario dijo una cosa, se hizo, no hay nada más que esperar. Dejarla abierta
    enciende el micrófono y hace que el Echo se quede escuchando en silencio.
    """
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": text},
            "shouldEndSession": end,
        },
    }


def link_account() -> dict[str, Any]:
    """Le pide al usuario que vincule la cuenta.

    La `card` de tipo `LinkAccount` es lo que hace aparecer el botón "Vincular"
    en la app de Alexa. Sin ella el usuario escucha "vinculá tu cuenta" y no
    tiene dónde hacerlo.
    """
    response = speak(
        "Primero tenés que vincular tu cuenta de Ahorrito. "
        "Te dejé el link en la app de Alexa."
    )
    response["response"]["card"] = {"type": "LinkAccount"}
    return response


HELP_TEXT = (
    f"Podés decirme: {INVOCATION_NAME}, agregá leche. "
    f"O preguntarme qué hay en tu lista. También puedo sacar cosas: "
    f"{INVOCATION_NAME}, borrá el pan."
)


async def handle(
    session: AsyncSession, user_id: int, request: dict[str, Any]
) -> dict[str, Any]:
    """Despacha un `request` ya verificado y con usuario resuelto.

    Recibe el `request` de adentro del sobre, no el sobre entero: quién es el
    usuario y si la firma cerraba ya se decidió en `web/skill.py`, y mezclar esas
    dos responsabilidades es lo que hace que un día alguien agregue un intent y
    se saltee la verificación sin darse cuenta.
    """
    kind = request.get("type")

    if kind == "LaunchRequest":
        # "Alexa, abrí Ahorrito", sin decir qué quiere. Se deja la sesión abierta
        # para que pueda contestar sin repetir el nombre del skill.
        return speak(f"Hola. {HELP_TEXT}", end=False)

    if kind == "SessionEndedRequest":
        # Amazon exige responder pero ignora el contenido: no se puede hablar acá.
        return {"version": "1.0", "response": {}}

    if kind != "IntentRequest":
        log.warning("Alexa mandó un request de tipo inesperado: %s", kind)
        return speak("No sé hacer eso todavía.")

    intent = request.get("intent") or {}
    name = intent.get("name", "")
    slots = intent.get("slots") or {}

    if name == "AgregarProductoIntent":
        return speak(await agregar_producto(session, user_id, _slot(slots, "producto")))

    if name == "LeerListaIntent":
        return speak(await leer_lista(session, user_id))

    if name == "BorrarProductoIntent":
        return speak(await borrar_producto(session, user_id, _slot(slots, "producto")))

    if name in ("AMAZON.HelpIntent", "AMAZON.FallbackIntent"):
        return speak(HELP_TEXT, end=False)

    if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"):
        return speak("Chau.")

    log.warning("Alexa mandó un intent desconocido: %s", name)
    return speak("No entendí. " + HELP_TEXT, end=False)


def _slot(slots: dict[str, Any], name: str) -> str:
    """El valor de un slot, o `""` si Alexa no lo llenó.

    Que la clave exista no garantiza que tenga `value`: cuando el usuario dice
    solo "agregá", Alexa manda el slot presente y vacío.
    """
    slot = slots.get(name) or {}
    return (slot.get("value") or "").strip()
