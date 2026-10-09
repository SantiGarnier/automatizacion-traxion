"""
Test de fuente_db + sql/01_bot_usuarios_traxion.sql contra un PostgreSQL de PRUEBA.

ATENCION: BORRA y vuelve a crear public.users, public.linked_accounts y el esquema bot
en la base indicada. Por eso solo corre si la base se llama prueba* o test*.

    TEST_DB_HOST (localhost)  TEST_DB_PORT (5544)  TEST_DB_NAME (prueba_bot)
    TEST_DB_USER (postgres)   TEST_DB_PASS ()

    python tests/test_fuente_db.py
"""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
import config  # noqa: E402

config.DB_HOST = os.environ.get("TEST_DB_HOST", "localhost")
config.DB_PORT = int(os.environ.get("TEST_DB_PORT", "5544"))
config.DB_NAME = os.environ.get("TEST_DB_NAME", "prueba_bot")
config.DB_USER = os.environ.get("TEST_DB_USER", "postgres")
config.DB_PASS = os.environ.get("TEST_DB_PASS", "")
config.MAX_INTENTOS = 3
config.SYNC_MARGEN_MIN = 60

if not config.DB_NAME.lower().startswith(("prueba", "test")):
    sys.exit(f"Me niego a correr contra la base '{config.DB_NAME}': el test borra tablas. Usar una base prueba*/test*.")

import psycopg  # noqa: E402

import fuente_db  # noqa: E402

A, B = "cib7kb9v3scb2sltq6yx6cf17", "usuario_inexistente_prueba_123"

ESQUEMA_APP = """
DROP SCHEMA IF EXISTS bot CASCADE;
DROP TABLE IF EXISTS public.linked_accounts, public.users;
CREATE TABLE public.users (id text PRIMARY KEY, email text NOT NULL, commerce_name text NOT NULL);
CREATE TABLE public.linked_accounts (
    id text PRIMARY KEY, user_id text NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    provider text NOT NULL, status text NOT NULL, external_user_id text, external_account_id text,
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (user_id, provider));
CREATE INDEX idx_linked_accounts_provider ON public.linked_accounts (provider);
INSERT INTO public.users VALUES ('u1','a@x.com','Kiosco Uno'), ('u2','b@x.com','Almacen Dos'),
                                ('u3','c@x.com','Otro'), ('u4','d@x.com','Nuevo');
INSERT INTO public.linked_accounts (id, user_id, provider, status, external_user_id, created_at) VALUES
  ('la1','u1','traxion','active',  'cib7kb9v3scb2sltq6yx6cf17',     now() - interval '10 days'),
  ('la2','u2','traxion','active',  'usuario_inexistente_prueba_123', now() - interval '9 days'),
  ('la3','u3','otro',   'active',  'no_debe_entrar',                 now() - interval '8 days'),
  ('la4','u4','traxion','inactive','inactivo_no_se_procesa',         now() - interval '7 days');
"""


def estado(conn, usuario):
    return conn.execute("SELECT status, status_message, resultado, intentos, cuenta_numero, monto_anterior"
                        " FROM bot.usuarios_traxion WHERE usuario_traxion = %s", (usuario,)).fetchone()


db = fuente_db.BaseDatos()
assert db is fuente_db.BaseDatos(), "singleton: siempre tiene que devolver la misma instancia"

