"""Perfil, domicilios y listas de compras."""

from __future__ import annotations

import pytest

from app.db.repositories import (
    AddressRepository,
    ShoppingListRepository,
    UserProfileRepository,
)


# --------------------------------------------------------------------- perfil


@pytest.mark.asyncio
async def test_perfil_se_crea_solo_la_primera_vez(session, user):
    """Base vacía y perfil vacío son el mismo estado: una app recién instalada."""
    repo = UserProfileRepository(session, user.id)

    row = await repo.get_or_create()
    assert row.postal_code is None
    assert row.channel == "in_store"
    assert row.theme == "system"

    await repo.update(row, {"postal_code": "1425"})
    # La segunda llamada devuelve el mismo perfil, no uno nuevo.
    assert (await repo.get_or_create()).postal_code == "1425"


@pytest.mark.asyncio
async def test_perfil_persiste_entre_requests(client):
    assert (await client.get("/api/v1/profile")).json()["postal_code"] is None

    saved = await client.put(
        "/api/v1/profile",
        json={
            "postal_code": "1425",
            "channel": "online",
            "full_name": "Pedro",
            "email": "pedro@ejemplo.com",
        },
    )
    assert saved.status_code == 200

    body = (await client.get("/api/v1/profile")).json()
    assert body["postal_code"] == "1425"
    assert body["channel"] == "online"
    assert body["full_name"] == "Pedro"


@pytest.mark.asyncio
async def test_campo_vaciado_se_borra(client):
    """Un input vaciado llega como `""`, y eso es borrar el dato.

    Si se guardara como cadena vacía, borrar un campo no se distinguiría de
    nunca haberlo cargado y la UI mostraría un hueco en vez del placeholder.
    """
    await client.put("/api/v1/profile", json={"full_name": "Pedro"})
    await client.put("/api/v1/profile", json={"full_name": "   "})
    assert (await client.get("/api/v1/profile")).json()["full_name"] is None


@pytest.mark.asyncio
async def test_email_y_dni_invalidos_se_rechazan(client):
    assert (await client.put("/api/v1/profile", json={"email": "no-es-mail"})).status_code == 422
    assert (await client.put("/api/v1/profile", json={"dni": "Pedro"})).status_code == 422
    # Con puntos sí: es como se escribe un DNI acá.
    assert (await client.put("/api/v1/profile", json={"dni": "30.123.456"})).status_code == 200


# ----------------------------------------------------------------- domicilios


@pytest.mark.asyncio
async def test_domicilio_repetido_da_409(client):
    body = {"label": "Casa", "street": "Corrientes", "number": "1234"}
    assert (await client.post("/api/v1/addresses", json=body)).status_code == 201

    repeated = await client.post("/api/v1/addresses", json=body)
    assert repeated.status_code == 409
    # El mensaje es el que ve el usuario en pantalla, no un "HTTP 409".
    assert "Casa" in repeated.json()["detail"]


@pytest.mark.asyncio
async def test_solo_queda_un_domicilio_predeterminado(session, user):
    """Marcar uno implica desmarcar el resto: es propiedad del conjunto."""
    repo = AddressRepository(session, user.id)
    casa = await repo.create({"label": "Casa", "is_default": True})
    await repo.clear_default(except_id=casa.id)

    trabajo = await repo.create({"label": "Trabajo", "is_default": True})
    await repo.clear_default(except_id=trabajo.id)

    marcados = [row.label for row in await repo.list() if row.is_default]
    assert marcados == ["Trabajo"]


@pytest.mark.asyncio
async def test_domicilio_inexistente_da_404(client):
    assert (await client.delete("/api/v1/addresses/999")).status_code == 404
    assert (await client.patch("/api/v1/addresses/999", json={})).status_code == 404


# ---------------------------------------------------------- listas de compras


@pytest.mark.asyncio
async def test_replace_lines_renumera_y_borra(session, user):
    repo = ShoppingListRepository(session, user.id)
    row = await repo.create("Semanal")
    await repo.replace_lines(
        row,
        [
            {"query": "leche", "quantity": 2, "ean": None, "pinned_label": None},
            {"query": "pan", "quantity": 1, "ean": None, "pinned_label": None},
            {"query": "yerba", "quantity": 1, "ean": None, "pinned_label": None},
        ],
    )
    assert [line.position for line in row.lines] == [0, 1, 2]

    # Se saca la del medio: las que quedan se renumeran sin huecos, porque el
    # orden de la lista es el orden en que se recorre la góndola.
    await repo.replace_lines(
        row,
        [
            {"query": "leche", "quantity": 2, "ean": None, "pinned_label": None},
            {"query": "yerba", "quantity": 1, "ean": None, "pinned_label": None},
        ],
    )
    assert [(line.position, line.query) for line in row.lines] == [
        (0, "leche"),
        (1, "yerba"),
    ]


@pytest.mark.asyncio
async def test_replace_lines_corta_en_el_tope(session, user):
    """Guardar más de 50 sería guardar algo que la comparación después rechaza."""
    repo = ShoppingListRepository(session, user.id)
    row = await repo.create("Larga")
    await repo.replace_lines(
        row,
        [
            {"query": f"item {i}", "quantity": 1, "ean": None, "pinned_label": None}
            for i in range(80)
        ],
    )
    assert len(row.lines) == ShoppingListRepository.MAX_LINES


@pytest.mark.asyncio
async def test_lista_por_defecto_es_estable(client):
    """`/default` tiene que devolver siempre la misma lista, no crear una nueva.

    Además cubre el orden de las rutas: si `/shopping-lists/{list_id}` estuviera
    declarada antes, «default» entraría como id y esto daría 422.
    """
    first = await client.get("/api/v1/shopping-lists/default")
    assert first.status_code == 200

    second = await client.get("/api/v1/shopping-lists/default")
    assert second.json()["id"] == first.json()["id"]
    assert len((await client.get("/api/v1/shopping-lists")).json()) == 1


@pytest.mark.asyncio
async def test_guardar_y_releer_la_lista(client):
    list_id = (await client.get("/api/v1/shopping-lists/default")).json()["id"]

    saved = await client.put(
        f"/api/v1/shopping-lists/{list_id}",
        json={
            "lines": [
                {"query": "leche entera 1L", "quantity": 2, "ean": "7790895000997"},
                {"query": "yerba", "quantity": 1, "pinned_label": "Playadito 1kg"},
            ]
        },
    )
    assert saved.status_code == 200

    lines = (await client.get(f"/api/v1/shopping-lists/{list_id}")).json()["lines"]
    assert [line["query"] for line in lines] == ["leche entera 1L", "yerba"]
    assert lines[0]["ean"] == "7790895000997"
    assert lines[1]["pinned_label"] == "Playadito 1kg"


@pytest.mark.asyncio
async def test_linea_fuera_de_rango_se_rechaza(client):
    """Los límites son los de `BasketLine`.

    Si acá entrara algo que la comparación rechaza, el error aparecería después,
    lejos de acá, disfrazado de "el súper no respondió".
    """
    list_id = (await client.get("/api/v1/shopping-lists/default")).json()["id"]
    response = await client.put(
        f"/api/v1/shopping-lists/{list_id}",
        json={"lines": [{"query": "leche", "quantity": 500}]},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_borrar_la_lista_se_lleva_las_lineas(session, user):
    repo = ShoppingListRepository(session, user.id)
    row = await repo.create("Semanal")
    await repo.replace_lines(
        row, [{"query": "leche", "quantity": 1, "ean": None, "pinned_label": None}]
    )
    await repo.delete(row)

    assert await repo.list() == []
