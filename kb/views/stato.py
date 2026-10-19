"""Stato del server per la riga in alto, e le impostazioni."""
import json
import time

from django.conf import settings
from django.http import HttpResponseBadRequest, JsonResponse
from django.db.models import Count
from django.views.decorators.http import require_GET, require_http_methods

from .. import config, jobs, ollama, vectorstore
from ..models import KnowledgeBase
from .comune import api_login_required, api_staff_required, kb_from_request


def _kb_list(user):
    """Elenco delle knowledge che ``user`` può aprire, con conteggio documenti
    (per il selettore).

    Il conteggio arriva dalla stessa query dell'elenco: una COUNT per
    knowledge, su una riga di stato ricaricata ogni 15 secondi da ogni scheda,
    si moltiplica in fretta."""
    return [{
        "slug": kb.slug,
        "name": kb.name,
        "is_default": kb.is_default,
        "documents": kb.n_documenti,
        "riservata": kb.riservata,
        # dove stanno i file sul disco: la chat lo mostra sulle fonti
        "cartella": str(kb.docs_dir),
    } for kb in KnowledgeBase.objects.visibili(user).annotate(n_documenti=Count("documents"))]


def lavoro_per(user) -> dict:
    """Stato della coda dei lavori (jobs.stato) come può vederlo ``user``.

    Un lavoro su una knowledge riservata porta con sé il nome della knowledge e
    del file che si sta leggendo: a chi non la può aprire resta solo il fatto
    che il server è occupato, e per quanto."""
    esito = jobs.stato()
    if not esito or user.is_staff:
        return esito
    visibili = set(KnowledgeBase.objects.visibili(user).values_list("slug", flat=True))
    if esito["kb"] not in visibili:
        esito.update(kb=None, kb_nome="", descrizione="Lavoro su un'altra knowledge",
                     errore="", stats={}, file="")
    esito["in_coda"] = [l if l["kb"] in visibili
                        else {"id": l["id"], "kb": None, "descrizione": "Lavoro su un'altra knowledge"}
                        for l in esito["in_coda"]]
    return esito


# La riga di stato viene ricaricata ogni 15 secondi da ogni scheda aperta:
# rileggere l'intero albero di documents/ ogni volta è sprecato, il risultato
# cambia solo quando si aggiungono file a mano. Su un archivio di posta da
# decine di migliaia di file la scansione dura, e avviene nel thread della
# richiesta: cinque minuti di cache sono un compromesso onesto fra il costo e
# il ritardo con cui compare l'avviso.
_FUORI_CACHE = {"scadenza": 0.0, "valore": []}
_FUORI_TTL = 300