with db.conexion() as conn:
    conn.execute(ESQUEMA_APP)
    conn.execute((RAIZ / "sql" / "01_bot_usuarios_traxion.sql").read_text(encoding="utf-8"))
    conn.execute((RAIZ / "sql" / "01_bot_usuarios_traxion.sql").read_text(encoding="utf-8"))  # idempotente

    # Carga inicial: las 3 de traxion (2 active + 1 inactive); la del otro provider no
    assert conn.execute("SELECT COUNT(*) FROM bot.usuarios_traxion").fetchone()[0] == 3
    assert fuente_db.sincronizar() == 0, "sin cuentas nuevas no debe importar nada"

    # Cola: solo active (leido en vivo por la FK), ordenadas por antiguedad; respeta el limite
    assert fuente_db.pendientes() == [A, B], fuente_db.pendientes()
    assert fuente_db.pendientes(1) == [A]

    # PRUEBA (GUARDAR=false): status sigue NULL, sin sumar intentos. NO_ENCONTRADO: FAILED directo
    fuente_db.actualizar(A, "PRUEBA", "", "138851", "0.00")
    fuente_db.actualizar(B, "NO_ENCONTRADO", "La busqueda devolvio 'Datos: 0'", None, None)
    assert estado(conn, A) == (None, "PRUEBA", "PRUEBA", 0, "138851", "0.00"), estado(conn, A)
    assert estado(conn, B)[:3] == ("FAILED", "NO_ENCONTRADO: La busqueda devolvio 'Datos: 0'", "NO_ENCONTRADO")
    assert fuente_db.pendientes() == [A]

    # Incremental: una cuenta nueva se importa; las viejas no se reescriben
    conn.execute("INSERT INTO public.users VALUES ('u5','e@x.com','Recien Vinculado')")
    conn.execute("INSERT INTO public.linked_accounts (id, user_id, provider, status, external_user_id)"
                 " VALUES ('la5','u5','traxion','active','nuevo_usuario_traxion')")
    assert fuente_db.sincronizar() == 1
    assert estado(conn, A) == (None, "PRUEBA", "PRUEBA", 0, "138851", "0.00"), estado(conn, A)
    assert fuente_db.pendientes() == [A, "nuevo_usuario_traxion"]

    # Margen: una cuenta que confirma tarde con created_at un poco anterior a la ultima importada igual entra
    conn.execute("INSERT INTO public.users VALUES ('u6','f@x.com','Tardia')")
    conn.execute("INSERT INTO public.linked_accounts (id, user_id, provider, status, external_user_id, created_at)"
                 " VALUES ('la6','u6','traxion','active','tardia', now() - interval '10 minutes')")
    assert fuente_db.sincronizar() == 1

    # Estado del vinculo en vivo: si la app la desactiva, sale de la cola sin re-sincronizar
    conn.execute("UPDATE public.linked_accounts SET status = 'inactive' WHERE id = 'la5'")
    assert "nuevo_usuario_traxion" not in fuente_db.pendientes()

    # OK -> SUCCESS, sale de la cola, limpia el mensaje y conserva el numero de cuenta
    fuente_db.actualizar(A, "OK", "", None, None)
    assert A not in fuente_db.pendientes()
    assert estado(conn, A) == ("SUCCESS", None, "OK", 0, "138851", "0.00"), estado(conn, A)

    # SIN_CAMBIOS (ya tenia el monto) tambien es SUCCESS
    conn.execute("UPDATE bot.usuarios_traxion SET status = NULL WHERE usuario_traxion = %s", (A,))
    fuente_db.actualizar(A, "SIN_CAMBIOS", "", None, "1.00")
    assert estado(conn, A)[0] == "SUCCESS"

    # Falla pasajera: sigue NULL (se reintenta) con el ultimo error; al 3er intento -> FAILED
    conn.execute("UPDATE bot.usuarios_traxion SET status = NULL, status_message = NULL, intentos = 0"
                 " WHERE usuario_traxion = %s", (B,))
    fuente_db.actualizar(B, "TIMEOUT_BUSQUEDA", "Buscar: la plataforma no respondio en 3 s", None, None)
    assert estado(conn, B)[:4] == (None, "TIMEOUT_BUSQUEDA: Buscar: la plataforma no respondio en 3 s",
                                   "TIMEOUT_BUSQUEDA", 1), estado(conn, B)
    assert B in fuente_db.pendientes()
    fuente_db.actualizar(B, "NO_PROCESADO", "Relogin fallido", None, None)  # no es culpa de la cuenta
    assert estado(conn, B)[0] is None and estado(conn, B)[3] == 1
    fuente_db.actualizar(B, "ERROR", "falla", None, None)
    assert B in fuente_db.pendientes()
    fuente_db.actualizar(B, "ERROR", "falla", None, None)
    assert estado(conn, B)[:4] == ("FAILED", "ERROR: falla (3 intentos)", "ERROR", 3), estado(conn, B)
    assert B not in fuente_db.pendientes(), "FAILED ya no se reintenta"

    # La cola usa el indice parcial de pendientes (status IS NULL)
    conn.execute("SET enable_seqscan = off")
    plan = "\n".join(r[0] for r in conn.execute(
        "EXPLAIN " + fuente_db.SQL_PENDIENTES, {"limite": None}).fetchall())
    assert "idx_usuarios_traxion_pendientes" in plan, plan
    conn.execute("RESET enable_seqscan")

    # FK: si la app borra el vinculo, la fila del bot se borra sola
    conn.execute("DELETE FROM public.linked_accounts WHERE id = 'la6'")
    assert estado(conn, "tardia") is None

    # Vista de consulta con datos de la app
    assert conn.execute("SELECT comercio, estado_vinculo, status FROM bot.v_usuarios_traxion"
                        " WHERE usuario_traxion = %s", (A,)).fetchone() == ("Kiosco Uno", "active", "SUCCESS")

    # El CHECK no deja escribir un status inventado
    try:
        conn.execute("UPDATE bot.usuarios_traxion SET status = 'OK' WHERE usuario_traxion = %s", (A,))
        raise AssertionError("el CHECK de status deberia haber fallado")
    except psycopg.errors.CheckViolation:
        pass

