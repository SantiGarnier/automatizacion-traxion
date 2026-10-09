"""
Bot Traxion (Pago24) - TRASA

Flujo (por cada usuario de la lista, con un solo login):
  1. Login con las credenciales del .env
  2. GESTORES -> Cuentas -> pestaña "Usuario"
  3. Busca el usuario (si no responde en TIMEOUT_BUSQUEDA_MS, se registra y se sigue)
  4. Aprieta el lapiz naranja de la columna "M"
  5. Setea "Monto credito" = 1 (y guarda solo si GUARDAR=true / --guardar)
Al final reintenta una vez los TIMEOUT_BUSQUEDA (si REINTENTAR_TIMEOUTS=true).

Cada usuario queda registrado en resultados.db con uno de estos estados:
  OK               se guardo el monto
  SIN_CAMBIOS      ya tenia el monto correcto
  PRUEBA           se cargo el monto pero no se guardo (GUARDAR=false)
  NO_ENCONTRADO    la busqueda devolvio "Datos: 0"
  TIMEOUT_BUSQUEDA la busqueda no respondio dentro de TIMEOUT_BUSQUEDA_MS
  VARIAS_CUENTAS   la busqueda devolvio mas de una cuenta (no se toca nada)
  ERROR            cualquier otro problema (ver columna detalle y carpeta debug/)
  NO_PROCESADO     el bot no llego a procesarlo (ej. no pudo volver a loguearse)

Notas de la plataforma:
  - Es una app Seaside: las URLs llevan _s (sesion) y _k (continuacion) que cambian
    en cada click, asi que NO se puede ir directo a una URL interna: siempre se navega
    clickeando el menu, como haria una persona.
  - Los "name" de los inputs son numeros dinamicos (6, 7, 8...). Usar ids o labels.
  - Navegacion (ajaxifier_v4.7.7.js de la plataforma): cada click en un link o submit hace un
    $.ajax con header "X-Requested-With: Ajaxifier" y al volver la respuesta reemplaza TODO el
    <body>. El bot espera ESE evento (ver click_y_esperar), nunca un tiempo fijo.
    Los links con atributo disable_ajaxifier (menu GESTORES, pestañas) no van al servidor.
"""
import argparse
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import psycopg
from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout, sync_playwright

import config
import fuente_db


class BotError(Exception):
    estado = "ERROR"


class SinRespuesta(BotError):
    """La plataforma no reemplazo la pagina dentro del tope (queda 'loading')."""


class TimeoutBusqueda(SinRespuesta):
    estado = "TIMEOUT_BUSQUEDA"


class NoEncontrado(BotError):
    estado = "NO_ENCONTRADO"


class VariasCuentas(BotError):
    estado = "VARIAS_CUENTAS"


class SesionVencida(BotError):
    """Despues de un click aparecio la pantalla de login: no se registra, se re-loguea y se reintenta."""


# Despues de estos estados la pagina quedo en un lugar conocido (la grilla) y se puede seguir.
# Con cualquier otro error la pagina puede quedar trabada (overlay de carga, respuesta tardia que
# pisa el body, pantalla inesperada): se descarta y se vuelve a loguear antes del siguiente usuario.
ESTADOS_SIN_RELOGIN = ("OK", "SIN_CAMBIOS", "PRUEBA", "NO_ENCONTRADO", "VARIAS_CUENTAS")


# --------------------------------------------------------------------------- utils

def log(msg: str) -> None:
    linea = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(linea, flush=True)
    try:
        config.LOG_DIR.mkdir(exist_ok=True)
        with open(config.LOG_DIR / f"bot_{datetime.now():%Y%m%d}.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d} {linea}\n")
    except OSError:
        pass  # que un problema con el archivo de log nunca frene el bot


_MARCA_ID = "__bot_marca"


