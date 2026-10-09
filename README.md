# Automatización Traxion – Monto crédito

Bot en Python + [Playwright](https://playwright.dev/python/) que entra a la plataforma **Traxion (Pago24)** y, para cada usuario de una lista:

1. Va a **GESTORES → Cuentas → pestaña Usuario** y busca el usuario.
2. Abre la cuenta con el lápiz de la columna **M** (Modificar).
3. Setea **Monto crédito = 1** y aprieta **Aceptar** (solo si `GUARDAR=true`).

Cada usuario queda registrado en `resultados.db` (SQLite) con su estado, aunque no exista o la plataforma no responda. El bot nunca se queda trabado en un usuario: lo registra y sigue con el siguiente.

---

## Requisitos

- Python **3.10 o superior**
- Acceso de red al entorno de Traxion (testing, stage o productivo)
- Un usuario de la plataforma con permiso para modificar cuentas en GESTORES

## Instalación

```bash
git clone https://github.com/SantiGarnier/automatizacion-traxion.git
cd automatizacion-traxion

python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

Después hay que instalar el navegador que usa Playwright:

```bash
# Windows
python -m playwright install chromium

# Linux (servidor): instala también las librerías del sistema que necesita Chromium (pide sudo)
python -m playwright install --with-deps chromium
```

## Configuración

Copiar la plantilla y completar las credenciales:

```bash
cp .env.example .env        # Linux
copy .env.example .env      # Windows
```

> El `.env` tiene la clave de la plataforma: **está excluido de git y no se debe subir nunca**. Las credenciales solo van en el `.env`, nunca en `.env.example`.

| Variable | Default | Descripción |
|---|---|---|
| `TRAXION_URL` | `https://testing.telerecargas.com.ar/` | URL del entorno (testing / stage / productivo) |
| `TRAXION_USER` / `TRAXION_PASS` | — | Credenciales de la plataforma |
| `USUARIOS` | vacío | Lista de usuarios separada por comas |
| `USUARIO_BUSQUEDA` | — | Usuario único, si `USUARIOS` está vacío |
| `MONTO_CREDITO` | `1` | Valor a cargar en "Monto crédito" |
| `GUARDAR` | `false` | `false` = carga el monto pero **no** aprieta Aceptar (modo prueba) |
| `HEADLESS` | `false` | `true` = sin ventana de navegador (**usar `true` en el servidor**) |
| `TIMEOUT_BUSQUEDA_MS` | `3000` | Si la búsqueda de un usuario no responde en este tiempo, se registra `TIMEOUT_BUSQUEDA` y se sigue |
| `REINTENTAR_TIMEOUTS` | `true` | Al final del lote, reintenta una vez los `TIMEOUT_BUSQUEDA` |
| `TIMEOUT_MS` | `60000` | Tope de seguridad para el resto de los pasos (solo corta si el servidor no responde nunca) |
| `DB_HOST` / `DB_PORT` | `localhost` / `5432` | Servidor PostgreSQL |
| `DB_NAME` | vacío | Base de la aplicación. Si queda vacío, el bot no usa la base. Con espacios, entre comillas: `DB_NAME="Pago24 Red"` |
| `DB_USER` / `DB_PASS` | — | Usuario de la base (ver `sql/02_usuario_bot.sql`) |
| `MAX_INTENTOS` | `3` | Fallas pasajeras (`ERROR`, `TIMEOUT_BUSQUEDA`) que se aceptan en corridas sucesivas antes de marcar la cuenta `FAILED` |
| `SYNC_MARGEN_MIN` | `60` | Margen hacia atrás de la sincronización incremental, en minutos |

## Base de datos

Los usuarios a procesar salen de la tabla de la aplicación **`public.linked_accounts`**, en las filas con `provider = 'traxion'`. El usuario que se busca en Traxion es la columna **`external_user_id`**.

El bot **nunca modifica las tablas de la aplicación**. Trabaja sobre su propia tabla **`bot.usuarios_traxion`**, en un esquema aparte. Esa tabla funciona como el "flag de procesamiento" de cada cuenta y tiene una **FK** a `linked_accounts.id`:

| `status` | Significado |
|---|---|
| `NULL` | Pendiente: el bot la procesa en la próxima corrida |
| `SUCCESS` | "Monto crédito" quedó en 1 (o ya lo tenía) |
| `FAILED` | No se pudo; el motivo está en `status_message` |

1. Al arrancar, hace una **sincronización incremental**: importa solo las cuentas de Traxion creadas después de la última importada (marca de agua sobre `linked_accounts.created_at`). No reescribe las que ya están. Por si alguna transacción confirma tarde, vuelve a mirar `SYNC_MARGEN_MIN` minutos hacia atrás; las repetidas se ignoran. Las nuevas entran con `status` en `NULL`.
2. Procesa las que tienen **`status` en `NULL`**. Las busca con un **índice parcial** que solo contiene las pendientes, así la cola no crece con el historial. Solo toma vínculos `active`; ese estado se lee **en vivo** de `linked_accounts` por la FK, así que si la app desactiva una cuenta, sale de la cola.
3. Después de cada cuenta guarda el resultado:

| Resultado del bot (`resultado`) | `status` |
|---|---|
| `OK`, `SIN_CAMBIOS` | `SUCCESS` |
| `NO_ENCONTRADO`, `VARIAS_CUENTAS` | `FAILED` (reintentar no cambia nada) |
| `ERROR`, `TIMEOUT_BUSQUEDA` (fallas pasajeras) | Sigue en `NULL` y se reintenta en la próxima corrida. A los `MAX_INTENTOS` intentos pasa a `FAILED` |
| `PRUEBA` (`GUARDAR=false`), `NO_PROCESADO` | Sigue en `NULL`, sin sumar intentos |

También guarda `intentos`, `cuenta_numero`, `monto_anterior` y `procesado_en`.

Si la app borra un vínculo, su fila del bot se borra sola (`ON DELETE CASCADE`).

Para consultar todo junto, con el comercio y el estado del vínculo, está la vista **`bot.v_usuarios_traxion`**:

```sql
SELECT * FROM bot.v_usuarios_traxion ORDER BY procesado_en DESC NULLS LAST;

-- Las que fallaron y por que
SELECT usuario_traxion, comercio, status_message FROM bot.v_usuarios_traxion WHERE status = 'FAILED';
```

Para forzar que una cuenta se vuelva a procesar:

```sql
UPDATE bot.usuarios_traxion SET status = NULL, status_message = NULL, intentos = 0 WHERE usuario_traxion = '...';
```

Puesta en marcha en una base nueva (la corre un administrador):

1. `sql/01_bot_usuarios_traxion.sql`: crea el esquema `bot`, la tabla y hace la primera sincronización.
2. `sql/02_usuario_bot.sql`: crea el usuario `traxion_bot`, con lectura sobre las tablas de la aplicación y escritura solo sobre su tabla.

## Uso

```bash
# Probar solo el login
python traxion_bot.py --solo-login

# Lote en modo prueba (no guarda): cuentas pendientes de la base (o usuarios del .env si DB_NAME está vacío)
python traxion_bot.py

# Solo las primeras 2 cuentas pendientes (útil para probar)
python traxion_bot.py --limite 2

# Usuarios sueltos o desde un archivo (uno por línea, # para comentarios). No usan ni actualizan la base.
python traxion_bot.py --usuario abc123 --usuario def456
python traxion_bot.py --archivo usuarios.txt

# Guardar de verdad y sin ventana (como en el servidor)
python traxion_bot.py --archivo usuarios.txt --guardar --headless
```

El código de salida es `0` si el lote terminó (aunque haya usuarios no encontrados) y `1` si no pudo ni siquiera loguearse.

## Resultados

Con base de datos, el **estado actual** de cada cuenta queda en `bot.usuarios_traxion` (ver arriba). Además, en todos los casos, cada intento queda como una fila en la tabla `ejecuciones` de `resultados.db` (SQLite local), que sirve de historial:

| Estado | Significado |
|---|---|
| `OK` | Se guardó el monto |
| `SIN_CAMBIOS` | Ya tenía el monto correcto, no se tocó |
| `PRUEBA` | Se cargó el monto pero no se guardó (`GUARDAR=false`) |
| `NO_ENCONTRADO` | La búsqueda devolvió "Datos: 0" |
| `TIMEOUT_BUSQUEDA` | La búsqueda no respondió dentro de `TIMEOUT_BUSQUEDA_MS` |
| `VARIAS_CUENTAS` | La búsqueda trajo más de una cuenta; no se toca nada |
| `ERROR` | Otro problema (ver columna `detalle` y la carpeta `debug/`) |
| `NO_PROCESADO` | El bot no llegó a procesarlo (no pudo volver a loguearse) |

Columnas: `fecha`, `entorno`, `usuario_busqueda`, `cuenta_numero`, `cuenta_nombre`, `monto_anterior`, `monto_nuevo`, `guardado`, `estado`, `detalle`, `lote` (una corrida), `intento` (1, o 2 si fue reintento).

Consultas útiles:

```sql
-- Estado final de cada usuario de la última corrida
SELECT usuario_busqueda, estado, detalle FROM ejecuciones
WHERE id IN (SELECT MAX(id) FROM ejecuciones
             WHERE lote = (SELECT MAX(lote) FROM ejecuciones)
             GROUP BY usuario_busqueda);

-- Usuarios que no existen en la plataforma
SELECT DISTINCT usuario_busqueda FROM ejecuciones WHERE estado = 'NO_ENCONTRADO';
```

Además:
- `logs/bot_AAAAMMDD.log`: log de cada día (lo mismo que se ve en consola).
- `debug/`: captura de pantalla + HTML de la página cada vez que un usuario termina en error.

## Boss y Worker (punto de entrada HTTP)

```
p24red  --POST /despertar-->  Boss  --despierta-->  Worker  --status NULL -> SUCCESS/FAILED-->  base
```

En el servidor, el bot corre como un proceso (`servidor.py`) con dos partes:

- **Boss:** recibe los avisos y sabe si el Worker está despierto o dormido. Si duerme, lo despierta. Si ya está trabajando, no arranca otro: le anota que dé una vuelta más al terminar. Cuando el Worker termina y no hay vuelta pendiente, lo manda a dormir.
- **Worker:** toma las cuentas con `status` en `NULL` de `bot.usuarios_traxion`, las procesa en Traxion, guarda `SUCCESS` o `FAILED`, y revisa la base hasta que no quedan pendientes.

Mientras el Worker duerme no se consulta la base ni la plataforma, así que no consume recursos. La app de Pago24 Red despierta al Worker cuando se vincula un usuario nuevo.

```bash
python servidor.py
```

| Llamada | Respuesta |
|---|---|
| `POST /despertar` | `202 {"estado": "despertando"}` si el Worker dormía: arranca en segundo plano y responde enseguida |
|  | `200 {"estado": "ya_despierto"}` si ya estaba trabajando: el Boss no arranca otro, pero al terminar le hace dar **una** vuelta más para tomar lo que haya entrado mientras tanto |
| `GET /estado` | `{"estado": "dormido" \| "despierto", "ultima_vuelta": {inicio, fin, ok, resultados \| error}}` |
| `GET /salud` | `{"ok": true}`, para monitoreo. Es la única que no pide token |

Todas las llamadas (menos `/salud`) llevan el header **`X-Token`** con el valor de `API_TOKEN` del `.env`:

```bash
curl -X POST -H "X-Token: $API_TOKEN" http://localhost:8080/despertar
curl -H "X-Token: $API_TOKEN" http://localhost:8080/estado
```

Mientras el Worker está despierto:
- Sincroniza, procesa los pendientes y repite hasta vaciar la cola. Así también toma las cuentas que se vinculan mientras trabaja.
- Cada cuenta se procesa **como máximo una vez por despertar**. Si una tiene una falla pasajera, queda en `NULL` para el próximo despertar. Si no fuera así, con Traxion caído el Worker reintentaría la misma cuenta en loop y nunca se dormiría. A los `MAX_INTENTOS` fallos pasa a `FAILED`.
- Usa `GUARDAR` y `HEADLESS` del `.env`. En el servidor: `GUARDAR=true` y `HEADLESS=true`.

Variables: `API_HOST` (`127.0.0.1` = solo la misma máquina; `0.0.0.0` = acepta llamadas de otras), `API_PORT` (default `8080`) y `API_TOKEN` (obligatorio, mínimo 16 caracteres). Para generar un token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Si el puerto se expone fuera de la red interna, conviene ponerlo detrás de HTTPS (por ejemplo un reverse proxy), porque el token viaja en el header.

## Correrlo programado en el servidor

Alternativa al Boss/Worker, si se prefiere correrlo a horario fijo:

Con `HEADLESS=true` en el `.env`, alcanza con ejecutar el script con el Python del entorno virtual:

```bash
# Linux (cron), todos los días a las 7:00
0 7 * * * cd /ruta/automatizacion-traxion && .venv/bin/python traxion_bot.py --guardar >> logs/cron.log 2>&1
```

En Windows, lo mismo con el **Programador de tareas**:
- Programa: `C:\ruta\automatizacion-traxion\.venv\Scripts\python.exe`
- Argumentos: `traxion_bot.py --guardar`
- Iniciar en: `C:\ruta\automatizacion-traxion`

## Tests

La lógica del lote (re-login, reintentos, registro), sin navegador ni plataforma:

```bash
python tests/test_orquestacion.py
```

El Boss/Worker HTTP (token, despierto/dormido, vuelta extra, errores), con el Worker simulado:

```bash
python tests/test_servidor.py
```

La parte de PostgreSQL: sincronización incremental, FK, cola con índice parcial y reintentos. El test **borra y recrea** las tablas en la base que le indiques, así que solo corre si esa base se llama `prueba*` o `test*`. Necesita una base vacía:

```bash
# variables TEST_DB_HOST, TEST_DB_PORT (default 5544), TEST_DB_NAME (default prueba_bot), TEST_DB_USER, TEST_DB_PASS
python tests/test_fuente_db.py
```

## Cómo funciona (para mantenerlo)

- **La plataforma es una app Seaside.** Las URLs llevan `_s` (sesión) y `_k` (continuación), que cambian en cada click. Por eso no se puede ir directo a una pantalla interna: el bot siempre navega con el menú, como una persona.
- **Los campos no tienen nombres fijos** (`name="331"`, `name="660"`… cambian en cada carga). El bot los ubica por su etiqueta (`Usuario`, `Monto credito`).
- **Esperas por evento, no por tiempo.** Cada click en la plataforma hace un AJAX (`X-Requested-With: Ajaxifier`), y al volver la respuesta se reemplaza todo el `<body>`. Antes de cada click el bot inserta un elemento marcador en la página. Cuando la marca desaparece, la pantalla nueva ya está lista. Ver `click_y_esperar()`.
- **Sesión vencida o página trabada:** si aparece la pantalla de login, el bot vuelve a entrar y reintenta ese usuario. Si un usuario deja la página en mal estado (timeout, error), descarta la página y vuelve a loguearse antes del siguiente.
- **Monto crédito** usa una librería de formato numérico (autoNumeric): el bot tipea el valor tecla por tecla y sale del campo con Tab. **No usa Enter**, porque dispara un botón oculto del formulario.
- **Captcha:** si la plataforma lo pide, el bot se frena y lo informa. No lo saltea.
- **Base de datos:** toda la conexión pasa por la clase `BaseDatos` de `fuente_db.py`, un singleton con una única conexión que se arma desde el `.env`. Se conecta recién cuando se usa y se reconecta sola si se cayó. Las consultas van siempre con parámetros, nunca concatenando valores en el SQL, así que no hay riesgo de inyección.

## Pendiente

- Confirmar con Fede qué valores de `linked_accounts.status` hay que procesar (hoy: solo `active`).
- Crear el esquema `bot` y el usuario `traxion_bot` en stage (`sql/01` y `sql/02`).
- Probar punta a punta en **stage** antes de productivo.
