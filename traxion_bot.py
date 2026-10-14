"""
Bot Traxion (Pago24) - TRASA

Flujo (por cada usuario de la lista, con un solo login):
  1. Login con las credenciales del .env
  2. GESTORES -> Cuentas -> pestaña "Usuario"
  3. Busca el usuario (si no responde en TIMEOUT_BUSQUEDA_MS, se registra y se sigue)
  4. Valida que la cuenta sea la del usuario: filtro exacto, "ID Usuario" en la pantalla de
     usuarios de la cuenta, y numero de cuenta en el formulario
  5. Aprieta el lapiz naranja de la columna "M"
  6. Setea "Monto credito" = 1 (y guarda solo si GUARDAR=true / --guardar)
  7. Al terminar el lote cierra la sesion en Traxion
Al final reintenta una vez los TIMEOUT_BUSQUEDA (si REINTENTAR_TIMEOUTS=true).

Cada cuenta termina con uno de estos resultados (en bot.usuarios_traxion y en el log del dia):
  OK               se guardo el monto
  SIN_CAMBIOS      ya tenia el monto correcto
  PRUEBA           se cargo el monto pero no se guardo (GUARDAR=false)
  NO_ENCONTRADO    la busqueda devolvio "Datos: 0"
  NO_COINCIDE      la cuenta que mostro Traxion no es la del usuario buscado (no se toca nada)
  TIMEOUT_BUSQUEDA la busqueda no respondio dentro de TIMEOUT_BUSQUEDA_MS
  VARIAS_CUENTAS   la busqueda devolvio mas de una cuenta (no se toca nada)
  ERROR            cualquier otro problema (ver status_message y carpeta debug/)
  NO_PROCESADO     el bot no llego a procesarlo (ej. no pudo volver a loguearse)
  OTRO_PROCESO     la cuenta la tenia reservada otra corrida: no se toca ni se registra

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
import os
import re
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


# Valor que el bot pone en "Monto credito". Es FIJO a proposito (no sale del .env): aunque Traxion lo
# llama "monto", en la practica es un ON/OFF (0 = apagado, 1 = encendido). Un valor configurable solo
# agregaria la posibilidad de aplicar un numero equivocado a todas las cuentas.
MONTO_CREDITO = "1"

# Despues de estos estados la pagina quedo en un lugar conocido (la grilla) y se puede seguir.
# Con cualquier otro error la pagina puede quedar trabada (overlay de carga, respuesta tardia que
# pisa el body, pantalla inesperada): se descarta y se vuelve a loguear antes del siguiente usuario.
# OTRO_PROCESO: la cuenta la reservo otra corrida y no se toco la pagina.
ESTADOS_SIN_RELOGIN = ("OK", "SIN_CAMBIOS", "PRUEBA", "NO_ENCONTRADO", "VARIAS_CUENTAS", "OTRO_PROCESO")


# --------------------------------------------------------------------------- utils

# Parametros de sesion de Seaside en las URLs de Traxion: con _s (sesion) y _k (continuacion) alguien
# podria entrar a la plataforma como el usuario operador del bot. Nunca se escriben en disco.
_PARAMS_SESION = re.compile(r"\b(_[sk]=)[^&\"'\s<>]+")


def tapar_sesion(texto: str) -> str:
    """Reemplaza los valores de _s y _k por REDACTADO (logs, mensajes de error, HTML de debug)."""
    return _PARAMS_SESION.sub(r"\1REDACTADO", texto)


def log(msg: str) -> None:
    linea = f"[{datetime.now():%H:%M:%S}] {tapar_sesion(msg)}"
    print(linea, flush=True)
    try:
        config.LOG_DIR.mkdir(exist_ok=True)
        with open(config.LOG_DIR / f"bot_{datetime.now():%Y%m%d}.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d} {linea}\n")
    except OSError:
        pass  # que un problema con el archivo de log nunca frene el bot


def avisar_config_obsoleta() -> None:
    """Variables que ya no se usan: se avisa para que nadie crea que las esta configurando."""
    if "MONTO_CREDITO" in os.environ:
        log(f"AVISO: MONTO_CREDITO del .env se ignora; el bot siempre pone {MONTO_CREDITO} "
            "(es fijo en el codigo). Se puede borrar esa linea del .env.")


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
    """
    Guarda una captura de la pagina cuando algo falla. La captura no incluye la barra de direcciones,
    asi que no lleva la sesion. El HTML solo se guarda con DEBUG_HTML=true (para desarrollar) y,
    aun asi, con _s/_k tapados. Las capturas viejas se borran solas (ver limpiar_debug).
    """
    config.DEBUG_DIR.mkdir(exist_ok=True)
    etiqueta = re.sub(r"[^\w.-]", "_", etiqueta)[:80]
    base = config.DEBUG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{etiqueta}"
    try:
        page.screenshot(path=f"{base}.png", full_page=True)
        if config.DEBUG_HTML:
            base.with_suffix(".html").write_text(tapar_sesion(page.content()), encoding="utf-8")
        log(f"Debug guardado en {base}.png" + (" / .html" if config.DEBUG_HTML else ""))
    except Exception as e:  # noqa: BLE001 - el debug nunca debe tapar el error original
        log(f"No se pudo guardar debug: {e}")


def limpiar_debug() -> None:
    """Borra de debug/ lo que tenga mas de DEBUG_RETENCION_DIAS (las capturas pueden tener datos personales)."""
    if not config.DEBUG_DIR.exists():
        return
    limite = datetime.now().timestamp() - config.DEBUG_RETENCION_DIAS * 86400
    borrados = 0
    for archivo in config.DEBUG_DIR.iterdir():
        try:
            if archivo.is_file() and archivo.stat().st_mtime < limite:
                archivo.unlink()
                borrados += 1
        except OSError:
            pass
    if borrados:
        log(f"Debug: {borrados} archivo(s) de mas de {config.DEBUG_RETENCION_DIAS} dias borrado(s)")


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
        self.terminar(timeout=5000)  # la pagina anterior puede estar trabada: tope corto para el logout
        self.page = self.context.new_page()
        # confirm()/alert() de JS: Playwright los cancela por defecto; los aceptamos y los logueamos
        self.page.on("dialog", lambda d: (log(f"Dialogo JS: {d.message!r} -> aceptar"), d.accept()))
        login(self.page)

    def terminar(self, timeout: int = 15000) -> None:
        """Cierra la sesion en Traxion (si se puede) y la pagina. Nunca deja una sesion viva al descartarla."""
        if self.page is None:
            return
        cerrar_sesion_traxion(self.page, timeout)
        try:
            self.page.close()
        except Exception:  # noqa: BLE001
            pass
        self.page = None


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


def cerrar_sesion_traxion(page: Page, timeout: int = 15000) -> None:
    """
    Cierra la sesion en Traxion: menu del usuario (arriba a la derecha) -> "Salir de plataforma" -> "Salir".
    Cerrar el navegador NO alcanza: la sesion seguiria viva del lado de Traxion hasta vencer, y con sus
    _s/_k alguien podria usarla. Es "best effort": si falla (pagina trabada), se avisa en el log y se sigue.
    """
    try:
        if page.is_closed() or es_pantalla_login(page):
            return
        salir_menu = page.get_by_text("Salir de plataforma", exact=True).first
        if not salir_menu.is_visible():
            usuario = re.compile(rf"^\s*{re.escape(config.TRAXION_USER)}\s*$")
            page.locator("a", has_text=usuario).first.click(timeout=timeout)
            salir_menu.wait_for(state="visible", timeout=timeout)
        click_y_esperar(page, salir_menu, "Cerrar sesion (menu)", timeout=timeout, chequear_sesion=False)
        confirmar = page.get_by_role("button", name="Salir", exact=True).or_(
            page.get_by_role("link", name="Salir", exact=True)).first
        confirmar.wait_for(state="visible", timeout=timeout)
        click_y_esperar(page, confirmar, "Cerrar sesion (confirmar)", timeout=timeout, chequear_sesion=False)
        if es_pantalla_login(page):
            log("Sesion de Traxion cerrada")
        else:
            log("AVISO: se confirmo la salida de Traxion pero no aparecio el login")
    except Exception as e:  # noqa: BLE001 - cerrar sesion nunca debe tapar el resultado del lote
        log(f"AVISO: no se pudo cerrar la sesion de Traxion ({str(e).splitlines()[0][:150]}); vence sola")


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


# La pestaña "Usuario" esta lista: seleccionada, sin clase de carga, y con el campo "Usuario" en su panel
_JS_PESTANA_USUARIO_LISTA = """() => {
    const tab = [...document.querySelectorAll('li[role=tab]')].find(t => t.textContent.trim() === 'Usuario');
    if (!tab || tab.getAttribute('aria-selected') !== 'true' || tab.classList.contains('ui-tabs-loading')) return false;
    const panel = document.getElementById(tab.getAttribute('aria-controls'));
    return !!panel && [...panel.querySelectorAll('label')].some(l => l.textContent.trim() === 'Usuario');
}"""


def abrir_pestana_usuario(page: Page, espera_ms: int | None = None) -> None:
    """
    Deja visible la pestaña "Usuario" de los filtros con su contenido cargado. Las pestañas son jQuery UI
    y cargan su contenido con un pedido aparte: despues de "Buscar" la pestaña aparece seleccionada pero
    VACIA hasta que llega ese contenido. Si en `espera_ms` no carga sola, se fuerza la recarga
    (General -> Usuario). Siempre se espera por estado del HTML, no por tiempo.
    """
    tab = page.get_by_role("tab", name="Usuario", exact=True)
    if espera_ms is None:
        tab.click()
        page.wait_for_function(_JS_PESTANA_USUARIO_LISTA)
        return
    try:
        page.wait_for_function(_JS_PESTANA_USUARIO_LISTA, timeout=espera_ms)  # ya estaba o carga sola
        return
    except PWTimeout:
        pass
    page.get_by_role("tab", name="General", exact=True).click()
    tab.click()
    page.wait_for_function(_JS_PESTANA_USUARIO_LISTA)


def buscar_usuario(page: Page, usuario: str) -> None:
    abrir_pestana_usuario(page)
    panel = page.locator(".ui-tabs-panel[aria-hidden='false']").first
    campo = panel.locator("xpath=.//div[contains(@class,'form-group')][label[normalize-space()='Usuario']]//input")
    campo.first.fill(usuario)

    # Tope corto: si la busqueda queda en "loading" no se espera de mas, se registra y se sigue
    click_y_esperar(page, page.get_by_role("button", name="Buscar", exact=True), "Buscar",
                    timeout=config.TIMEOUT_BUSQUEDA_MS, error_cls=TimeoutBusqueda)


# El lapiz naranja de la columna "M" esta envuelto en <span title="Modificar el registro">
LAPIZ_MODIFICAR = "span[title='Modificar el registro']"
# El icono de la columna "Usuarios" (muniequito) es un <input type=image src=".../commonUser.gif">
ICONO_USUARIOS = "a:has(input[src*='commonUser'])"
# Campo "Usuario" del filtro de Cuentas (en cualquier panel de pestañas, visible o no)
CAMPO_FILTRO_USUARIO = ("xpath=//div[contains(@class,'ui-tabs-panel')]"
                        "//div[contains(@class,'form-group')][label[normalize-space()='Usuario']]//input")

# Devuelve los valores de la columna cuyo encabezado es `titulo` (en la primera tabla visible que la tenga)
_JS_COLUMNA = r"""
titulo => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  for (const th of document.querySelectorAll('th')) {
    if (th.offsetParent === null || norm(th.textContent) !== titulo) continue;
    const encabezado = th.parentElement;
    let idx = 0;
    for (const c of encabezado.children) { if (c === th) break; idx += c.colSpan || 1; }
    const valores = [];
    for (const tr of th.closest('table').querySelectorAll('tr')) {
      if (tr === encabezado) continue;
      let p = 0;
      for (const td of tr.children) {
        if (td.tagName !== 'TD') break;
        if (p === idx) { const v = norm(td.textContent); if (v) valores.push(v); break; }
        p += td.colSpan || 1;
      }
    }
    return valores;
  }
  return null;
}
"""


class NoCoincide(BotError):
    """La cuenta que mostro Traxion no es la del usuario buscado: no se toca nada."""
    estado = "NO_COINCIDE"


def verificar_filtro(page: Page, usuario: str) -> None:
    """
    Validacion 1: la grilla nueva tiene que ser la de ESTA busqueda (filtro 'Usuario' exacto).
    No poder LEER el filtro es una falla del bot (ERROR, se reintenta); solo un valor leido y distinto
    es NO_COINCIDE (falla definitiva).
    """
    try:
        abrir_pestana_usuario(page, espera_ms=5000)
    except PWTimeout:
        raise BotError("Despues de buscar no cargo la pestaña 'Usuario' de los filtros") from None
    campo = page.locator(CAMPO_FILTRO_USUARIO).first
    valor = campo.input_value() if campo.count() else ""
    if not valor:
        raise BotError("No se pudo leer el filtro 'Usuario' despues de buscar")
    if valor != usuario:
        raise NoCoincide(f"La grilla no corresponde a la busqueda: el filtro 'Usuario' dice {valor!r}, "
                         f"se busco {usuario!r}")


def leer_resultado(page: Page) -> dict:
    """Lee la grilla de la busqueda: tiene que haber exactamente una cuenta. Devuelve sus datos."""
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
    return {
        "numero": celdas.nth(1).inner_text().strip(),
        "nombre": celdas.nth(2).inner_text().strip(),
        "estado": celdas.nth(4).inner_text().strip(),
    }


def verificar_usuarios_de_la_cuenta(page: Page, usuario: str, cuenta: dict) -> None:
    """
    Validacion 2: abre "Usuarios" de la cuenta (muniequito) y exige que el titulo sea de esa cuenta y que
    el usuario buscado este en la columna "ID Usuario" con el texto EXACTO. Despues vuelve a la grilla.
    Esa pantalla muestra datos personales (DNI, email, telefono): solo se leen el titulo y "ID Usuario".
    """
    fila = page.locator(LAPIZ_MODIFICAR).first.locator("xpath=ancestor::tr[1]")
    icono = fila.locator(ICONO_USUARIOS).first
    if not icono.count():
        raise BotError("No se encontro el icono de la columna 'Usuarios' de la cuenta")
    click_y_esperar(page, icono, "Abrir usuarios de la cuenta")

    titulo = page.get_by_text(re.compile(r"Usuarios de la cuenta:\s*\d+")).first
    if not titulo.count():
        raise BotError("Abrir usuarios: no aparecio 'Usuarios de la cuenta'")
    numero = re.search(r"Usuarios de la cuenta:\s*(\d+)", titulo.inner_text()).group(1)
    if numero != cuenta["numero"]:
        raise NoCoincide(f"La pantalla de usuarios es de la cuenta {numero}, la grilla mostro {cuenta['numero']}")
    ids = page.evaluate(_JS_COLUMNA, "ID Usuario")
    if ids is None:
        raise BotError("Usuarios de la cuenta: no se encontro la columna 'ID Usuario'")
    if usuario not in ids:
        raise NoCoincide(f"Se busco {usuario!r} pero la cuenta {numero} tiene los usuarios {ids}")
    log(f"  Usuario verificado en la cuenta {numero} ({len(ids)} usuario(s))")

    volver = page.get_by_role("button", name="Volver", exact=True).or_(
        page.get_by_role("link", name="Volver", exact=True)).first
    click_y_esperar(page, volver, "Volver de usuarios")


def volver_a_la_grilla(page: Page, usuario: str, cuenta: dict) -> None:
    """Si 'Volver' no dejo la grilla con la misma cuenta, se repite la busqueda (con sus validaciones)."""
    try:
        if leer_resultado(page)["numero"] == cuenta["numero"]:
            return
    except BotError:
        pass
    log("  'Volver' no dejo la grilla con el resultado: se repite la busqueda")
    if not page.get_by_role("button", name="Buscar", exact=True).count():
        ir_a_cuentas(page)
    buscar_usuario(page, usuario)
    verificar_filtro(page, usuario)
    otra = leer_resultado(page)
    if otra["numero"] != cuenta["numero"]:
        raise NoCoincide(f"Al repetir la busqueda aparecio la cuenta {otra['numero']}, antes {cuenta['numero']}")


def abrir_edicion(page: Page, cuenta: dict) -> None:
    """Abre el lapiz y, validacion 3, exige que el formulario sea de esa misma cuenta."""
    click_y_esperar(page, page.locator(LAPIZ_MODIFICAR).first.locator("a"), "Abrir modificacion")
    if not page.get_by_text(re.compile(r"Modificaci.n de Cuenta Comercial")).count():
        raise BotError("Abrir modificacion: no aparecio el formulario 'Modificación de Cuenta Comercial'")
    campo = page.locator(
        "xpath=//div[contains(@class,'form-group')][label[normalize-space()='Numero de Cuenta']]//input").first
    numero = campo.input_value().strip() if campo.count() else ""
    if not numero:
        raise BotError("No se pudo leer el 'Numero de Cuenta' del formulario")
    if numero != cuenta["numero"]:
        raise NoCoincide(f"El formulario es de la cuenta {numero!r}, se esperaba {cuenta['numero']}")


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


def procesar_usuario(page: Page, trabajo: fuente_db.Cuenta, guardar: bool, lote: str, intento: int) -> str:
    """
    Procesa una cuenta y la registra. Devuelve el estado. SesionVencida se propaga sin registrar.
    Si viene de la base, primero la reserva: si la tiene otra corrida, no la toca (OTRO_PROCESO).
    """
    usuario = trabajo.usuario
    log(f"Usuario '{usuario}'" + (f" [{trabajo.linked_account_id}]" if trabajo.linked_account_id else "")
        + (f" (intento {intento})" if intento > 1 else ""))
    if _USA_DB and trabajo.linked_account_id is not None:
        try:
            reservada = fuente_db.reclamar(trabajo)
        except psycopg.Error as e:
            # sin reserva no se procesa: podria estar tomandola otro proceso
            log(f"  -> NO_PROCESADO: no se pudo reservar en la base ({e})")
            return "NO_PROCESADO"
        if not reservada:
            log("  -> OTRO_PROCESO: la reservo otra corrida o ya no esta pendiente; no se toca")
            return "OTRO_PROCESO"
    cuenta, anterior = None, None
    try:
        ir_a_cuentas(page)
        buscar_usuario(page, usuario)
        # Antes de tocar nada, tres validaciones de que la cuenta es la del usuario buscado
        verificar_filtro(page, usuario)                           # 1. la grilla es de esta busqueda
        cuenta = leer_resultado(page)                             #    y trae exactamente una cuenta
        log(f"  Cuenta: {cuenta['numero']} - {cuenta['nombre']} ({cuenta['estado']})")
        verificar_usuarios_de_la_cuenta(page, usuario, cuenta)    # 2. el usuario esta en esa cuenta
        volver_a_la_grilla(page, usuario, cuenta)
        abrir_edicion(page, cuenta)                               # 3. el formulario es de esa cuenta
        anterior, nuevo, guardado = setear_monto_credito(page, MONTO_CREDITO, guardar)
        estado, detalle = ("OK" if guardado else ("SIN_CAMBIOS" if anterior == nuevo else "PRUEBA")), ""
    except SesionVencida:
        raise
    except (BotError, PWTimeout) as e:
        estado = getattr(e, "estado", "ERROR")
        detalle = tapar_sesion(str(e).splitlines()[0][:500])
        if not isinstance(e, NoEncontrado):
            guardar_debug(page, f"{estado}_{usuario}")
    log(f"  -> {estado}" + (f": {detalle}" if detalle else ""))
    registrar(trabajo, cuenta, anterior, estado, detalle)
    return estado


def procesar_lista(sesion: Sesion, trabajos: list[fuente_db.Cuenta], guardar: bool, lote: str,
                   intento: int) -> dict[fuente_db.Cuenta, str]:
    """
    Procesa la lista sin cortarse: cada cuenta termina con un estado registrado.
    - Si la sesion vence, se re-loguea y se reintenta ESA cuenta (una vez).
    - Si una cuenta deja la pagina en mal estado, se re-loguea antes de la siguiente.
    - Si no se puede volver a loguear, las restantes quedan como NO_PROCESADO.
    Los resultados van por cuenta (no por usuario): dos vinculos con el mismo usuario de Traxion
    son dos trabajos distintos, cada uno con su resultado.
    """
    resultados: dict[fuente_db.Cuenta, str] = {}
    for i, trabajo in enumerate(trabajos):
        try:
            if sesion.sucia:
                log("Pagina anterior en mal estado: nuevo login para seguir")
                sesion.iniciar()
            try:
                estado = procesar_usuario(sesion.page, trabajo, guardar, lote, intento)
            except SesionVencida as e:
                log(f"  {e}; nuevo login y se reintenta '{trabajo.usuario}'")
                sesion.iniciar()
                try:
                    estado = procesar_usuario(sesion.page, trabajo, guardar, lote, intento)
                except SesionVencida as e2:
                    estado = "ERROR"
                    registrar(trabajo, None, None, estado, f"La sesion vencio dos veces seguidas: {e2}"[:500])
            resultados[trabajo] = estado
            if estado not in ESTADOS_SIN_RELOGIN:
                sesion.sucia = True
        except (BotError, PWTimeout) as e:  # fallo un login: no se puede seguir
            quedan = [t for t in trabajos[i:] if t not in resultados]
            log(f"ERROR: no se pudo volver a loguear ({e}); quedan {len(quedan)} sin procesar")
            for t in quedan:
                registrar(t, None, None, "NO_PROCESADO", f"Relogin fallido: {e}"[:500])
                resultados[t] = "NO_PROCESADO"
            break
    return resultados


# --------------------------------------------------------------------------- resultados

# True cuando los usuarios vienen de bot.usuarios_traxion: el resultado se guarda tambien ahi
_USA_DB = False


def registrar(trabajo: fuente_db.Cuenta, cuenta: dict | None, anterior, estado: str, detalle: str) -> None:
    """
    Guarda el resultado de una cuenta en PostgreSQL (bot.usuarios_traxion, por linked_account_id), que es
    la unica fuente de verdad. El detalle de cada intento queda ademas en el log del dia (logs/).
    Las pruebas manuales (--usuario/--archivo) no tocan la base: quedan solo en el log.
    """
    if not (_USA_DB and trabajo.linked_account_id is not None):
        return
    try:
        if not fuente_db.actualizar(trabajo, estado, detalle, (cuenta or {}).get("numero"), anterior):
            log(f"  AVISO: '{trabajo.usuario}' ({trabajo.linked_account_id}) no se guardo en la base: "
                "la tiene reservada otra corrida")
    except psycopg.Error as e:
        # No se corta el lote: queda en el log y, como en la base sigue pendiente,
        # la proxima corrida lo vuelve a procesar (el bot es idempotente: SIN_CAMBIOS).
        log(f"  AVISO: no se pudo actualizar '{trabajo.usuario}' en la base: {e}")


def resumen(lote: str, resultados: dict[fuente_db.Cuenta, str]) -> None:
    """Estado FINAL de cada cuenta del lote (si se reintento, cuenta el ultimo intento)."""
    conteo = Counter(resultados.values())
    log(f"Resumen lote {lote}: " + (", ".join(f"{k}={v}" for k, v in conteo.most_common()) or "sin cuentas"))
    for trabajo, estado in resultados.items():
        if estado not in ("OK", "SIN_CAMBIOS", "PRUEBA"):
            log(f"  {estado:<16} {trabajo.usuario}" + (f" [{trabajo.linked_account_id}]"
                                                       if trabajo.linked_account_id else ""))


def cargar_usuarios(args) -> list[fuente_db.Cuenta]:
    """
    Usuarios para pruebas manuales (no vienen de la base, no tienen linked_account_id).
    Prioridad: --usuario / --archivo  >  USUARIOS del .env  >  USUARIO_BUSQUEDA del .env.
    """
    usuarios = list(args.usuario or [])
    if args.archivo:
        for linea in Path(args.archivo).read_text(encoding="utf-8").splitlines():
            linea = linea.split("#", 1)[0].strip()
            if linea:
                usuarios.append(linea)
    if not usuarios:
        usuarios = config.USUARIOS or [config.USUARIO_BUSQUEDA]
    unicos = dict.fromkeys(u.strip() for u in usuarios if u.strip())  # sin duplicados, mismo orden
    return [fuente_db.Cuenta(None, u) for u in unicos]


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

    avisar_config_obsoleta()
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

    global _USA_DB
    db = fuente_db.BaseDatos()
    try:
        # Con base configurada, cualquier corrida (incluso una prueba manual) toma el candado: asi no
        # opera Traxion a la vez que el servidor o un cron, con el mismo usuario operador.
        if config.DB_NAME and not db.tomar_candado():
            log("Otra corrida del bot esta en curso (candado de la base tomado): no hago nada")
            return 0

        # De donde salen los usuarios: --usuario/--archivo (pruebas, no toca la base) > base PostgreSQL > .env
        if args.usuario or args.archivo or not config.DB_NAME:
            trabajos = cargar_usuarios(args)[:args.limite]
        else:
            _USA_DB = True
            sincronizadas = fuente_db.sincronizar()
            trabajos = fuente_db.pendientes(args.limite)
            log(f"Base '{config.DB_NAME}': {sincronizadas} cuenta(s) nueva(s) importada(s) de linked_accounts, "
                f"{len(trabajos)} pendiente(s) para procesar")

        if not trabajos:
            log("No hay usuarios para procesar")
            return 0
        correr_lote(trabajos, guardar, headless, pausa=args.pausa)
        return 0
    except psycopg.Error as e:
        log(f"ERROR con la base '{config.DB_NAME}' en {config.DB_HOST}:{config.DB_PORT}: {e}")
        return 1
    except fuente_db.CandadoPerdido as e:
        log(f"ERROR: {e}")
        return 1
    except (BotError, PWTimeout):
        return 1  # fallo el login: ya quedo logueado y con captura en debug/
    finally:
        db.cerrar()  # al cerrar la conexion se libera el candado


def vaciar_cola(guardar: bool, headless: bool) -> Counter:
    """
    Procesa la base hasta que no quedan pendientes (lo usa el servidor al "despertar").
    - Primero toma el candado de la base: si otra corrida (otro servidor, un cron) esta trabajando,
      sale sin hacer nada; esa corrida re-sincroniza al terminar y toma lo nuevo.
    - En cada vuelta re-sincroniza, asi tambien toma las cuentas que se vincularon mientras trabajaba.
    - Cada cuenta se procesa como maximo una vez por llamada: las que quedan pendientes por una falla
      pasajera esperan al proximo despertar (asi no se queda en loop contra una plataforma caida).
    Devuelve cuantas cuentas terminaron en cada resultado.
    """
    global _USA_DB
    _USA_DB = True
    db = fuente_db.BaseDatos()
    vistas: set[str] = set()  # linked_account_id ya procesados en esta llamada
    conteo: Counter = Counter()
    try:
        if not db.tomar_candado():
            log("Otra corrida del bot esta en curso (candado de la base tomado): este despertar no hace nada")
            return conteo
        while True:
            nuevas = fuente_db.sincronizar()
            cola = [c for c in fuente_db.pendientes() if c.linked_account_id not in vistas]
            log(f"Base '{config.DB_NAME}': {nuevas} cuenta(s) nueva(s) importada(s), {len(cola)} para procesar")
            if not cola:
                return conteo
            vistas.update(c.linked_account_id for c in cola)
            conteo.update(correr_lote(cola, guardar, headless).values())
    finally:
        db.cerrar()  # al cerrar la conexion se libera el candado


def correr_lote(trabajos: list[fuente_db.Cuenta], guardar: bool, headless: bool,
                solo_login: bool = False, pausa: bool = False) -> dict[fuente_db.Cuenta, str]:
    """
    Abre el navegador, se loguea y procesa la lista. Devuelve el resultado final de cada cuenta.
    Si no puede loguearse lanza BotError/PWTimeout (con captura en debug/).
    """
    lote = datetime.now().strftime("%Y%m%d_%H%M%S")
    limpiar_debug()

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

            log(f"Lote {lote}: {len(trabajos)} cuenta(s), guardar={guardar}, "
                f"tope busqueda={config.TIMEOUT_BUSQUEDA_MS / 1000:g} s")
            resultados = procesar_lista(sesion, trabajos, guardar, lote, intento=1)

            timeouts = [t for t, e in resultados.items() if e == "TIMEOUT_BUSQUEDA"]
            if timeouts and config.REINTENTAR_TIMEOUTS and "NO_PROCESADO" not in resultados.values():
                log(f"Reintentando {len(timeouts)} usuario(s) que dieron TIMEOUT_BUSQUEDA")
                resultados.update(procesar_lista(sesion, timeouts, guardar, lote, intento=2))
        finally:
            if pausa and sesion.page is not None:
                sesion.page.pause()
            sesion.terminar()  # cierra la sesion en Traxion: no queda viva al terminar el lote
            context.close()
            browser.close()

    resumen(lote, resultados)
    return resultados


if __name__ == "__main__":
    sys.exit(main())
