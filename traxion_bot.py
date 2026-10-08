"""
Bot Traxion (Pago24) - TRASA

Flujo (por cada usuario de la lista, con un solo login):
  1. Login con las credenciales del .env
  2. GESTORES -> Cuentas -> pestaña "Usuario"
  3. Busca el usuario (si no responde en TIMEOUT_BUSQUEDA_MS, se registra y se sigue)
  4. Aprieta el lapiz naranja de la columna "M"
  5. Setea "Monto credito" = 1 (y guarda solo si GUARDAR=true / --guardar)

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

from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout, sync_playwright

import config


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


# Despues de estos errores la pagina puede quedar trabada (overlay de carga, respuesta tardia
# que llega mas tarde y pisa el body): se descarta la pagina y se vuelve a loguear.
REQUIERE_RELOGIN = (SinRespuesta, PWTimeout)


# --------------------------------------------------------------------------- utils

def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


_MARCA_ID = "__bot_marca"


def click_y_esperar(page: Page, elemento, accion: str, timeout: int | None = None,
                    error_cls: type[SinRespuesta] = SinRespuesta) -> None:
    """
    Clickea y espera a que la plataforma reemplace la pagina, por evento y no por tiempo:
      1. Antes del click se inserta un <div> marcador dentro del <body>.
      2. El ajaxifier, al recibir la respuesta, hace $("body").html(...) -> el marcador desaparece.
         (Si en vez de AJAX hubiera una navegacion completa, el marcador tambien desaparece.)
      3. Apenas el marcador no existe mas, la pagina nueva ya esta en el DOM y se sigue.
    `timeout` es el tope: si se cumple sin respuesta se lanza `error_cls`.
    Por defecto es TIMEOUT_MS (tope de seguridad); la busqueda usa uno corto.
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
    # el overlay "Un momento por favor..." (blockUI) se va con el body viejo; por las dudas
    page.locator(".blockUI").first.wait_for(state="detached")


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


# --------------------------------------------------------------------------- pasos

def nueva_pagina(context: BrowserContext) -> Page:
    page = context.new_page()
    # confirm()/alert() de JS: Playwright los cancela por defecto; los aceptamos y los logueamos
    page.on("dialog", lambda d: (log(f"Dialogo JS: {d.message!r} -> aceptar"), d.accept()))
    return page


def login(page: Page) -> None:
    log(f"Abriendo {config.TRAXION_URL}")
    page.goto(config.TRAXION_URL, wait_until="domcontentloaded")
    page.locator("#usuario").wait_for(state="visible")

    page.locator("#usuario").fill(config.TRAXION_USER)
    page.locator("#password").fill(config.TRAXION_PASS)
    click_y_esperar(page, page.get_by_role("button", name="Ingresar"), "Login")

    # La pagina ya cambio: si no esta el menu GESTORES, el login fallo
    if not page.get_by_text("GESTORES", exact=True).count():
        if page.locator("iframe[src*='recaptcha']").count():
            raise BotError("La plataforma pidio captcha. Hay que resolverlo a mano; el bot no lo saltea.")
        detalle = " | ".join(mensajes_visibles(page)) or "sin mensaje visible"
        raise BotError(f"Login fallido ({detalle})")

    log(f"Login OK como '{config.TRAXION_USER}'")


def ir_a_cuentas(page: Page) -> None:
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


def procesar_usuario(page: Page, usuario: str, guardar: bool, lote: str) -> BotError | PWTimeout | None:
    """Procesa un usuario y SIEMPRE lo registra. Devuelve el error (si hubo) para decidir si re-loguear."""
    log(f"Usuario '{usuario}'")
    cuenta, anterior, nuevo, guardado = None, None, None, False
    try:
        ir_a_cuentas(page)
        buscar_usuario(page, usuario)
        cuenta = abrir_edicion(page)
        anterior, nuevo, guardado = setear_monto_credito(page, config.MONTO_CREDITO, guardar)
        estado = "OK" if guardado else ("SIN_CAMBIOS" if anterior == nuevo else "PRUEBA")
        registrar(lote, usuario, cuenta, anterior, nuevo, guardado, estado, "")
        log(f"  -> {estado}")
        return None
    except (BotError, PWTimeout) as e:
        estado = getattr(e, "estado", "ERROR")
        detalle = str(e).splitlines()[0][:500]
        log(f"  -> {estado}: {detalle}")
        if not isinstance(e, NoEncontrado):
            guardar_debug(page, f"{estado}_{usuario}")
        registrar(lote, usuario, cuenta, anterior, nuevo, guardado, estado, detalle)
        return e


