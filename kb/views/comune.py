"""Pezzi comuni alle viste: autenticazione, knowledge dalla richiesta, file statici."""
from functools import wraps
from pathlib import Path

from django.http import FileResponse, HttpResponseNotModified, JsonResponse
from django.utils.http import http_date

from ..models import KnowledgeBase

# kb/static: questo modulo sta in kb/views/, una cartella più in basso
STATIC = Path(__file__).resolve().parent.parent / "static"


def servi_file(request, f: Path):
    """Un file statico con ``ETag`` e ``Last-Modified``.

    Le pagine e i loro script venivano riletti dal disco e rispediti interi a
    ogni caricamento. Ora il browser li tiene e a ogni apertura chiede soltanto
    se sono cambiati (``no-cache`` = rivalida sempre): se no, 304 senza corpo.
    ``private`` perché dietro c'è un login, e nessuna cache condivisa deve
    tenerne una copia. L'impronta viene da data di modifica e dimensione, così
    un file aggiornato si ricarica subito."""
    st = f.stat()
    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    richiesti = {e.strip() for e in request.headers.get("If-None-Match", "").split(",")}
    if etag in richiesti:
        risposta = HttpResponseNotModified()
    else:
        risposta = FileResponse(open(f, "rb"))
        risposta["Last-Modified"] = http_date(st.st_mtime)
    risposta["ETag"] = etag
    risposta["Cache-Control"] = "private, no-cache"
    return risposta


def resolve_kb(slug, user):
    """Ritorna la knowledge con quello slug, altrimenti la default/prima, fra
    quelle che ``user`` può aprire. Una knowledge riservata a cui l'utente non
    ha accesso si comporta come uno slug inesistente."""
    visibili = KnowledgeBase.objects.visibili(user)
    if slug:
        kb = visibili.filter(slug=slug).first()
        return kb
    return visibili.filter(is_default=True).first() or visibili.first()


def kb_from_request(request, body=None):
    """Knowledge attiva: da ?kb=… (GET) o dal campo 'kb' del body JSON (POST)."""
    slug = request.GET.get("kb") or (body or {}).get("kb")
    return resolve_kb(slug, request.user)


def api_login_required(view):
    """Come login_required, ma per le API: 401 JSON invece del redirect alla login."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"detail": "Autenticazione richiesta"}, status=401)
        return view(request, *args, **kwargs)
    return wrapper


def api_staff_required(view):
    """API riservate agli utenti staff (configurazione)."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"detail": "Autenticazione richiesta"}, status=401)
        if not request.user.is_staff:
            return JsonResponse({"detail": "Permessi insufficienti"}, status=403)
        return view(request, *args, **kwargs)
    return wrapper


def _is_local(request) -> bool:
    """True se il browser gira sulla stessa macchina del server: solo così ha
    senso aprire un dialogo del Finder, che compare sullo schermo del server."""
    return request.META.get("REMOTE_ADDR") in ("127.0.0.1", "::1")