def click_y_esperar(page: Page, elemento, accion: str, timeout: int | None = None,
                    error_cls: type[SinRespuesta] = SinRespuesta, chequear_sesion: bool = True) -> None:
    """
    Clickea y espera a que la plataforma reemplace la pagina, por evento y no por tiempo:
      1. Antes del click se inserta un <div> marcador dentro del <body>.
      2. El ajaxifier, al recibir la respuesta, hace $("body").html(...) -> el marcador desaparece.
         (Si en vez de AJAX hubiera una navegacion completa, el marcador tambien desaparece.)
      3. Apenas el marcador no existe mas, la pagina nueva ya esta en el DOM y se sigue.
    `timeout` es el tope: si se cumple sin respuesta se lanza `error_cls`.
    Por defecto es TIMEOUT_MS (tope de seguridad); la busqueda usa uno corto.
    Si la pagina nueva es el login, la sesion vencio -> SesionVencida.
    """
    page.evaluate(
        "id => { const d = document.createElement('div'); d.id = id; d.hidden = true; document.body.appendChild(d); }",
        _MARCA_ID,
    )
    respuestas = []

    def _capturar(resp):
        if resp.request.headers.get("x-requested-with") == "Ajaxifier":
            respuestas.append(resp)

    page.on("response", _capturar)
    try:
        elemento.click()
        page.wait_for_function(f"() => !document.getElementById('{_MARCA_ID}')", timeout=timeout)
    except PWTimeout:
        ms = timeout if timeout is not None else config.TIMEOUT_MS
        raise error_cls(f"{accion}: la plataforma no respondio en {ms / 1000:g} s") from None
    finally:
        page.remove_listener("response", _capturar)

    if respuestas and respuestas[-1].status >= 400:
        raise BotError(f"{accion}: el servidor respondio HTTP {respuestas[-1].status}")
    if chequear_sesion and es_pantalla_login(page):
        raise SesionVencida(f"{accion}: la sesion vencio (aparecio el login)")
    # el overlay "Un momento por favor..." (blockUI) se va con el body viejo; por las dudas
    page.locator(".blockUI").first.wait_for(state="detached")


def es_pantalla_login(page: Page) -> bool:
    return bool(page.locator("#usuario").count() and page.locator("#password").count()
                and page.get_by_role("button", name="Ingresar").count())


def guardar_debug(page: Page, etiqueta: str) -> None:
    """Guarda screenshot + HTML para poder ajustar selectores cuando algo falla."""
    config.DEBUG_DIR.mkdir(exist_ok=True)
    etiqueta = re.sub(r"[^\w.-]", "_", etiqueta)[:80]
    base = config.DEBUG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{etiqueta}"
    try:
        page.screenshot(path=f"{base}.png", full_page=True)
        base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
        log(f"Debug guardado en {base}.png / .html")
    except Exception as e:  # noqa: BLE001 - el debug nunca debe tapar el error original
        log(f"No se pudo guardar debug: {e}")


def mensajes_visibles(page: Page) -> list[str]:
    """Textos de alertas/notificaciones visibles (errores de login, confirmaciones, etc.)."""
    selector = ".alert, .jGrowl-notification, .sweet-alert, .help-block, .text-danger, .ui-dialog, .toast"
    textos = []
    for el in page.locator(selector).all():
        try:
            if el.is_visible():
                t = el.inner_text().strip()
                if t:
                    textos.append(" ".join(t.split()))
        except Exception:  # noqa: BLE001
            pass
    return textos


def a_numero(valor: str) -> float | None:
    """'1.00' / '1,00' / '1.234,56' -> float."""
    v = (valor or "").strip().replace("$", "").replace(" ", "")
    if not v:
        return None
    if "," in v and "." in v:
        v = v.replace(".", "").replace(",", ".") if v.rfind(",") > v.rfind(".") else v.replace(",", "")
    else:
        v = v.replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


# --------------------------------------------------------------------------- sesion

