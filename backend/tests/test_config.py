"""Los settings de lista, y la regla que los rige: **nunca tirar la app**.

Este archivo existe por un incidente concreto. Un setting declarado `list[str]`
se cargó en Fly separado por comas en vez de como array JSON, y pydantic-settings
levantó `SettingsError` **adentro del source** —antes de cualquier validador—
mientras construía los settings. Como `settings = Settings()`
corre al importar `core/config.py`, la excepción mató a uvicorn antes de que
existiera la app: Fly reinició diez veces, se rindió, y el sitio entero devolvió
502 durante horas. Incluidos `/robots.txt` y el health check.

El setting que lo provocó era de la integración con Alexa, que después se
descartó entera. Los tests se quedan igual, sobre los que sobrevivieron: la mina
no era de ese campo, era del tipo.

Lo que se prueba acá no es que el parseo sea inteligente, sino que **ningún valor
de entorno pueda impedir que el servidor arranque**. Una feature opcional mal
configurada tiene que apagarse sola y dejar el resto en pie.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings, parse_lista

URIS = (
    "https://ahorrito.fly.dev",
    "https://otro.ejemplo.com",
    "http://localhost:5173",
)


# --------------------------------------------------------------- el parser


@pytest.mark.parametrize(
    "valor",
    [
        pytest.param('["{0}","{1}","{2}"]'.format(*URIS), id="json"),
        pytest.param("{0},{1},{2}".format(*URIS), id="comas"),
        pytest.param("{0}, {1}, {2}".format(*URIS), id="comas-con-espacios"),
        pytest.param("{0}\n{1}\n{2}".format(*URIS), id="saltos-de-linea"),
        pytest.param(' ["{0}", "{1}", "{2}"] '.format(*URIS), id="json-con-espacios"),
    ],
)
def test_todas_las_formas_de_escribirlo_dan_lo_mismo(valor):
    """Las cuatro formas en que alguien escribe esto a mano."""
    assert parse_lista(valor) == list(URIS)


@pytest.mark.parametrize(
    "valor",
    ["", "   ", "\n", None, [], "[]"],
    ids=["vacio", "espacios", "salto", "none", "lista-vacia", "json-vacio"],
)
def test_lo_vacio_es_lista_vacia(valor):
    """Vacío no es un error: es "esta feature está apagada"."""
    assert parse_lista(valor) == []


def test_una_sola_url_sin_comas():
    assert parse_lista(URIS[0]) == [URIS[0]]


def test_json_roto_no_deja_basura():
    """Un array mal cerrado da vacío, no pedazos con corchetes pegados.

    Es deliberado: `CORS_ORIGINS` es una whitelist, y vale más que quede vacía
    —y se note— que llena de entradas inservibles que aparentan estar cargadas.
    """
    assert parse_lista('["https://ahorrito.fly.dev",]') == []
    assert parse_lista("[roto") == []


def test_json_que_no_es_lista():
    assert parse_lista('{"a": 1}') == []


def test_el_parser_nunca_levanta():
    """El invariante entero de este módulo, en un test."""
    for basura in ("{{{", "[[[", '"', "\\", "🙂", "null", "123", "[1, 2, 3]"):
        assert isinstance(parse_lista(basura), list)


# ------------------------------------------------- construir los settings


@pytest.mark.parametrize("campo", ["CORS_ORIGINS", "ENABLED_CHAINS"])
@pytest.mark.parametrize(
    "valor",
    ['["a","b"]', "a,b", "a", "", "   ", "{{{", '["a",]', "[]", "a\nb"],
)
def test_ningun_valor_impide_arrancar(monkeypatch, campo, valor):
    """El test que habría evitado el incidente.

    Se prueban todos los campos de lista y no sólo el que rompió —ése ya no
    existe—: la mina era del tipo, no del campo. `ENABLED_CHAINS=coto,carrefour`
    habría tirado el sitio exactamente igual.
    """
    monkeypatch.setenv(campo, valor)

    # `_env_file=None` en todos los tests de este archivo: sin eso se lee el
    # `.env` real de backend/, y el resultado dependería de qué tenga cargado la
    # máquina donde corren los tests.
    settings = Settings(_env_file=None)

    assert isinstance(getattr(settings, campo), list)


def test_el_caso_exacto_que_tiro_produccion(monkeypatch):
    """Comas en vez de JSON: la forma exacta del valor que causó el 502."""
    monkeypatch.setenv("CORS_ORIGINS", "{0},{1},{2}".format(*URIS))

    settings = Settings(_env_file=None)

    assert settings.CORS_ORIGINS == list(URIS)


def test_el_formato_de_fly_toml_sigue_andando(monkeypatch):
    """`CORS_ORIGINS = '[]'` es lo que hay en `fly.toml` hoy.

    El cambio de parseo tocó config que ya estaba funcionando; esto es lo que
    cubre esa espalda.
    """
    monkeypatch.setenv("CORS_ORIGINS", "[]")
    assert Settings(_env_file=None).CORS_ORIGINS == []

    monkeypatch.setenv("CORS_ORIGINS", '["https://ahorrito.fly.dev"]')
    assert Settings(_env_file=None).CORS_ORIGINS == ["https://ahorrito.fly.dev"]


def test_los_defaults_en_codigo_no_se_rompen():
    """Sin variables de entorno, los defaults declarados llegan enteros."""
    settings = Settings(_env_file=None)

    assert "http://localhost:5173" in settings.CORS_ORIGINS
    assert settings.ENABLED_CHAINS == []
