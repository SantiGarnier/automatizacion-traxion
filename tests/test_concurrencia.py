"""
Test de concurrencia con PROCESOS REALES (no hilos) contra un PostgreSQL de PRUEBA.
Reproduce el escenario del punto 4 de Fede: el servidor y un cron corriendo a la vez.

  1. Dos procesos llaman a vaciar_cola() al mismo tiempo -> trabaja uno solo (candado de la base);
     el otro sale sin tocar nada. Cada cuenta se procesa exactamente una vez.
  2. Dos procesos se saltean el candado y compiten por las mismas filas -> la reserva por fila
     hace que cada cuenta la tome uno solo.

El navegador esta simulado (cada cuenta "tarda" 0,3 s). Misma base y mismas variables que
tests/test_fuente_db.py; solo corre contra una base prueba*/test*.

    python tests/test_concurrencia.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
import config  # noqa: E402

config.DB_HOST = os.environ.get("TEST_DB_HOST", "localhost")
config.DB_PORT = int(os.environ.get("TEST_DB_PORT", "5544"))
config.DB_NAME = os.environ.get("TEST_DB_NAME", "prueba_bot")
config.DB_USER = os.environ.get("TEST_DB_USER", "postgres")
config.DB_PASS = os.environ.get("TEST_DB_PASS", "")
if not config.DB_NAME.lower().startswith(("prueba", "test")):
    sys.exit(f"Me niego a correr contra la base '{config.DB_NAME}': el test borra tablas. Usar una base prueba*/test*.")

N_CUENTAS = 8

# Codigo que corre cada proceso hijo. Recibe: modo (lote | sin_candado) y archivo donde anotar lo que hizo.
HIJO = r'''
import json, os, sys, time
sys.path.insert(0, sys.argv[3])
import config
config.DB_HOST, config.DB_PORT, config.DB_NAME, config.DB_USER, config.DB_PASS = json.loads(sys.argv[4])
from pathlib import Path
modo, salida = sys.argv[1], sys.argv[2]
config.LOG_DIR = Path(salida).parent / "logs"
import fuente_db, traxion_bot

procesadas = []

def correr_lote_simulado(trabajos, guardar, headless):
    res = {}
    for t in trabajos:
        if not fuente_db.reclamar(t):
            res[t] = "OTRO_PROCESO"
            continue
        time.sleep(0.3)  # "abrir Traxion y editar la cuenta"
        procesadas.append(t.linked_account_id)
        fuente_db.actualizar(t, "OK", "", None, None)
        res[t] = "OK"
    return res

traxion_bot.correr_lote = correr_lote_simulado
time.sleep(max(0.0, float(os.environ["ARRANQUE"]) - time.time()))  # arrancan los dos a la vez

if modo == "lote":
    traxion_bot.vaciar_cola(guardar=True, headless=True)
else:  # sin_candado: compite directo por las filas, como si alguien se salteara el candado
    traxion_bot._USA_DB = True
    correr_lote_simulado(fuente_db.pendientes(), True, True)
    fuente_db.BaseDatos().cerrar()

with open(salida, "w") as f:
    json.dump({"proceso": fuente_db.PROCESO, "procesadas": procesadas}, f)
'''


def preparar_base(conn):
    conn.execute("""
        DROP SCHEMA IF EXISTS bot CASCADE;
        DROP TABLE IF EXISTS public.linked_accounts, public.users;
        CREATE TABLE public.users (id text PRIMARY KEY, email text NOT NULL, commerce_name text NOT NULL);
        CREATE TABLE public.linked_accounts (
            id text PRIMARY KEY, user_id text NOT NULL REFERENCES public.users(id),
            provider text NOT NULL, status text NOT NULL, external_user_id text,
            created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (user_id, provider));
    """)
    for i in range(N_CUENTAS):
        conn.execute("INSERT INTO public.users VALUES (%s, %s, %s)", (f"u{i}", f"{i}@x.com", f"Comercio {i}"))
        conn.execute("INSERT INTO public.linked_accounts (id, user_id, provider, status, external_user_id)"
                     " VALUES (%s, %s, 'traxion', 'active', %s)", (f"la{i}", f"u{i}", f"usuario_{i}"))
    conn.execute((RAIZ / "sql" / "01_bot_usuarios_traxion.sql").read_text(encoding="utf-8"))


def correr_dos(modo):
    tmp = Path(tempfile.mkdtemp())
    script = tmp / "hijo.py"
    script.write_text(HIJO, encoding="utf-8")
    datos_db = json.dumps([config.DB_HOST, config.DB_PORT, config.DB_NAME, config.DB_USER, config.DB_PASS])
    entorno = dict(os.environ, ARRANQUE=str(time.time() + 2.0))
    hijos = [subprocess.Popen([sys.executable, str(script), modo, str(tmp / f"r{i}.json"), str(RAIZ), datos_db],
                              env=entorno, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
             for i in range(2)]
    salidas = [h.communicate(timeout=60)[0] for h in hijos]
    for h, s in zip(hijos, salidas):
        assert h.returncode == 0, s
    return [json.loads((tmp / f"r{i}.json").read_text()) for i in range(2)], salidas


import fuente_db  # noqa: E402

db = fuente_db.BaseDatos()
todas = {f"la{i}" for i in range(N_CUENTAS)}

# ---- 1. servidor + cron a la vez: el candado deja trabajar a uno solo
preparar_base(db.conexion())
db.cerrar()
(r1, r2), salidas = correr_dos("lote")
hechas = r1["procesadas"] + r2["procesadas"]
trabajaron = [r for r in (r1, r2) if r["procesadas"]]
assert len(trabajaron) == 1, f"tendria que trabajar un solo proceso: {r1} {r2}"
assert sorted(hechas) == sorted(todas), f"cada cuenta exactamente una vez: {hechas}"
assert any("Otra corrida del bot esta en curso" in s for s in salidas), salidas
print(f"1. candado OK: trabajo un solo proceso ({len(hechas)} cuentas), el otro salio sin tocar nada")

# ---- 2. sin candado, compitiendo por las mismas filas: la reserva evita el doble procesamiento
preparar_base(db.conexion())
db.cerrar()
(r1, r2), _ = correr_dos("sin_candado")
hechas = r1["procesadas"] + r2["procesadas"]
assert len(hechas) == len(set(hechas)), f"una cuenta la procesaron los dos procesos: {r1} {r2}"
assert set(hechas) == todas, f"quedaron cuentas sin procesar: {todas - set(hechas)}"
print(f"2. reserva OK: {len(r1['procesadas'])} + {len(r2['procesadas'])} cuentas, ninguna repetida")

with db.conexion() as conn:
    pendientes = conn.execute("SELECT COUNT(*) FROM bot.usuarios_traxion WHERE status IS NULL").fetchone()[0]
    reservas = conn.execute("SELECT COUNT(*) FROM bot.usuarios_traxion WHERE reclamado_por IS NOT NULL").fetchone()[0]
assert pendientes == 0 and reservas == 0, (pendientes, reservas)
db.cerrar()
print("TEST CONCURRENCIA OK")