class Sesion:
    """La pagina logueada. Se puede descartar y volver a loguear cuando queda en mal estado."""

    def __init__(self, context: BrowserContext):
        self.context = context
        self.page: Page | None = None
        self.sucia = False  # True = la pagina quedo en mal estado; re-loguear antes de seguir

    def iniciar(self) -> None:
        self.sucia = False
        if self.page is not None:
            try:
                self.page.close()
            except Exception:  # noqa: BLE001
                pass
        self.page = self.context.new_page()
        # confirm()/alert() de JS: Playwright los cancela por defecto; los aceptamos y los logueamos
        self.page.on("dialog", lambda d: (log(f"Dialogo JS: {d.message!r} -> aceptar"), d.accept()))
        login(self.page)


def login(page: Page) -> None:
    log(f"Abriendo {config.TRAXION_URL}")
    page.goto(config.TRAXION_URL, wait_until="domcontentloaded")
    page.locator("#usuario").wait_for(state="visible")

    page.locator("#usuario").fill(config.TRAXION_USER)
    page.locator("#password").fill(config.TRAXION_PASS)
    click_y_esperar(page, page.get_by_role("button", name="Ingresar"), "Login", chequear_sesion=False)

    # La pagina ya cambio: si no esta el menu GESTORES, el login fallo
    if not page.get_by_text("GESTORES", exact=True).count():
        if page.locator("iframe[src*='recaptcha']").count():
            raise BotError("La plataforma pidio captcha. Hay que resolverlo a mano; el bot no lo saltea.")
        detalle = " | ".join(mensajes_visibles(page)) or "sin mensaje visible"
        raise BotError(f"Login fallido ({detalle})")

    log(f"Login OK como '{config.TRAXION_USER}'")


# --------------------------------------------------------------------------- pasos

def ir_a_cuentas(page: Page) -> None:
    if es_pantalla_login(page):
        raise SesionVencida("La sesion vencio (la pagina muestra el login)")
    # Ojo: PROVEEDORES tambien tiene un submenu "Cuentas"; hay que tomar el de GESTORES
    gestores = page.locator(
        "xpath=//li[contains(@class,'nav-item')][a/span[contains(@class,'title') and normalize-space()='GESTORES']]"
    ).first
    cuentas = gestores.locator("xpath=.//ul[contains(@class,'sub-menu')]//a[normalize-space()='Cuentas']").first
    # GESTORES solo despliega el submenu en el navegador (no va al servidor). Si ya esta abierto
    # (pasa desde el 2do usuario) no se clickea, porque clickearlo lo cerraria.
    if not cuentas.is_visible():
        gestores.locator("xpath=./a").click()
        cuentas.wait_for(state="visible")
    click_y_esperar(page, cuentas, "Abrir Cuentas")
    if not page.get_by_role("button", name="Buscar", exact=True).count():
        raise BotError("Abrir Cuentas: no aparecio la pantalla de filtros")


def buscar_usuario(page: Page, usuario: str) -> None:
    # Las pestañas de filtros son jQuery UI (li[role=tab]). Se espera el estado del HTML:
    # pestaña seleccionada, sin clase de carga, y con el campo "Usuario" en su panel.
    page.get_by_role("tab", name="Usuario", exact=True).click()
    page.wait_for_function(
        """() => {
            const tab = [...document.querySelectorAll('li[role=tab]')].find(t => t.textContent.trim() === 'Usuario');
            if (!tab || tab.getAttribute('aria-selected') !== 'true' || tab.classList.contains('ui-tabs-loading')) return false;
            const panel = document.getElementById(tab.getAttribute('aria-controls'));
            return !!panel && [...panel.querySelectorAll('label')].some(l => l.textContent.trim() === 'Usuario');
        }"""
    )
    panel = page.locator(".ui-tabs-panel[aria-hidden='false']").first
    campo = panel.locator("xpath=.//div[contains(@class,'form-group')][label[normalize-space()='Usuario']]//input")
    campo.first.fill(usuario)

    # Tope corto: si la busqueda queda en "loading" no se espera de mas, se registra y se sigue
    click_y_esperar(page, page.get_by_role("button", name="Buscar", exact=True), "Buscar",
                    timeout=config.TIMEOUT_BUSQUEDA_MS, error_cls=TimeoutBusqueda)


