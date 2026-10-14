"""
Test de la orquestacion del lote (procesar_lista / resumen) SIN navegador ni plataforma:
se simula el resultado de cada cuenta. Correr con:  python tests/test_orquestacion.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import fuente_db  # noqa: E402
import traxion_bot as bot  # noqa: E402

tmp = Path(tempfile.mkdtemp())
config.LOG_DIR = tmp / "logs"


def C(usuario, linked_account_id=None):
    return fuente_db.Cuenta(linked_account_id, usuario)


class FakeSesion:
    def __init__(self, fallar_login_en=None):
        self.page, self.sucia, self.logins, self.fallar_login_en = object(), False, 0, fallar_login_en

    def iniciar(self):
        self.logins += 1
        self.sucia = False
        if self.logins == self.fallar_login_en:
            raise bot.BotError("login caido")


guion = {}  # trabajo -> resultado de cada llamada sucesiva
registrados = []  # (trabajo, estado) de cada llamada a registrar()


def fake_procesar(page, trabajo, guardar, lote, intento):
    accion = guion[trabajo].pop(0)
    if accion == "vencida":
        raise bot.SesionVencida("vencio")
    return accion


def fake_registrar(trabajo, cuenta, anterior, estado, detalle):
    registrados.append((trabajo, estado))


def test_vencida_timeout_y_reintento():
    a, b, c, d = C("a"), C("b"), C("c"), C("d")
    guion.update({a: ["PRUEBA"], b: ["vencida", "SIN_CAMBIOS"], c: ["NO_ENCONTRADO"],
                  d: ["TIMEOUT_BUSQUEDA", "PRUEBA"]})
    s = FakeSesion()
    r1 = bot.procesar_lista(s, [a, b, c, d], False, "L1", 1)
    assert r1 == {a: "PRUEBA", b: "SIN_CAMBIOS", c: "NO_ENCONTRADO", d: "TIMEOUT_BUSQUEDA"}, r1
    # 1 relogin por la sesion vencida de "b"; queda sucia por el timeout de "d" (ultimo de la lista)
    assert s.logins == 1 and s.sucia, (s.logins, s.sucia)

    r2 = bot.procesar_lista(s, [d], False, "L1", 2)
    assert r2 == {d: "PRUEBA"} and s.logins == 2, (r2, s.logins)  # relogin antes del reintento
    r1.update(r2)
    bot.resumen("L1", r1)  # el resumen se arma en memoria con el estado final de cada cuenta


def test_relogin_fallido_marca_no_procesado():
    x, y, z = C("x"), C("y"), C("z")
    guion.update({x: ["ERROR"], y: ["PRUEBA"], z: ["PRUEBA"]})
    registrados.clear()
    s = FakeSesion(fallar_login_en=1)
    r = bot.procesar_lista(s, [x, y, z], False, "L2", 1)
    assert r == {x: "ERROR", y: "NO_PROCESADO", z: "NO_PROCESADO"}, r
    assert registrados == [(y, "NO_PROCESADO"), (z, "NO_PROCESADO")], "los no procesados quedan registrados"


def test_mismo_usuario_dos_vinculos_son_dos_trabajos():
    # Dos vinculos de Pago24 al mismo usuario de Traxion: cada uno tiene su propio resultado
    v1, v2 = C("compartido", "la_1"), C("compartido", "la_2")
    guion.update({v1: ["OK"], v2: ["SIN_CAMBIOS"]})
    r = bot.procesar_lista(FakeSesion(), [v1, v2], True, "L4", 1)
    assert r == {v1: "OK", v2: "SIN_CAMBIOS"}, r


def test_otra_corrida_no_ensucia_la_sesion():
    o = C("reservada_por_otro", "la_9")
    guion.update({o: ["OTRO_PROCESO"]})
    s = FakeSesion()
    assert bot.procesar_lista(s, [o], True, "L5", 1) == {o: "OTRO_PROCESO"}
    assert not s.sucia and s.logins == 0, "una cuenta de otra corrida no toca la pagina: no hace falta relogin"


def test_no_coincide_ensucia_la_sesion():
    # Despues de NO_COINCIDE la pagina puede estar en usuarios o en el formulario: relogin antes de seguir
    n, m = C("no_coincide"), C("siguiente")
    guion.update({n: ["NO_COINCIDE"], m: ["OK"]})
    s = FakeSesion()
    assert bot.procesar_lista(s, [n, m], True, "L6", 1) == {n: "NO_COINCIDE", m: "OK"}
    assert s.logins == 1


def test_monto_credito_fijo():
    assert bot.MONTO_CREDITO == "1"
    assert not hasattr(config, "MONTO_CREDITO"), "el monto ya no se configura desde el .env"


def test_sin_sqlite():
    assert not hasattr(config, "DB_PATH"), "el historial local en SQLite se saco: la fuente de verdad es PostgreSQL"
    assert "sqlite3" not in sys.modules or not hasattr(bot, "sqlite3")
    assert "NO_COINCIDE" in fuente_db.FALLA_DEFINITIVA


if __name__ == "__main__":
    bot.procesar_usuario = fake_procesar
    bot.registrar = fake_registrar
    test_vencida_timeout_y_reintento()
    test_relogin_fallido_marca_no_procesado()
    test_mismo_usuario_dos_vinculos_son_dos_trabajos()
    test_otra_corrida_no_ensucia_la_sesion()
    test_no_coincide_ensucia_la_sesion()
    test_monto_credito_fijo()
    test_sin_sqlite()
    assert list(config.LOG_DIR.glob("*.log")), "no se escribio el log"
    print("TODOS LOS TESTS OK")