# El "with" cerro la conexion: el singleton se tiene que reconectar solo en la siguiente consulta
assert conn.closed
assert fuente_db.pendientes() == fuente_db.pendientes()
assert db.conexion() is not conn and not db.conexion().closed
db.cerrar()
assert db.conexion().execute("SELECT 1").fetchone() == (1,), "despues de cerrar() vuelve a conectar"
db.cerrar()

# --- vaciar_cola (lo que corre el servidor al despertar), con el navegador simulado ---
import traxion_bot  # noqa: E402

app = db.conexion()
app.execute("INSERT INTO public.users VALUES ('u7','g@x.com','Siete'), ('u8','h@x.com','Ocho'), ('u9','i@x.com','Nueve')")
app.execute("INSERT INTO public.linked_accounts (id, user_id, provider, status, external_user_id) VALUES"
            " ('la7','u7','traxion','active','siete'), ('la8','u8','traxion','active','ocho')")
lotes = []


def correr_lote_falso(usuarios, guardar, headless):
    lotes.append(list(usuarios))
    if len(lotes) == 1:  # mientras procesa el primer lote, la app vincula una cuenta nueva
        fuente_db.BaseDatos().ejecutar("INSERT INTO public.linked_accounts (id, user_id, provider, status,"
                                       " external_user_id) VALUES ('la9','u9','traxion','active','nueve')")
    resultados = {}
    for u in usuarios:
        resultados[u] = "TIMEOUT_BUSQUEDA" if u == "ocho" else "OK"  # "ocho" tiene una falla pasajera
        fuente_db.actualizar(u, resultados[u], "", None, None)
    return resultados


traxion_bot.correr_lote = correr_lote_falso
conteo = traxion_bot.vaciar_cola(guardar=True, headless=True)
assert lotes == [["siete", "ocho"], ["nueve"]], lotes  # toma la que llego mientras trabajaba
assert conteo == {"OK": 2, "TIMEOUT_BUSQUEDA": 1}, conteo
# "ocho" sigue pendiente para el proximo despertar, pero NO se reintento en loop en este
assert fuente_db.pendientes() == ["ocho"]
assert estado(db.conexion(), "ocho")[:4] == (None, "TIMEOUT_BUSQUEDA", "TIMEOUT_BUSQUEDA", 1)
db.cerrar()

print("TEST FUENTE_DB OK")
