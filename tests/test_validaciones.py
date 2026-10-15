"""
Test de las validaciones antes de Aceptar (punto 2 de Fede) y del cierre de sesion (punto 5), con un
navegador REAL (Playwright, headless) contra paginas que imitan a Traxion: grilla de Cuentas, pantalla
"Usuarios de la cuenta", formulario de modificacion y menu de salida. Como el ajaxifier real, cada click
reemplaza el <body>, asi que tambien ejercita click_y_esperar.

No reemplaza la prueba contra Traxion testing (el HTML real puede diferir en detalles): verifica la logica.

    python tests/test_validaciones.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import traxion_bot as bot  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

config.LOG_DIR = Path(tempfile.mkdtemp()) / "logs"
config.TRAXION_USER = "operador_prueba"
U = "cib7kb9v3scb2sltq6yx6cf17"

# Simula la navegacion de Traxion: cada click con data-ir reemplaza el <body> (como el ajaxifier) y las
# pestañas de filtros cargan su contenido APARTE, segun window.modoPestana:
#   cargada -> ya viene con el campo | tarde -> llega 300 ms despues (lo que paso en la corrida real)
#   forzar  -> solo carga si se activa otra pestaña y se vuelve | nunca -> no carga nunca
JS_NAVEGACION = """
window.llenarPestana = () => {
  const p = document.getElementById('p-usuario');
  if (p && !p.innerHTML.trim()) p.innerHTML = window.panelUsuario;
};
window.aplicarModo = () => {
  if (window.modoPestana === 'tarde') setTimeout(window.llenarPestana, 300);
};
document.addEventListener('click', ev => {
  if (ev.target.id === 'menu-usuario') {            // el menu del usuario solo se despliega (no va al servidor)
    ev.preventDefault();
    const dd = document.getElementById('desplegable');
    dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
    return;
  }
  const toggle = ev.target.closest('.dropdown-toggle');
  if (toggle) {                                     // desplegable estilo Bootstrap/Metronic
    ev.preventDefault();
    if (window.menuRoto) return;                    // los scripts del menu todavia no se engancharon
    const m = toggle.parentElement.querySelector('.dropdown-menu');
    m.style.display = m.style.display === 'none' ? 'block' : 'none';
    return;
  }
  const tab = ev.target.closest('li[role=tab]');
  if (tab) {                                        // pestañas jQuery UI
    ev.preventDefault();
    document.querySelectorAll('li[role=tab]').forEach(t => t.setAttribute('aria-selected', String(t === tab)));
    if (tab.textContent.trim() === 'General') window.pasoPorGeneral = true;
    if (tab.textContent.trim() === 'Usuario' && window.modoPestana !== 'nunca'
        && (window.modoPestana !== 'forzar' || window.pasoPorGeneral)) window.llenarPestana();
    return;
  }
  const el = ev.target.closest('[data-ir]');
  if (!el) return;
  ev.preventDefault();
  if (el.dataset.confirmar && !confirm(el.dataset.confirmar)) return;   // confirm() de JS
  setTimeout(() => {                                 // "respuesta AJAX"
    document.body.innerHTML = window.pantallas[el.dataset.ir];
    window.aplicarModo();
  }, 40);
});
window.aplicarModo();
"""


def panel_usuario(filtro=U):
    return f'<div class="form-group"><label>Usuario</label><div><input type="text" value="{filtro}"></div></div>'


def grilla(numero="138851", panel=None):
    """panel=None: la pestaña viene vacia (su contenido lo pone JS_NAVEGACION segun modoPestana)."""
    return f"""
    <ul>
      <li role="tab" aria-selected="false" aria-controls="p-general"><a href="#">General</a></li>
      <li role="tab" aria-selected="true" aria-controls="p-usuario"><a href="#">Usuario</a></li>
    </ul>
    <div id="p-general" class="ui-tabs-panel" aria-hidden="true"></div>
    <div id="p-usuario" class="ui-tabs-panel" aria-hidden="false">{panel or ''}</div>
    <input type="submit" value="Buscar">
    <div class="caption">Datos: 1</div>
    <table>
      <tr><th></th><th>Numero</th><th>Nombre</th><th>Tipo</th><th>Estado</th><th>Homolog</th><th>Padre</th>
          <th>Usuarios</th><th>Historial</th><th>V</th><th>M</th><th>R</th></tr>
      <tr><td></td><td>{numero}</td><td>Prueba de Mail al crear</td><td>Punto de venta</td><td>Abierta</td>
          <td></td><td>109165-TR Autogestion</td>
          <td><a href="#" data-ir="usuarios"><input type="image" src="/ipsImages/commonUser.gif"></a></td>
          <td></td><td></td>
          <td><span title="Modificar el registro"><a href="#" data-ir="formulario">M</a></span></td><td></td></tr>
    </table>"""


def usuarios(numero="138851", ids=(U,)):
    filas = "".join(f"<tr><td>Superuser</td><td>{numero}</td><td>{i}</td><td>LOPEZ, PEDRO</td></tr>" for i in ids)
    return f"""
    <h3>Usuarios de la cuenta: {numero}-Prueba de Mail al crear</h3>
    <input type="submit" value="Volver" data-ir="grilla">
    <table>
      <tr><th>Rol de Usuario</th><th>Cuenta Asignada</th><th>ID Usuario</th><th>Nombre</th></tr>{filas}
    </table>"""


def formulario(numero="138851"):
    return f"""
    <div class="caption">Modificación de Cuenta Comercial</div>
    <div class="form-group"><label>Numero de Cuenta</label><div><input value="{numero}" disabled></div></div>
    <div class="form-group"><label>Monto credito</label><div><input value="0.00"></div></div>
    <input type="submit" value="Aceptar">"""


LOGIN = '<input id="usuario"><input id="password" type="password"><button>Ingresar</button>'
INICIO = """
    <a href="#" id="menu-usuario">operador_prueba</a>
    <ul id="desplegable" style="display:none">
      <li>Usr: operador_prueba</li><li><a href="#" data-ir="salir">Salir de plataforma</a></li>
    </ul>"""
SALIR = """<h1>Desea cerrar la plataforma?</h1>
    <input type="submit" value="Salir" data-ir="login"> <input type="submit" value="No">"""


def inicio_metronic(confirmar=""):
    """
    Encabezado como el del tema Metronic: el menu del usuario es un desplegable Bootstrap, y ANTES hay una
    copia oculta de "Salir de plataforma" (menu para celular). Con confirmar=..., la salida se confirma con
    un confirm() de JS en vez de una pantalla con boton "Salir".
    """
    destino = 'data-ir="login" data-confirmar="' + confirmar + '"' if confirmar else 'data-ir="salir"'
    return f"""
    <ul class="menu-celular" style="display:none"><li><a href="#" data-ir="salir">Salir de plataforma</a></li></ul>
    <div style="display:none"><input type="submit" value="Salir"></div>
    <ul class="nav navbar-nav pull-right">
      <li class="dropdown dropdown-user">
        <a href="javascript:;" class="dropdown-toggle" data-toggle="dropdown">
          <span class="username">operador_prueba</span> <i class="fa fa-angle-down"></i></a>
        <ul class="dropdown-menu" style="display:none">
          <li>Usr: operador_prueba</li><li><a href="#" {destino}>Salir de plataforma</a></li>
        </ul>
      </li>
    </ul>"""


def probar_salida(page, html, menu_roto=False, pantallas=None):
    page.set_content(f"<html><body>{html}</body></html>")
    page.evaluate("([p, roto]) => { window.pantallas = p; window.menuRoto = roto; }",
                  [pantallas or {"salir": SALIR, "login": LOGIN}, menu_roto])
    page.evaluate(JS_NAVEGACION)
    bot.cerrar_sesion_traxion(page, timeout=3000)
    assert bot.es_pantalla_login(page), "despues de salir tiene que quedar en el login"


def preparar(page, modo="cargada", filtro=U, **pantallas):
    base = {"grilla": grilla(panel=panel_usuario(filtro) if modo == "cargada" else None),
            "usuarios": usuarios(), "formulario": formulario(), "login": LOGIN, "salir": SALIR}
    base.update(pantallas)
    page.set_content(f"<html><body>{base['grilla']}</body></html>")
    page.evaluate("([p, panel, modo]) => { window.pantallas = p; window.panelUsuario = panel;"
                  " window.modoPestana = modo; window.pasoPorGeneral = false; }",
                  [base, panel_usuario(filtro), modo])
    page.evaluate(JS_NAVEGACION)


def recorrido(page):
    """Lo mismo que hace procesar_usuario entre la busqueda y el formulario."""
    bot.verificar_filtro(page, U)
    cuenta = bot.leer_resultado(page)
    bot.verificar_usuarios_de_la_cuenta(page, U, cuenta)
    bot.volver_a_la_grilla(page, U, cuenta)
    bot.abrir_edicion(page, cuenta)
    return cuenta


def debe_fallar(page, pantallas, contiene, modo="cargada", filtro=U, error=None):
    """Por defecto espera NO_COINCIDE. Con error=bot.BotError espera ERROR (y que NO sea NO_COINCIDE)."""
    preparar(page, modo=modo, filtro=filtro, **pantallas)
    esperado = error or bot.NoCoincide
    try:
        recorrido(page)
    except esperado as e:
        assert contiene in str(e), str(e)
        if error is not None:
            assert not isinstance(e, bot.NoCoincide), f"no poder leer no es NO_COINCIDE: {e}"
            assert getattr(e, "estado", "ERROR") == "ERROR"
        return
    raise AssertionError(f"tendria que haber fallado ({contiene})")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_default_timeout(5000)

    # Todo coincide: llega al formulario de la cuenta correcta
    preparar(page)
    assert recorrido(page)["numero"] == "138851"
    assert page.get_by_text("Modificación de Cuenta Comercial").count() == 1
    print("ok  todo coincide: llega al formulario")

    # La pestaña "Usuario" carga su contenido despues de la grilla (lo que paso en la corrida real):
    # el bot espera a que llegue, y si no llega solo, la fuerza
    preparar(page, modo="tarde")
    recorrido(page)
    print("ok  pestaña 'Usuario' que carga tarde: espera y sigue")
    preparar(page, modo="forzar")
    recorrido(page)
    print("ok  pestaña 'Usuario' que no carga sola: la fuerza (General -> Usuario) y sigue")

    # Si el filtro no se puede LEER es una falla del bot (ERROR, se reintenta), NO un NO_COINCIDE
    debe_fallar(page, {}, "no cargo la pestaña 'Usuario'", modo="nunca", error=bot.BotError)
    debe_fallar(page, {}, "No se pudo leer el filtro", filtro="", error=bot.BotError)
    debe_fallar(page, {"formulario": formulario(numero="")}, "No se pudo leer el 'Numero de Cuenta'",
                error=bot.BotError)
    print("ok  no poder leer filtro / numero de cuenta -> ERROR (reintentable), no NO_COINCIDE")

    # 1. La grilla no es de esta busqueda (el filtro dice otra cosa)
    debe_fallar(page, {}, "el filtro 'Usuario' dice 'otro_usuario'", filtro="otro_usuario")
    print("ok  validacion 1: filtro distinto -> NO_COINCIDE")

    # 2. El usuario buscado no esta entre los usuarios de la cuenta
    debe_fallar(page, {"usuarios": usuarios(ids=("otro_usuario", "y_otro"))}, "tiene los usuarios")
    # ...ni siquiera con otra combinacion de mayusculas: la comparacion es EXACTA
    debe_fallar(page, {"usuarios": usuarios(ids=(U.upper(),))}, "tiene los usuarios")
    # ...o la pantalla de usuarios es de otra cuenta
    debe_fallar(page, {"usuarios": usuarios(numero="999999")}, "es de la cuenta 999999")
    print("ok  validacion 2: usuario ausente / mayusculas / otra cuenta -> NO_COINCIDE")

    # Con varios usuarios en la cuenta alcanza con que este el buscado
    preparar(page, usuarios=usuarios(ids=("admin_comercio", U)))
    recorrido(page)
    print("ok  validacion 2: cuenta con varios usuarios, uno es el buscado -> sigue")

    # 3. El formulario que abrio el lapiz es de otra cuenta
    debe_fallar(page, {"formulario": formulario(numero="999999")}, "El formulario es de la cuenta '999999'")
    print("ok  validacion 3: formulario de otra cuenta -> NO_COINCIDE")

    # Cierre de sesion: menu del usuario -> "Salir de plataforma" -> "Salir" -> login
    probar_salida(page, INICIO)
    print("ok  cierre de sesion: menu -> Salir de plataforma -> Salir -> login")

    # Encabezado Metronic con copias ocultas de "Salir de plataforma" y "Salir" antes de las visibles
    probar_salida(page, inicio_metronic())
    print("ok  cierre de sesion: ignora las copias ocultas (menu de celular) y usa el desplegable")

    # El desplegable no se abre (sus scripts todavia no se engancharon): activa el link oculto directo
    probar_salida(page, inicio_metronic(), menu_roto=True)
    print("ok  cierre de sesion: si el menu no se despliega, activa 'Salir de plataforma' directo")

    # La salida se confirma con un confirm() de JS (lo acepta el manejador de dialogos de la Sesion)
    page.once("dialog", lambda d: d.accept())
    probar_salida(page, inicio_metronic(confirmar="Desea cerrar la plataforma?"), pantallas={"login": LOGIN})
    print("ok  cierre de sesion: confirmacion con confirm() de JS -> login sin buscar el boton")

    # Si la pagina ya esta en el login (sesion vencida) no hace nada; si esta trabada, no rompe nada
    config.DEBUG_DIR = Path(tempfile.mkdtemp()) / "debug"
    bot.cerrar_sesion_traxion(page, timeout=1000)
    page.set_content("<html><body><p>pagina trabada, sin menu</p></body></html>")
    bot.cerrar_sesion_traxion(page, timeout=1000)  # solo deja un AVISO en el log (con el paso y una captura)
    assert len(list(config.DEBUG_DIR.glob("*logout*.png"))) == 1, "el aviso tiene que dejar una captura"
    print("ok  cierre de sesion: en el login o con la pagina trabada no rompe")

    browser.close()

log = "".join(f.read_text(encoding="utf-8") for f in config.LOG_DIR.glob("*.log"))
assert "Sesion de Traxion cerrada" in log and "no se pudo cerrar la sesion de Traxion" in log
assert "en el paso" in log, "el aviso tiene que decir en que paso se trabo"
print("TEST VALIDACIONES OK")