# --------------------------------------------------------------------------- resultados

def registrar(lote: str, usuario: str, cuenta: dict | None, anterior, nuevo, guardado: bool,
              estado: str, detalle: str) -> None:
    """Deja registro de cada usuario procesado en SQLite (base para exponerlo despues por un endpoint)."""
    with sqlite3.connect(config.DB_PATH) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS ejecuciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT, entorno TEXT, usuario_busqueda TEXT,
                cuenta_numero TEXT, cuenta_nombre TEXT,
                monto_anterior TEXT, monto_nuevo TEXT, guardado INTEGER,
                estado TEXT, detalle TEXT, lote TEXT)"""
        )
        # bases creadas con la version anterior no tienen la columna "lote"
        if "lote" not in {fila[1] for fila in db.execute("PRAGMA table_info(ejecuciones)")}:
            db.execute("ALTER TABLE ejecuciones ADD COLUMN lote TEXT")
        db.execute(
            "INSERT INTO ejecuciones (fecha, entorno, usuario_busqueda, cuenta_numero, cuenta_nombre,"
            " monto_anterior, monto_nuevo, guardado, estado, detalle, lote) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
            ),
        )


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
    args = ap.parse_args()

    guardar = args.guardar or config.GUARDAR
    headless = args.headless or config.HEADLESS

    if not config.TRAXION_USER or not config.TRAXION_PASS:
        log(f"ERROR: Faltan TRAXION_USER / TRAXION_PASS en {config.BASE_DIR / '.env'}")
        return 1

    usuarios = [] if args.solo_login else cargar_usuarios(args)
    lote = datetime.now().strftime("%Y%m%d_%H%M%S")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=0 if headless else 150)
        context = browser.new_context(viewport={"width": 1600, "height": 1000}, locale="es-AR")
        context.set_default_timeout(config.TIMEOUT_MS)
        page = nueva_pagina(context)
        try:
            try:
                login(page)
            except (BotError, PWTimeout) as e:
                log(f"ERROR en login: {e}")
                guardar_debug(page, "login_error")
                return 1
            if args.solo_login:
                guardar_debug(page, "login_ok")
                return 0

            log(f"Lote {lote}: {len(usuarios)} usuario(s), guardar={guardar}, "
                f"tope busqueda={config.TIMEOUT_BUSQUEDA_MS / 1000:g} s")
            for i, usuario in enumerate(usuarios):
                error = procesar_usuario(page, usuario, guardar, lote)

                quedan = usuarios[i + 1:]
                if isinstance(error, REQUIERE_RELOGIN) and quedan:
                    # La pagina puede seguir en "loading" o recibir tarde la respuesta vieja:
                    # se tira y se arranca limpia con un login nuevo.
                    log("  Pagina descartada por falta de respuesta; nuevo login para seguir")
                    page.close()
                    page = nueva_pagina(context)
                    try:
                        login(page)
                    except (BotError, PWTimeout) as e:
                        log(f"ERROR: no se pudo volver a loguear ({e}); se cortan los {len(quedan)} restantes")
                        for u in quedan:
                            registrar(lote, u, None, None, None, False, "NO_PROCESADO", f"Relogin fallido: {e}"[:500])
                        break
        finally:
            if args.pausa:
                page.pause()
            context.close()
            browser.close()

    resumen(lote)
    return 0


def resumen(lote: str) -> None:
    with sqlite3.connect(config.DB_PATH) as db:
        filas = db.execute(
            "SELECT usuario_busqueda, estado FROM ejecuciones WHERE lote = ? ORDER BY id", (lote,)
        ).fetchall()
    conteo = Counter(estado for _, estado in filas)
    log("Resumen: " + ", ".join(f"{k}={v}" for k, v in conteo.most_common()))
    for usuario, estado in filas:
        if estado not in ("OK", "SIN_CAMBIOS", "PRUEBA"):
            log(f"  {estado:<16} {usuario}")


if __name__ == "__main__":
    sys.exit(main())
