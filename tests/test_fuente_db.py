"""
Test de fuente_db + sql/01 + sql/02 contra un PostgreSQL de PRUEBA.

ATENCION: BORRA y vuelve a crear public.users, public.linked_accounts y el esquema bot en la base
indicada, y crea/borra el rol traxion_bot_test. Por eso solo corre si la base se llama prueba* o test*.

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
config.RECLAMO_VENCE_MIN = 30
ADMIN = config.DB_USER
ROL_BOT = "traxion_bot_test"

if not config.DB_NAME.lower().startswith(("prueba", "test")):
    sys.exit(f"Me niego a correr contra la base '{config.DB_NAME}': el test borra tablas. Usar una base prueba*/test*.")

import psycopg  # noqa: E402

import fuente_db  # noqa: E402
from fuente_db import Cuenta  # noqa: E402

A, B = Cuenta("la1", "cib7kb9v3scb2sltq6yx6cf17"), Cuenta("la2", "usuario_inexistente_prueba_123")

ESQUEMA_APP = f"""
DROP SCHEMA IF EXISTS bot CASCADE;
DROP TABLE IF EXISTS public.linked_accounts, public.users;
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{ROL_BOT}') THEN
    EXECUTE 'DROP OWNED BY {ROL_BOT}'; EXECUTE 'DROP ROLE {ROL_BOT}';
  END IF;
END $$;
CREATE TABLE public.users (id text PRIMARY KEY, email text NOT NULL, commerce_name text NOT NULL);
CREATE TABLE public.linked_accounts (
    id text PRIMARY KEY, user_id text NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    provider text NOT NULL, status text NOT NULL, external_user_id text, external_account_id text,
    metadata_json jsonb, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (user_id, provider));
CREATE INDEX idx_linked_accounts_provider ON public.linked_accounts (provider);
INSERT INTO public.users VALUES ('u1','a@x.com','Kiosco Uno'), ('u2','b@x.com','Almacen Dos'),
                                ('u3','c@x.com','Otro'), ('u4','d@x.com','Nuevo');
INSERT INTO public.linked_accounts (id, user_id, provider, status, external_user_id, created_at) VALUES
  ('la1','u1','traxion','active',  'cib7kb9v3scb2sltq6yx6cf17',     now() - interval '10 days'),
  ('la2','u2','traxion','active',  'usuario_inexistente_prueba_123', now() - interval '9 days'),
  ('la3','u3','otro',   'active',  'no_debe_entrar',                 now() - interval '8 days'),
  ('la4','u4','traxion','inactive','inactivo_no_se_procesa',         now() - interval '7 days');