# El lapiz naranja de la columna "M" esta envuelto en <span title="Modificar el registro">
LAPIZ_MODIFICAR = "span[title='Modificar el registro']"


def abrir_edicion(page: Page) -> dict:
    # Se llama despues de click_y_esperar(Buscar): la grilla ya es la nueva, se lee directo sin esperar
    datos = page.locator(".caption", has_text=re.compile(r"^\s*Datos:\s*\d+")).first
    if datos.count():
        n = int(re.search(r"\d+", datos.inner_text()).group())
    else:
        n = page.locator(LAPIZ_MODIFICAR).count()

    if n == 0:
        raise NoEncontrado("La busqueda devolvio 'Datos: 0'")
    if n > 1:
        raise VariasCuentas(f"La busqueda devolvio {n} cuentas, se esperaba 1")

    lapices = page.locator(LAPIZ_MODIFICAR)
    if lapices.count() != 1:
        raise BotError(f"Se esperaba 1 lapiz de 'Modificar' en la grilla y hay {lapices.count()}")

    # Columnas de la grilla: [check, Numero, Nombre, Tipo, Estado, Homolog, Padre, Usuarios, Historial, V, M, R]
    celdas = lapices.first.locator("xpath=ancestor::tr[1]/td")
    cuenta = {
        "numero": celdas.nth(1).inner_text().strip(),
        "nombre": celdas.nth(2).inner_text().strip(),
        "estado": celdas.nth(4).inner_text().strip(),
    }
    log(f"  Cuenta: {cuenta['numero']} - {cuenta['nombre']} ({cuenta['estado']})")

    click_y_esperar(page, lapices.first.locator("a"), "Abrir modificacion")
    if not page.get_by_text(re.compile(r"Modificaci.n de Cuenta Comercial")).count():
        raise BotError("Abrir modificacion: no aparecio el formulario 'Modificación de Cuenta Comercial'")
    return cuenta


def setear_monto_credito(page: Page, monto: str, guardar: bool) -> tuple[str, str, bool]:
    """Devuelve (monto_anterior, monto_nuevo, guardado)."""
    campo = page.locator(
        "xpath=//div[contains(@class,'form-group')][label[normalize-space()='Monto credito']]//input"
    ).first
    campo.wait_for(state="visible")

    anterior = campo.input_value()
    if a_numero(anterior) == a_numero(monto):
        log(f"  Monto credito ya es {anterior!r}, no se modifica")
        return anterior, anterior, False

    # El campo usa autoNumeric (escucha keydown/keypress/focusout): hay que tipear tecla por tecla
    # y salir con Tab para que formatee. NO usar Enter: dispara el submit oculto "Default" del form.
    campo.click()
    campo.press("Control+a")
    campo.press_sequentially(monto)
    campo.press("Tab")
    nuevo = campo.input_value()
    if a_numero(nuevo) != a_numero(monto):
        raise BotError(f"El campo quedo en {nuevo!r} en vez de {monto}")
    log(f"  Monto credito: {anterior!r} -> {nuevo!r}")

    if not guardar:
        log("  GUARDAR=false -> NO se guarda (modo prueba)")
        return anterior, nuevo, False

    click_y_esperar(page, page.get_by_role("button", name="Aceptar", exact=True), "Guardar")
    msgs = mensajes_visibles(page)
    # Si despues de Aceptar sigue el formulario, la plataforma rechazo el cambio (validacion)
    if page.get_by_text(re.compile(r"Modificaci.n de Cuenta Comercial")).count():
        raise BotError(f"Guardar: el formulario sigue abierto, posible error de validacion ({msgs or 'sin mensaje'})")
    log(f"  Guardado. Mensajes: {msgs or 'ninguno'}")
    return anterior, nuevo, True


