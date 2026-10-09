"""
Test de la orquestacion del lote (procesar_lista / resumen / registrar) SIN navegador ni plataforma:
se simula el resultado de cada usuario. Correr con:  python tests/test_orquestacion.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import traxion_bot as bot  # noqa: E402

tmp = Path(tempfile.mkdtemp())
config.DB_PATH = tmp / "resultados.db"
config.LOG_DIR = tmp / "logs"


class FakeSesion:
    def __init__(self, fallar_login_en=None):
        self.page, self.sucia, self.logins, self.fallar_login_en = object(), False, 0, fallar_login_en

    def iniciar(self):
        self.logins += 1
        self.sucia = False
        if self.logins == self.fallar_login_en:
            raise bot.BotError("login caido")


guion = {}  # usuario -> resultado de cada llamada sucesiva


def fake_procesar(page, usuario, guardar, lote, intento):
    accion = guion[usuario].pop(0)
    if accion == "vencida":
        raise bot.SesionVencida("vencio")
    bot.registrar(lote, intento, usuario, None, None, None, False, accion, "")
    return accion


def estados_finales(lote):
    with sqlite3.connect(config.DB_PATH) as db:
        return dict(db.execute(
            "SELECT usuario_busqueda, estado FROM ejecuciones WHERE id IN"
            " (SELECT MAX(id) FROM ejecuciones WHERE lote = ? GROUP BY usuario_busqueda)", (lote,)))


def test_vencida_timeout_y_reintento():
    guion.update({"a": ["PRUEBA"], "b": ["vencida", "SIN_CAMBIOS"], "c": ["NO_ENCONTRADO"],
                  "d": ["TIMEOUT_BUSQUEDA", "PRUEBA"]})
    s = FakeSesion()
    r1 = bot.procesar_lista(s, ["a", "b", "c", "d"], False, "L1", 1)
    assert r1 == {"a": "PRUEBA", "b": "SIN_CAMBIOS", "c": "NO_ENCONTRADO", "d": "TIMEOUT_BUSQUEDA"}, r1
    # 1 relogin por la sesion vencida de "b"; queda sucia por el timeout de "d" (ultimo de la lista)
    assert s.logins == 1 and s.sucia, (s.logins, s.sucia)

    r2 = bot.procesar_lista(s, ["d"], False, "L1", 2)
    assert r2 == {"d": "PRUEBA"} and s.logins == 2, (r2, s.logins)  # relogin antes del reintento
    assert estados_finales("L1")["d"] == "PRUEBA"
    bot.resumen("L1")


def test_relogin_fallido_marca_no_procesado():
    guion.update({"x": ["ERROR"], "y": ["PRUEBA"], "z": ["PRUEBA"]})
    s = FakeSesion(fallar_login_en=1)
    r = bot.procesar_lista(s, ["x", "y", "z"], False, "L2", 1)
    assert r == {"x": "ERROR", "y": "NO_PROCESADO", "z": "NO_PROCESADO"}, r
    assert estados_finales("L2") == r


def test_migra_base_vieja():
    vieja = tmp / "vieja.db"
    with sqlite3.connect(vieja) as db:
        db.execute("CREATE TABLE ejecuciones (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT, entorno TEXT,"
                   " usuario_busqueda TEXT, cuenta_numero TEXT, cuenta_nombre TEXT, monto_anterior TEXT,"
                   " monto_nuevo TEXT, guardado INTEGER, estado TEXT, detalle TEXT)")
    original, config.DB_PATH = config.DB_PATH, vieja
    try:
        bot.registrar("L3", 1, "u", None, None, None, False, "PRUEBA", "")
        with sqlite3.connect(vieja) as db:
            assert db.execute("SELECT lote, intento FROM ejecuciones").fetchone() == ("L3", 1)
    finally:
        config.DB_PATH = original


if __name__ == "__main__":
    bot.procesar_usuario = fake_procesar
    test_vencida_timeout_y_reintento()
    test_relogin_fallido_marca_no_procesado()
    test_migra_base_vieja()
    assert list(config.LOG_DIR.glob("*.log")), "no se escribio el log"
    print("TODOS LOS TESTS OK")
