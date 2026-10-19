from django.contrib.auth.decorators import login_required

from django.http import JsonResponse

from django.shortcuts import redirect

from django.views.decorators.csrf import ensure_csrf_cookie

from django.views.decorators.http import require_GET

from .comune import STATIC, api_login_required, servi_file

@require_GET
@login_required
@ensure_csrf_cookie
def index(request):
    """Knowledge e configurazione: scelta e gestione delle knowledge, documenti,
    indice, mappa dei temi, ricerca e anteprima. Ricerca limitata ai documenti locali."""
    return servi_file(request, STATIC / "index.html")

@require_GET
@login_required
@ensure_csrf_cookie
def config_page(request):
    if not request.user.is_staff:
        return redirect("/")
    return servi_file(request, STATIC / "admin.html")

def _file_di(cartella: str, request, name):
    """Un file di static/<cartella>. Il nome arriva dall'URL: va risolto e
    confrontato con la cartella, altrimenti un ".." porterebbe a leggere file
    fuori da lì."""
    radice = (STATIC / cartella).resolve()
    f = (radice / name).resolve()
    if radice not in f.parents or not f.is_file():
        return JsonResponse({"detail": "Non trovato"}, status=404)
    return servi_file(request, f)

@require_GET
@api_login_required
def lib_view(request, name):
    """Librerie JavaScript di terze parti (force-graph, Chart.js)."""
    return _file_di("lib", request, name)

@require_GET
def font_view(request, name):
    """I caratteri delle pagine (static/font: Inter, Montserrat), ospitati qui
    invece che da Google: I caratteri sono distribuiti insieme al progetto.
    Senza login, come il marchio: servono già alla pagina di accesso."""
    return _file_di("font", request, name)

@require_GET
def marchio_view(request, name):
    """Logo e icone (static/marchio). Senza login: servono già alla pagina
    di accesso e alla favicon, e non contengono nulla di riservato."""
    return _file_di("marchio", request, name)

@require_GET
@api_login_required
def app_view(request, name):
    """Fogli di stile e script delle pagine dell'app (static/app)."""
    return _file_di("app", request, name)

home = index


@require_GET
@login_required
@ensure_csrf_cookie
def chat_page(request):
    return servi_file(request, STATIC / "chat.html")