def procesar_usuario(page: Page, usuario: str, guardar: bool, lote: str, intento: int) -> str:
    """Procesa un usuario y lo registra. Devuelve el estado. SesionVencida se propaga sin registrar."""
    log(f"Usuario '{usuario}'" + (f" (intento {intento})" if intento > 1 else ""))
    cuenta, anterior, nuevo, guardado = None, None, None, False
    try:
        ir_a_cuentas(page)
        buscar_usuario(page, usuario)
        cuenta = abrir_edicion(page)
        anterior, nuevo, guardado = setear_monto_credito(page, config.MONTO_CREDITO, guardar)
        estado, detalle = ("OK" if guardado else ("SIN_CAMBIOS" if anterior == nuevo else "PRUEBA")), ""
    except SesionVencida:
        raise
    except (BotError, PWTimeout) as e:
        estado = getattr(e, "estado", "ERROR")
        detalle = str(e).splitlines()[0][:500]
        if not isinstance(e, NoEncontrado):
            guardar_debug(page, f"{estado}_{usuario}")
    log(f"  -> {estado}" + (f": {detalle}" if detalle else ""))
    registrar(lote, intento, usuario, cuenta, anterior, nuevo, guardado, estado, detalle)
    return estado


def procesar_lista(sesion: Sesion, usuarios: list[str], guardar: bool, lote: str, intento: int) -> dict[str, str]:
    """
    Procesa la lista sin cortarse: cada usuario termina con un estado registrado.
    - Si la sesion vence, se re-loguea y se reintenta ESE usuario (una vez).
    - Si un usuario deja la pagina en mal estado, se re-loguea antes del siguiente.
    - Si no se puede volver a loguear, los restantes quedan como NO_PROCESADO.
    """
    resultados: dict[str, str] = {}
    for i, usuario in enumerate(usuarios):
        try:
            if sesion.sucia:
                log("Pagina anterior en mal estado: nuevo login para seguir")
                sesion.iniciar()
            try:
                estado = procesar_usuario(sesion.page, usuario, guardar, lote, intento)
            except SesionVencida as e:
                log(f"  {e}; nuevo login y se reintenta '{usuario}'")
                sesion.iniciar()
                try:
                    estado = procesar_usuario(sesion.page, usuario, guardar, lote, intento)
                except SesionVencida as e2:
                    estado = "ERROR"
                    registrar(lote, intento, usuario, None, None, None, False, estado,
                              f"La sesion vencio dos veces seguidas: {e2}"[:500])
            resultados[usuario] = estado
            if estado not in ESTADOS_SIN_RELOGIN:
                sesion.sucia = True
        except (BotError, PWTimeout) as e:  # fallo un login: no se puede seguir
            quedan = [u for u in usuarios[i:] if u not in resultados]
            log(f"ERROR: no se pudo volver a loguear ({e}); quedan {len(quedan)} sin procesar")
            for u in quedan:
                registrar(lote, intento, u, None, None, None, False, "NO_PROCESADO",
                          f"Relogin fallido: {e}"[:500])
                resultados[u] = "NO_PROCESADO"
            break
    return resultados


# --------------------------------------------------------------------------- resultados

_COLUMNAS = {
    "fecha": "TEXT", "entorno": "TEXT", "usuario_busqueda": "TEXT",
    "cuenta_numero": "TEXT", "cuenta_nombre": "TEXT",
    "monto_anterior": "TEXT", "monto_nuevo": "TEXT", "guardado": "INTEGER",
    "estado": "TEXT", "detalle": "TEXT", "lote": "TEXT", "intento": "INTEGER",
}