"""


def fila(conn, linked_account_id):
    return conn.execute("SELECT status, status_message, resultado, intentos, cuenta_numero, monto_anterior"
                        " FROM bot.usuarios_traxion WHERE linked_account_id = %s", (linked_account_id,)).fetchone()


def usuarios(cuentas):
    return [c.usuario for c in cuentas]


def conectar_aparte(usuario=ADMIN):
    """Una conexion independiente del singleton: hace de 'otro proceso'."""
    return psycopg.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME, user=usuario,
                           password=config.DB_PASS, autocommit=True)


db = fuente_db.BaseDatos()
assert db is fuente_db.BaseDatos(), "singleton: siempre tiene que devolver la misma instancia"

# ============================================================== contrato status, sync, cola
with db.conexion() as conn:
    conn.execute(ESQUEMA_APP)
    conn.execute((RAIZ / "sql" / "01_bot_usuarios_traxion.sql").read_text(encoding="utf-8"))
    conn.execute((RAIZ / "sql" / "01_bot_usuarios_traxion.sql").read_text(encoding="utf-8"))  # idempotente

    # Carga inicial: las 3 de traxion (2 active + 1 inactive); la del otro provider no
    assert conn.execute("SELECT COUNT(*) FROM bot.usuarios_traxion").fetchone()[0] == 3
    assert fuente_db.sincronizar() == 0, "sin cuentas nuevas no debe importar nada"

    # Cola: devuelve la CLAVE y el usuario; solo active (en vivo por la FK); por antiguedad; respeta el limite
    assert fuente_db.pendientes() == [A, B], fuente_db.pendientes()
    assert fuente_db.pendientes(1) == [A]

    # PRUEBA (GUARDAR=false): status sigue NULL, sin sumar intentos. NO_ENCONTRADO: FAILED directo
    assert fuente_db.actualizar(A, "PRUEBA", "", "138851", "0.00")
    assert fuente_db.actualizar(B, "NO_ENCONTRADO", "La busqueda devolvio 'Datos: 0'", None, None)
    assert fila(conn, "la1") == (None, "PRUEBA", "PRUEBA", 0, "138851", "0.00"), fila(conn, "la1")
    assert fila(conn, "la2")[:3] == ("FAILED", "NO_ENCONTRADO: La busqueda devolvio 'Datos: 0'", "NO_ENCONTRADO")
    assert fuente_db.pendientes() == [A]

    # Incremental: una cuenta nueva se importa; las viejas no se reescriben
    conn.execute("INSERT INTO public.users VALUES ('u5','e@x.com','Recien Vinculado')")
    conn.execute("INSERT INTO public.linked_accounts (id, user_id, provider, status, external_user_id)"
                 " VALUES ('la5','u5','traxion','active','nuevo_usuario_traxion')")
    assert fuente_db.sincronizar() == 1
    assert fila(conn, "la1") == (None, "PRUEBA", "PRUEBA", 0, "138851", "0.00"), fila(conn, "la1")
    assert usuarios(fuente_db.pendientes()) == [A.usuario, "nuevo_usuario_traxion"]

    # Margen: una cuenta que confirma tarde con created_at un poco anterior a la ultima importada igual entra
    conn.execute("INSERT INTO public.users VALUES ('u6','f@x.com','Tardia')")
    conn.execute("INSERT INTO public.linked_accounts (id, user_id, provider, status, external_user_id, created_at)"
                 " VALUES ('la6','u6','traxion','active','tardia', now() - interval '10 minutes')")
    assert fuente_db.sincronizar() == 1

    # Estado del vinculo en vivo: si la app la desactiva, sale de la cola sin re-sincronizar
    conn.execute("UPDATE public.linked_accounts SET status = 'inactive' WHERE id = 'la5'")
    assert "nuevo_usuario_traxion" not in usuarios(fuente_db.pendientes())

    # OK -> SUCCESS, sale de la cola, limpia el mensaje y conserva el numero de cuenta
    fuente_db.actualizar(A, "OK", "", None, None)
    assert A not in fuente_db.pendientes()
    assert fila(conn, "la1") == ("SUCCESS", None, "OK", 0, "138851", "0.00"), fila(conn, "la1")

    # SIN_CAMBIOS (ya tenia el monto) tambien es SUCCESS
    conn.execute("UPDATE bot.usuarios_traxion SET status = NULL WHERE linked_account_id = 'la1'")
    fuente_db.actualizar(A, "SIN_CAMBIOS", "", None, "1.00")
    assert fila(conn, "la1")[0] == "SUCCESS"

    # Falla pasajera: sigue NULL (se reintenta) con el ultimo error; al 3er intento -> FAILED
    conn.execute("UPDATE bot.usuarios_traxion SET status = NULL, status_message = NULL, intentos = 0"
                 " WHERE linked_account_id = 'la2'")
    fuente_db.actualizar(B, "TIMEOUT_BUSQUEDA", "Buscar: la plataforma no respondio en 3 s", None, None)
    assert fila(conn, "la2")[:4] == (None, "TIMEOUT_BUSQUEDA: Buscar: la plataforma no respondio en 3 s",
                                     "TIMEOUT_BUSQUEDA", 1), fila(conn, "la2")
    assert B in fuente_db.pendientes()
    fuente_db.actualizar(B, "NO_PROCESADO", "Relogin fallido", None, None)  # no es culpa de la cuenta
    assert fila(conn, "la2")[0] is None and fila(conn, "la2")[3] == 1
    fuente_db.actualizar(B, "ERROR", "falla", None, None)
    assert B in fuente_db.pendientes()
    fuente_db.actualizar(B, "ERROR", "falla", None, None)
    assert fila(conn, "la2")[:4] == ("FAILED", "ERROR: falla (3 intentos)", "ERROR", 3), fila(conn, "la2")
    assert B not in fuente_db.pendientes(), "FAILED ya no se reintenta"

    # La cola usa el indice parcial de pendientes (status IS NULL)
    conn.execute("SET enable_seqscan = off")
    plan = "\n".join(r[0] for r in conn.execute(
        "EXPLAIN " + fuente_db.SQL_PENDIENTES, {"limite": None, "vence_min": 30}).fetchall())
    assert "idx_usuarios_traxion_pendientes" in plan, plan
    conn.execute("RESET enable_seqscan")

    # FK: si la app borra el vinculo, la fila del bot se borra sola
    conn.execute("DELETE FROM public.linked_accounts WHERE id = 'la6'")
    assert fila(conn, "la6") is None

    # Vista de consulta humana con datos de la app (incluye las columnas de reserva)
    assert conn.execute("SELECT comercio, estado_vinculo, status, reclamado_por FROM bot.v_usuarios_traxion"
                        " WHERE linked_account_id = 'la1'").fetchone() == ("Kiosco Uno", "active", "SUCCESS", None)

    # El CHECK no deja escribir un status inventado
    try:
        conn.execute("UPDATE bot.usuarios_traxion SET status = 'OK' WHERE linked_account_id = 'la1'")
        raise AssertionError("el CHECK de status deberia haber fallado")
    except psycopg.errors.CheckViolation:
        pass

# El "with" cerro la conexion: el singleton se tiene que reconectar solo en la siguiente consulta
assert conn.closed
assert fuente_db.pendientes() == fuente_db.pendientes()
assert db.conexion() is not conn and not db.conexion().closed
db.cerrar()
assert db.conexion().execute("SELECT 1").fetchone() == (1,), "despues de cerrar() vuelve a conectar"

# ============================================================== punto 4: misma cuenta de Traxion, dos vinculos
admin = db.conexion()
admin.execute("INSERT INTO public.users VALUES ('u10','j@x.com','Comercio A'), ('u11','k@x.com','Comercio B')")
admin.execute("INSERT INTO public.linked_accounts (id, user_id, provider, status, external_user_id) VALUES"
              " ('la10','u10','traxion','active','usuario_compartido'),"
              " ('la11','u11','traxion','active','usuario_compartido')")
fuente_db.sincronizar()
V10, V11 = Cuenta("la10", "usuario_compartido"), Cuenta("la11", "usuario_compartido")
assert V10 in fuente_db.pendientes() and V11 in fuente_db.pendientes(), "son dos trabajos distintos"
fuente_db.actualizar(V10, "NO_ENCONTRADO", "x", None, None)
fuente_db.actualizar(V11, "OK", "", "555", "0.00")
assert fila(admin, "la10")[0] == "FAILED" and fila(admin, "la11")[0] == "SUCCESS", \
    "cada vinculo guarda su propio resultado: uno no pisa al otro"

# ============================================================== punto 4: reserva por fila
admin.execute("INSERT INTO public.users VALUES ('u12','l@x.com','Reserva')")
admin.execute("INSERT INTO public.linked_accounts (id, user_id, provider, status, external_user_id)"
              " VALUES ('la12','u12','traxion','active','reservable')")
fuente_db.sincronizar()
R = Cuenta("la12", "reservable")
yo = fuente_db.PROCESO

assert fuente_db.reclamar(R), "la primera reserva tiene que funcionar"
assert fuente_db.reclamar(R), "el mismo proceso la puede volver a reservar (reintento en el mismo lote)"
assert R not in fuente_db.pendientes(), "una cuenta reservada no aparece en la cola de otro"

fuente_db.PROCESO = "otra-maquina:999:otro"           # ---- ahora hace de OTRO proceso
assert not fuente_db.reclamar(R), "otro proceso no la puede tomar mientras esta reservada"
assert not fuente_db.actualizar(R, "OK", "", None, None), "otro proceso no puede pisar su resultado"
assert fila(admin, "la12")[0] is None
fuente_db.PROCESO = yo                                 # ---- vuelve a ser este proceso

assert fuente_db.actualizar(R, "PRUEBA", "", None, None), "el duenio de la reserva guarda el resultado"
assert admin.execute("SELECT reclamado_en, reclamado_por FROM bot.usuarios_traxion WHERE linked_account_id = 'la12'"
                     ).fetchone() == (None, None), "guardar el resultado libera la reserva"
assert R in fuente_db.pendientes(), "PRUEBA sigue pendiente y, ya liberada, vuelve a la cola"

# Si el proceso que la reservo murio, la reserva vence sola
admin.execute("UPDATE bot.usuarios_traxion SET reclamado_en = now() - interval '31 minutes',"
              " reclamado_por = 'proceso-muerto' WHERE linked_account_id = 'la12'")
assert R in fuente_db.pendientes(), "reserva vencida: vuelve a la cola"
assert fuente_db.reclamar(R), "reserva vencida: otro proceso la puede tomar"
fuente_db.actualizar(R, "PRUEBA", "", None, None)

# Una cuenta ya resuelta no se puede reservar
assert not fuente_db.reclamar(V11), "SUCCESS no se reserva"

# ============================================================== punto 4: candado entre procesos
otro = conectar_aparte()
assert db.tomar_candado(), "la primera corrida toma el candado"
assert db.tomar_candado(), "pedirlo de nuevo en el mismo proceso no falla"
assert otro.execute("SELECT pg_try_advisory_lock(%s)", (fuente_db.CANDADO_ID,)).fetchone()[0] is False, \
    "otro proceso NO puede tomarlo mientras la primera corrida trabaja"
db.cerrar()
assert otro.execute("SELECT pg_try_advisory_lock(%s)", (fuente_db.CANDADO_ID,)).fetchone()[0] is True, \
    "al cerrarse la conexion (o morir el proceso) el candado se libera solo"
assert not db.tomar_candado(), "ahora este proceso es el que no lo consigue"
otro.execute("SELECT pg_advisory_unlock(%s)", (fuente_db.CANDADO_ID,))
db.cerrar()

# Reconexion: si se cae la conexion, al reconectar se recupera el candado...
assert db.tomar_candado()
otro.execute("SELECT pg_terminate_backend(%s)", (db.conexion().info.backend_pid,))
try:
    db.ejecutar("SELECT 1")
    raise AssertionError("la conexion terminada tendria que fallar")
except psycopg.OperationalError:
    pass
assert db.ejecutar("SELECT 1").fetchone() == (1,), "reconecta"
assert otro.execute("SELECT pg_try_advisory_lock(%s)", (fuente_db.CANDADO_ID,)).fetchone()[0] is False, \
    "y al reconectar volvio a tomar el candado"

# ...y si otra corrida lo agarro mientras tanto, corta en vez de seguir sin proteccion
otro.execute("SELECT pg_terminate_backend(%s)", (db.conexion().info.backend_pid,))
try:
    db.ejecutar("SELECT 1")
except psycopg.OperationalError:
    pass
assert otro.execute("SELECT pg_try_advisory_lock(%s)", (fuente_db.CANDADO_ID,)).fetchone()[0] is True
try:
    db.ejecutar("SELECT 1")
    raise AssertionError("tendria que haber lanzado CandadoPerdido")
except fuente_db.CandadoPerdido:
    pass
otro.execute("SELECT pg_advisory_unlock(%s)", (fuente_db.CANDADO_ID,))
db.cerrar()

# ============================================================== vaciar_cola (lo que corre el Worker)
import traxion_bot  # noqa: E402

admin = db.conexion()
admin.execute("UPDATE bot.usuarios_traxion SET status = 'SUCCESS' WHERE status IS NULL")  # cola limpia
admin.execute("INSERT INTO public.users VALUES ('u7','g@x.com','Siete'), ('u8','h@x.com','Ocho'), ('u9','i@x.com','Nueve')")
admin.execute("INSERT INTO public.linked_accounts (id, user_id, provider, status, external_user_id) VALUES"
              " ('la7','u7','traxion','active','siete'), ('la8','u8','traxion','active','ocho')")
lotes = []


def correr_lote_falso(trabajos, guardar, headless):
    lotes.append(usuarios(trabajos))
    if len(lotes) == 1:  # mientras procesa el primer lote, la app vincula una cuenta nueva
        fuente_db.BaseDatos().ejecutar("INSERT INTO public.linked_accounts (id, user_id, provider, status,"
                                       " external_user_id) VALUES ('la9','u9','traxion','active','nueve')")
    resultados = {}
    for t in trabajos:
        assert fuente_db.reclamar(t)
        resultados[t] = "TIMEOUT_BUSQUEDA" if t.usuario == "ocho" else "OK"  # "ocho" tiene una falla pasajera
        fuente_db.actualizar(t, resultados[t], "", None, None)
    return resultados


traxion_bot.correr_lote = correr_lote_falso

# Si otra corrida tiene el candado, este despertar no hace nada (ni siquiera sincroniza)
assert otro.execute("SELECT pg_try_advisory_lock(%s)", (fuente_db.CANDADO_ID,)).fetchone()[0] is True
assert traxion_bot.vaciar_cola(guardar=True, headless=True) == {} and lotes == []
otro.execute("SELECT pg_advisory_unlock(%s)", (fuente_db.CANDADO_ID,))

conteo = traxion_bot.vaciar_cola(guardar=True, headless=True)
assert lotes == [["siete", "ocho"], ["nueve"]], lotes  # toma la que llego mientras trabajaba
assert conteo == {"OK": 2, "TIMEOUT_BUSQUEDA": 1}, conteo
# "ocho" sigue pendiente para el proximo despertar, pero NO se reintento en loop en este
assert fuente_db.pendientes() == [Cuenta("la8", "ocho")]
assert fila(db.conexion(), "la8")[:4] == (None, "TIMEOUT_BUSQUEDA", "TIMEOUT_BUSQUEDA", 1)
assert otro.execute("SELECT pg_try_advisory_lock(%s)", (fuente_db.CANDADO_ID,)).fetchone()[0] is True, \
    "al terminar, vaciar_cola libera el candado"
otro.execute("SELECT pg_advisory_unlock(%s)", (fuente_db.CANDADO_ID,))
db.cerrar()

# ============================================================== punto 1: permisos minimos (sql/02)
sql02 = (RAIZ / "sql" / "02_usuario_bot.sql").read_text(encoding="utf-8").replace(" traxion_bot", f" {ROL_BOT}")
db.conexion().execute(sql02)
db.cerrar()
config.DB_USER = ROL_BOT                                      # ---- desde aca, el bot corre con su usuario
try:
    admin2 = conectar_aparte()
    admin2.execute("INSERT INTO public.users VALUES ('u13','m@x.com','Permisos')")
    admin2.execute("INSERT INTO public.linked_accounts (id, user_id, provider, status, external_user_id, metadata_json)"
                   " VALUES ('la13','u13','traxion','active','con_permisos_minimos', '{\"token\": \"secreto\"}')")
    P = Cuenta("la13", "con_permisos_minimos")

    # Todo lo que el bot necesita funciona con sus permisos minimos (incluida la FK y el candado)
    assert fuente_db.sincronizar() == 1
    assert P in fuente_db.pendientes()
    assert fuente_db.reclamar(P)
    assert fuente_db.actualizar(P, "OK", "", "777", "0.00")
    assert db.tomar_candado()
    db.cerrar()

    bot_conn = conectar_aparte(ROL_BOT)
    prohibidas = {
        "public.users (datos personales)": "SELECT email FROM public.users",
        "otra columna de linked_accounts": "SELECT metadata_json FROM public.linked_accounts",
        "SELECT * de linked_accounts": "SELECT * FROM public.linked_accounts",
        "la vista de consulta humana": "SELECT * FROM bot.v_usuarios_traxion",
        "modificar linked_accounts": "UPDATE public.linked_accounts SET status = 'x' WHERE id = 'la13'",
        "borrar filas del bot": "DELETE FROM bot.usuarios_traxion WHERE linked_account_id = 'la13'",
    }
    for que, sql in prohibidas.items():
        try:
            bot_conn.execute(sql)
            raise AssertionError(f"el usuario del bot NO deberia poder: {que}")
        except psycopg.errors.InsufficientPrivilege:
            pass
    bot_conn.close()
    admin2.close()
finally:
    config.DB_USER = ADMIN
    db.cerrar()

otro.close()
print("TEST FUENTE_DB OK")
