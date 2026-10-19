"""Ricerca, mappa e lettura dei soli documenti della knowledge selezionata."""
from datetime import datetime, timezone

from django.conf import settings
from django.http import FileResponse, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from .. import filtri, graph, ollama, retrieval
from ..models import Document
from .comune import api_login_required, kb_from_request


def _non_trovata():
    return JsonResponse({"detail": "Knowledge non trovata"}, status=404)


def _parametri(request):
    params = request.GET.copy()
    entita = params.get("entita", "")
    if entita.startswith("#") and ":" in entita:
        tipo, valore = entita[1:].split(":", 1)
        if tipo in {"tema", "persona", "org", "tag"}:
            params[tipo] = valore
        elif tipo in {"documento", "doc"}:
            params.setlist("docs", [valore])
    return params


@require_GET
@api_login_required
def graph_view(request):
    kb = kb_from_request(request)
    return JsonResponse(graph.build_graph(kb)) if kb else _non_trovata()


@require_GET
@api_login_required
def documents_view(request):
    kb = kb_from_request(request)
    if kb is None:
        return _non_trovata()
    params = _parametri(request)
    filters = filtri.da_parametri(params, kb)
    docs = Document.objects.filter(knowledge=kb)
    if filters.get("tipi"):
        docs = docs.filter(kind__in=filters["tipi"])
    if "docs" in filters:
        docs = docs.filter(rel_path__in=filters["docs"])
    if any(params.get(k) for k in ("tema", "persona", "org", "tag")) and not filters.get("docs"):
        docs = docs.none()
    for key, lookup in (("da", "doc_date__gte"), ("a", "doc_date__lte")):
        if filters.get(key):
            docs = docs.filter(**{lookup: datetime.fromtimestamp(filters[key], tz=timezone.utc)})
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
        limit = min(500, max(1, int(request.GET.get("limit", 200))))
    except ValueError:
        return JsonResponse({"detail": "Paginazione non valida"}, status=400)
    total = docs.count()
    rows = [{"rel_path": d.rel_path, "title": d.title, "kind": d.kind,
             "topic": d.topic, "preview": d.preview[:600]}
            for d in docs.order_by("rel_path")[offset:offset + limit]]
    return JsonResponse({"documents": rows, "total": total, "offset": offset,
                         "next_offset": offset + limit if offset + limit < total else None})


@require_GET
@api_login_required
def search_view(request):
    kb = kb_from_request(request)
    if kb is None:
        return _non_trovata()
    query = request.GET.get("q", "").strip()
    if not query:
        return JsonResponse({"results": []})
    params = _parametri(request)
    filters = filtri.da_parametri(params, kb)
    if any(params.get(k) for k in ("tema", "persona", "org", "tag")) and not filters.get("docs"):
        return JsonResponse({"results": []})
    try:
        hits = retrieval.search(kb, query, filtri=filters)
        return JsonResponse({"results": graph.con_entita(kb, hits)})
    except ollama.OllamaError as error:
        return JsonResponse({"detail": str(error)}, status=503)


def _documento(request):
    kb = kb_from_request(request)
    path = request.GET.get("path", "")
    doc = Document.objects.filter(knowledge=kb, rel_path=path).first() if kb else None
    if doc is None:
        return None, None, JsonResponse({"detail": "Documento non trovato"}, status=404)
    root = kb.docs_dir.resolve()
    full = (root / path).resolve()
    if not full.is_relative_to(root):
        return None, None, JsonResponse({"detail": "Percorso non valido"}, status=400)
    return doc, full, None


@require_GET
@api_login_required
def doc_view(request):
    doc, full, error = _documento(request)
    if error is not None:
        return error
    if full.suffix.lower() in settings.KB_TEXT_EXT and full.is_file():
        text = full.read_text(encoding="utf-8", errors="replace")
    else:
        text = doc.preview or "(anteprima non disponibile)"
    return HttpResponse(text, content_type="text/plain; charset=utf-8")


@require_GET
@api_login_required
def file_view(request):
    doc, full, error = _documento(request)
    if error is not None:
        return error
    if not full.is_file():
        return JsonResponse({"detail": "File non presente su disco"}, status=404)
    response = FileResponse(full.open("rb"), filename=full.name,
                            as_attachment=full.suffix.lower() in {".html", ".htm", ".svg"})
    response["Content-Security-Policy"] = "sandbox"
    return response