def _conectar() -> sqlite3.Connection:
    db = sqlite3.connect(config.DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS ejecuciones (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    # bases creadas con versiones anteriores pueden no tener todas las columnas
    existentes = {fila[1] for fila in db.execute("PRAGMA table_info(ejecuciones)")}
    for nombre, tipo in _COLUMNAS.items():
        if nombre not in existentes:
            db.execute(f"ALTER TABLE ejecuciones ADD COLUMN {nombre} {tipo}")
    return db


# True cuando los usuarios vienen de bot.usuarios_traxion: el resultado se guarda tambien ahi
_USA_DB = False


def registrar(lote: str, intento: int, usuario: str, cuenta: dict | None, anterior, nuevo,
              guardado: bool, estado: str, detalle: str) -> None:
    """
    Deja registro de cada usuario procesado:
      - SQLite local (resultados.db): historial completo de cada intento.
      - PostgreSQL (bot.usuarios_traxion), si los usuarios vinieron de la base: estado actual.
    """
    if _USA_DB:
        try:
            fuente_db.actualizar(usuario, estado, detalle, (cuenta or {}).get("numero"), anterior)
        except psycopg.Error as e:
            # No se corta el lote: queda en SQLite y, como en la base sigue pendiente,
            # la proxima corrida lo vuelve a procesar (el bot es idempotente: SIN_CAMBIOS).
            log(f"  AVISO: no se pudo actualizar '{usuario}' en la base: {e}")
    with _conectar() as db:
        db.execute(
            "INSERT INTO ejecuciones (fecha, entorno, usuario_busqueda, cuenta_numero, cuenta_nombre,"
            " monto_anterior, monto_nuevo, guardado, estado, detalle, lote, intento)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                config.TRAXION_URL,
                usuario,
                (cuenta or {}).get("numero"),
                (cuenta or {}).get("nombre"),
                anterior,
                nuevo,
                int(guardado),
                estado,
                detalle,
                lote,
                intento,
            ),
        )


def resumen(lote: str) -> None:
    """Estado FINAL de cada usuario del lote (si se reintento, cuenta el ultimo intento)."""
    with _conectar() as db:
        filas = db.execute(
            "SELECT usuario_busqueda, estado FROM ejecuciones WHERE id IN"
            " (SELECT MAX(id) FROM ejecuciones WHERE lote = ? GROUP BY usuario_busqueda) ORDER BY id",
            (lote,),
        ).fetchall()
    conteo = Counter(estado for _, estado in filas)
    log(f"Resumen lote {lote}: " + ", ".join(f"{k}={v}" for k, v in conteo.most_common()))
    for usuario, estado in filas:
        if estado not in ("OK", "SIN_CAMBIOS", "PRUEBA"):
            log(f"  {estado:<16} {usuario}")


def cargar_usuarios(args) -> list[str]:
    """Prioridad: --usuario / --archivo  >  USUARIOS del .env  >  USUARIO_BUSQUEDA del .env."""
    usuarios = list(args.usuario or [])
    if args.archivo:
        for linea in Path(args.archivo).read_text(encoding="utf-8").splitlines():
            linea = linea.split("#", 1)[0].strip()
            if linea:
                usuarios.append(linea)
    if not usuarios:
        usuarios = config.USUARIOS or [config.USUARIO_BUSQUEDA]
    return list(dict.fromkeys(u.strip() for u in usuarios if u.strip()))  # sin duplicados, mismo orden


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Bot Traxion - Monto credito")
    ap.add_argument("--usuario", action="append", help="Usuario a buscar (se puede repetir)")
    ap.add_argument("--archivo", help="Archivo de texto con un usuario por linea")
    ap.add_argument("--solo-login", action="store_true", help="Solo prueba el login y sale")
    ap.add_argument("--guardar", action="store_true", help="Aprieta Aceptar (si no, modo prueba)")
    ap.add_argument("--headless", action="store_true", help="Sin ventana de navegador")
    ap.add_argument("--pausa", action="store_true", help="Abre el Playwright Inspector al final")
    ap.add_argument("--limite", type=int, help="Procesar como maximo N usuarios (util para probar)")
    args = ap.parse_args()

    if not config.TRAXION_USER or not config.TRAXION_PASS:
        log(f"ERROR: Faltan TRAXION_USER / TRAXION_PASS en {config.BASE_DIR / '.env'}")
        return 1
    guardar = args.guardar or config.GUARDAR
    headless = args.headless or config.HEADLESS
    if args.solo_login:
        try:
            correr_lote([], guardar, headless, solo_login=True, pausa=args.pausa)
            return 0
        except (BotError, PWTimeout):
            return 1

    # De donde salen los usuarios: --usuario/--archivo (pruebas, no toca la base) > base PostgreSQL > .env
    global _USA_DB
    if args.usuario or args.archivo or not config.DB_NAME:
        usuarios = cargar_usuarios(args)[:args.limite]
    else:
        _USA_DB = True
        try:
            sincronizadas = fuente_db.sincronizar()
            usuarios = fuente_db.pendientes(args.limite)
        except psycopg.Error as e:
            log(f"ERROR con la base '{config.DB_NAME}' en {config.DB_HOST}:{config.DB_PORT}: {e}")
            fuente_db.BaseDatos().cerrar()
            return 1
        log(f"Base '{config.DB_NAME}': {sincronizadas} cuenta(s) nueva(s) importada(s) de linked_accounts, "
            f"{len(usuarios)} pendiente(s) para procesar")

    try:
        if not usuarios:
            log("No hay usuarios para procesar")
            return 0
        correr_lote(usuarios, guardar, headless, pausa=args.pausa)
        return 0
    except (BotError, PWTimeout):
        return 1  # fallo el login: ya quedo logueado y con captura en debug/
    finally:
        fuente_db.BaseDatos().cerrar()


def vaciar_cola(guardar: bool, headless: bool) -> Counter:
    """
    Procesa la base hasta que no quedan pendientes (lo usa el servidor al "despertar").
    En cada vuelta re-sincroniza, asi tambien toma las cuentas que se vincularon mientras trabajaba.
    Cada cuenta se procesa como maximo una vez por llamada: las que quedan pendientes por una falla
    pasajera esperan al proximo despertar (asi no se queda en loop contra una plataforma caida).
    Devuelve cuantas cuentas terminaron en cada resultado.
    """
    global _USA_DB
    _USA_DB = True
    vistas: set[str] = set()
    conteo: Counter = Counter()
    try:
        while True:
            nuevas = fuente_db.sincronizar()
            cola = [u for u in fuente_db.pendientes() if u not in vistas]
            log(f"Base '{config.DB_NAME}': {nuevas} cuenta(s) nueva(s) importada(s), {len(cola)} para procesar")
            if not cola:
                return conteo
            vistas.update(cola)
            conteo.update(correr_lote(cola, guardar, headless).values())
    finally:
        fuente_db.BaseDatos().cerrar()


def correr_lote(usuarios: list[str], guardar: bool, headless: bool,
                solo_login: bool = False, pausa: bool = False) -> dict[str, str]:
    """
    Abre el navegador, se loguea y procesa la lista. Devuelve el resultado final de cada usuario.
    Si no puede loguearse lanza BotError/PWTimeout (con captura en debug/).
    """
    lote = datetime.now().strftime("%Y%m%d_%H%M%S")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=0 if headless else 150)
        context = browser.new_context(viewport={"width": 1600, "height": 1000}, locale="es-AR")
        context.set_default_timeout(config.TIMEOUT_MS)
        sesion = Sesion(context)
        try:
            try:
                sesion.iniciar()
            except (BotError, PWTimeout) as e:
                log(f"ERROR en login: {e}")
                if sesion.page is not None:
                    guardar_debug(sesion.page, "login_error")
                raise
            if solo_login:
                guardar_debug(sesion.page, "login_ok")
                return {}

            log(f"Lote {lote}: {len(usuarios)} usuario(s), guardar={guardar}, "
                f"tope busqueda={config.TIMEOUT_BUSQUEDA_MS / 1000:g} s")
            resultados = procesar_lista(sesion, usuarios, guardar, lote, intento=1)

            timeouts = [u for u, e in resultados.items() if e == "TIMEOUT_BUSQUEDA"]
            if timeouts and config.REINTENTAR_TIMEOUTS and "NO_PROCESADO" not in resultados.values():
                log(f"Reintentando {len(timeouts)} usuario(s) que dieron TIMEOUT_BUSQUEDA")
                resultados.update(procesar_lista(sesion, timeouts, guardar, lote, intento=2))
        finally:
            if pausa and sesion.page is not None:
                sesion.page.pause()
            context.close()
            browser.close()

    resumen(lote)
    return resultados


if __name__ == "__main__":
    sys.exit(main())