def _fuori_knowledge() -> list[dict]:
    """Contenuto di documents/ che non appartiene a nessuna knowledge.

    La scansione parte da ``documents/<slug>/``: una cartella con un nome che
    non corrisponde a nessuno slug (knowledge cancellata, materiale copiato a
    mano) e i file lasciati nella radice non vengono indicizzati e non danno
    alcun segnale. Qui li contiamo per poterlo dire nell'interfaccia."""
    if time.monotonic() < _FUORI_CACHE["scadenza"]:
        return _FUORI_CACHE["valore"]
    root = settings.KB_DOCS_DIR
    if not root.exists():
        # anche questo è un risultato: senza salvarlo, la scansione (fallita)
        # ripartirebbe a ogni richiesta
        _FUORI_CACHE.update(scadenza=time.monotonic() + _FUORI_TTL, valore=[])
        return []
    slugs = set(KnowledgeBase.objects.values_list("slug", flat=True))
    leggibili = settings.KB_ALL_EXT - config.estensioni_escluse()
    cartelle = config.cartelle_escluse()
    fuori = []
    for path in sorted(root.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_dir():
            if path.name in slugs or path.name.lower() in cartelle:
                continue
            n = sum(1 for f in config.scorri(path, cartelle)
                    if f.suffix.lower() in leggibili and f.is_file())
            nome = path.name + "/"
        else:
            if path.suffix.lower() not in leggibili:
                continue
            n, nome = 1, path.name
        if n:
            fuori.append({"nome": nome, "file": n})
    _FUORI_CACHE.update(scadenza=time.monotonic() + _FUORI_TTL, valore=fuori)
    return fuori


_NON_SUPPORTATI_CACHE = {"scadenza": 0.0, "valore": {}}


def _non_supportati() -> dict:
    """File dentro le knowledge che nessuna pipeline sa leggere.

    Un .zip o un formato esotico in mezzo ai documenti non viene indicizzato e,
    senza questo conteggio, nessuno se ne accorge: due archivi mbox sono
    rimasti mesi in una cartella senza che l'interfaccia lo dicesse."""
    if time.monotonic() < _NON_SUPPORTATI_CACHE["scadenza"]:
        return _NON_SUPPORTATI_CACHE["valore"]
    estensioni: dict[str, int] = {}
    for kb in KnowledgeBase.objects.all():
        if not kb.docs_dir.exists():
            continue
        # un'estensione esclusa (in /config o nel filtro della knowledge) l'ha
        # scelta l'utente: non è una sorpresa da segnalare
        ignora = settings.KB_ALL_EXT | config.estensioni_escluse(kb)
        # e così le cartelle escluse: dentro un node_modules ci sono migliaia di .js
        for path in config.scorri(kb.docs_dir, config.cartelle_escluse(kb)):
            if not path.is_file():
                continue
            ext = path.suffix.lower()
            if ext in ignora:
                continue
            chiave = ext or "(senza estensione)"
            estensioni[chiave] = estensioni.get(chiave, 0) + 1
    valore = {"file": sum(estensioni.values()), "estensioni": estensioni}
    _NON_SUPPORTATI_CACHE.update(scadenza=time.monotonic() + _FUORI_TTL, valore=valore)
    return valore


@require_GET
@api_login_required
def status(request):
    oll = ollama.ollama_available()
    chat_model = config.get("chat_model")
    embed_model = config.get("embed_model")
    missing = []
    if oll["ok"]:
        # nome completo, con il tag: «qwen3.5:9b» non c'è se è installato solo
        # «qwen3.5:27b». Senza tag Ollama intende «:latest»
        def completo(nome):
            return nome if ":" in nome else f"{nome}:latest"
        installed = {completo(m) for m in oll["models"]}
        for needed in (embed_model, chat_model):
            if completo(needed) not in installed:
                missing.append(needed)
    kb = kb_from_request(request)
    knowledges = _kb_list(request.user)
    # il conteggio della knowledge attiva sta già nell'elenco appena letto:
    # ripeterlo sarebbe una seconda COUNT sulla stessa tabella
    documenti = next((k["documents"] for k in knowledges
                      if kb and k["slug"] == kb.slug), 0)
    return JsonResponse({
        "ollama": oll,
        "modelli_mancanti": missing,
        "knowledges": knowledges,
        "fuori_knowledge": _fuori_knowledge(),
        "non_supportati": _non_supportati(),
        "lavoro": lavoro_per(request.user),
        # fermata dall'utente: niente indicizzazione automatica finché non la riavvia
        "sospesa": bool(kb and jobs.sospesa(kb)),
        "kb": kb.slug if kb else None,
        "documenti": documenti,
        # Lo stato della collection prende il lock di Qdrant: durante
        # un'indicizzazione la riga di stato resterebbe ferma ad aspettare gli
        # upsert. Nessuna pagina lo mostra, quindi si dà solo a chi lo chiede.
        "qdrant": (vectorstore.collection_info(kb)
                   if kb and request.GET.get("qdrant") else None),
        "cartella_documenti": str(kb.docs_dir) if kb else str(settings.KB_DOCS_DIR),
        "chat_model": chat_model,
        "embed_model": embed_model,
        "utente": {
            "username": request.user.username,
            "is_staff": request.user.is_staff,
        },
    })


@require_http_methods(["GET", "POST"])
@api_staff_required
def config_view(request):
    """Sezione amministrazione: legge e salva le impostazioni runtime."""
    if request.method == "POST":
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return HttpResponseBadRequest("JSON non valido")
        errors = config.set_values(body.get("values", {}))
        if errors:
            return JsonResponse({"errors": errors}, status=400)
        # estensioni e cartelle escluse cambiano i conteggi della riga di stato
        _FUORI_CACHE["scadenza"] = _NON_SUPPORTATI_CACHE["scadenza"] = 0.0
    return JsonResponse({
        "fields": config.dump(),
        # con le capacità, così il menu di ogni campo propone i modelli adatti
        "ollama_models": ollama.modelli_installati(),
        "servizio": ollama.servizio(),
        # campi di testo con un elenco di valori possibili, mostrati come menu
        "scelte": {},
    })
