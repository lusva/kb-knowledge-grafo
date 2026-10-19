"""Knowledge: creazione, eliminazione, file, importazione, indicizzazione e temi."""
import json
import logging
import queue
import re
import threading

from django.db import close_old_connections
from django.http import HttpResponseBadRequest, JsonResponse, StreamingHttpResponse
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .. import config, elimina, jobs, importer, ollama, picker, reindex as reindex_module, temi, vectorstore
from ..models import Document, KnowledgeBase
from .comune import _is_local, api_login_required, kb_from_request, resolve_kb
from .stato import _FUORI_CACHE, _NON_SUPPORTATI_CACHE, _kb_list, lavoro_per

log = logging.getLogger("kb.knowledge")


@require_http_methods(["GET", "POST"])
@api_login_required
def kbs_view(request):
    """GET: elenco knowledge. POST {name}: crea una nuova knowledge."""
    if request.method == "POST":
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return HttpResponseBadRequest("JSON non valido")
        name = str(body.get("name", "")).strip()
        if not name:
            return JsonResponse({"detail": "Nome mancante"}, status=400)
        slug = slugify(name)[:140] or "knowledge"
        if KnowledgeBase.objects.filter(slug=slug).exists() or \
           KnowledgeBase.objects.filter(name=name).exists():
            return JsonResponse({"detail": "Esiste già una knowledge con questo nome"}, status=409)
        kb = KnowledgeBase.objects.create(name=name, slug=slug)
        kb.docs_dir.mkdir(parents=True, exist_ok=True)
        return JsonResponse({"slug": kb.slug, "name": kb.name, "documents": 0})
    return JsonResponse({"knowledges": _kb_list(request.user)})


@require_http_methods(["DELETE"])
@api_login_required
def kb_detail_view(request, slug):
    """Elimina una knowledge: documenti, vettori e cartella su disco."""
    kb = KnowledgeBase.objects.visibili(request.user).filter(slug=slug).first()
    if kb is None:
        return JsonResponse({"detail": "Knowledge non trovata"}, status=404)
    if KnowledgeBase.objects.count() <= 1:
        return JsonResponse({"detail": "Non puoi eliminare l'ultima knowledge"}, status=400)

    vectorstore.delete_collection(kb)
    docs_dir = kb.docs_dir
    was_default = kb.is_default
    kb.delete()  # i Document (e i Chunk) vengono rimossi in cascata

    import shutil
    if docs_dir.exists():
        shutil.rmtree(docs_dir, ignore_errors=True)

    if was_default:
        other = KnowledgeBase.objects.first()
        if other:
            KnowledgeBase.objects.filter(pk=other.pk).update(is_default=True)
    return JsonResponse({"ok": True})


@require_POST
@api_login_required
def pick_view(request):
    """Apre il selettore grafico del sistema e ritorna i percorsi scelti."""
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("JSON non valido")
    kind = "folder" if body.get("kind") == "folder" else "file"
    if not picker.available() or not _is_local(request):
        return JsonResponse({"detail": "Selettore grafico non disponibile"}, status=501)

    kb = resolve_kb(body.get("kb"), request.user)
    what = "le cartelle (⌘-clic per sceglierne più d'una)" if kind == "folder" else "i file"
    prompt = f"Scegli {what} da aggiungere a «{kb.name}»" if kb else f"Scegli {what} da aggiungere"
    try:
        paths = picker.pick(kind, prompt)
    except picker.PickerError as e:
        return JsonResponse({"detail": str(e)}, status=400)
    if paths is None:
        return JsonResponse({"canceled": True})
    return JsonResponse({"paths": paths})


@require_POST
@api_login_required
def kb_import_view(request, slug):
    """Importa file o cartelle dal disco nella knowledge e reindicizza.

    Risponde in NDJSON: i file trovati mentre si scorrono le cartelle
    ({"type": "cerco"}), l'avanzamento della copia (start/progress, come
    l'indicizzazione), l'esito della copia ({"type": "import"}), poi
    l'avanzamento dell'indicizzazione. Anche un percorso sbagliato arriva
    così, come {"error"}: scorrere una cartella grande non è istantaneo, e
    finché non finisce la pagina deve avere qualcosa da mostrare.

    Il lavoro gira in un thread, come l'indicizzazione nella coda dei lavori:
    chiudere la pagina non lo interrompe a metà, e alla fine è il thread a
    mettere in coda l'indicizzazione."""
    kb = KnowledgeBase.objects.visibili(request.user).filter(slug=slug).first()
    if kb is None:
        return JsonResponse({"detail": "Knowledge non trovata"}, status=404)
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("JSON non valido")
    if not _is_local(request):
        return JsonResponse({"detail": "Importazione da disco disponibile solo in locale"}, status=403)
    raw = body.get("paths") or body.get("path") or []
    paths = [raw] if isinstance(raw, str) else list(raw)
    if not paths:
        return JsonResponse({"detail": "Percorso mancante"}, status=400)

    eventi = queue.Queue()

    def avanza(fatti, totale, file, azione):
        eventi.put({"type": "progress", "done": fatti, "total": totale,
                    "file": file, "action": azione})

    def lavora():
        try:
            piano = importer.prepara(kb, paths, lambda n: eventi.put({"type": "cerco", "trovati": n}))
            eventi.put({"type": "start", "label": "Copio i file…", "total": len(piano.voci)})
            imp = importer.copia(piano, avanza)
            eventi.put({"type": "import", "import": imp})
            if ollama.ollama_available()["ok"]:
                eventi.put(jobs.avvia(kb, f"Indicizzazione di «{kb.name}»",
                                      reindex_module.reindex_iter))
            else:
                eventi.put({"type": "avviso",
                            "detail": "File copiati, ma Ollama è spento: reindicizza quando è attivo."})
        except importer.ImportError_ as e:
            eventi.put({"error": str(e)})
        except Exception as e:
            log.exception("importazione in «%s»", kb.slug)
            eventi.put({"error": f"Copia non riuscita: {e}"})
        finally:
            eventi.put(None)
            close_old_connections()

    threading.Thread(target=lavora, name=f"importa-{kb.slug}", daemon=True).start()

    def ndjson():
        while (ev := eventi.get()) is not None:
            if isinstance(ev, jobs.Lavoro):
                # copia finita: da qui in poi si segue l'indicizzazione
                yield from _righe_lavoro(ev)
            else:
                yield json.dumps(ev) + "\n"
    return StreamingHttpResponse(ndjson(), content_type="application/x-ndjson")


def _filtro(kb) -> dict:
    """Il filtro dei file di ``kb`` come lo mostra il dialogo della pagina."""
    escluse = set(kb.escludi_tipi or [])
    return {
        "tipi": [{"chiave": chiave, "nome": nome, "estensioni": sorted(ext),
                  "escluso": chiave in escluse}
                 for chiave, (nome, ext) in config.TIPI_FILE.items()],
        "ext": kb.escludi_ext,
        "cartelle": kb.escludi_cartelle,
        # quelle di /config valgono per tutte le knowledge: qui si vedono soltanto
        "globali": sorted(config.elenco_estensioni(config.get("escludi_ext"))),
        "cartelle_globali": sorted(config.cartelle_escluse()),
    }


@require_http_methods(["GET", "POST"])
@api_login_required
def kb_filtro_view(request, slug):
    """Filtro dei file della knowledge: quali tipi ed estensioni non importare
    né indicizzare, e in quali cartelle non entrare. POST {tipi: [chiavi
    escluse], ext: "…", cartelle: "…"} lo sostituisce.

    Il filtro non tocca il disco: i file già importati di un tipo escluso
    escono dall'indice alla prossima indicizzazione e restano nella cartella,
    pronti a rientrare se il tipo torna ammesso."""
    kb = KnowledgeBase.objects.visibili(request.user).filter(slug=slug).first()
    if kb is None:
        return JsonResponse({"detail": "Knowledge non trovata"}, status=404)
    if request.method == "GET":
        return JsonResponse(_filtro(kb))
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("JSON non valido")
    tipi = body.get("tipi") or []
    ext = str(body.get("ext") or "").strip()
    cartelle = str(body.get("cartelle") or "").strip()
    if not isinstance(tipi, list) or any(t not in config.TIPI_FILE for t in tipi):
        return JsonResponse({"detail": "Tipo di file sconosciuto"}, status=400)
    if len(ext) > KnowledgeBase._meta.get_field("escludi_ext").max_length:
        return JsonResponse({"detail": "Elenco di estensioni troppo lungo"}, status=400)
    if len(cartelle) > KnowledgeBase._meta.get_field("escludi_cartelle").max_length:
        return JsonResponse({"detail": "Elenco di cartelle troppo lungo"}, status=400)
    if any("/" in v.strip().strip("/") for v in re.split(r"[,;\n]+", cartelle)):
        return JsonResponse({"detail": "Scrivi solo nomi di cartelle (es. dist), non percorsi"},
                            status=400)
    if not (config.TIPI_FILE.keys() - set(tipi)):
        return JsonResponse({"detail": "Lascia ammesso almeno un tipo di file"}, status=400)

    prima = (config.estensioni_escluse(kb), config.cartelle_escluse(kb))
    kb.escludi_tipi = [t for t in config.TIPI_FILE if t in tipi]   # ordine stabile
    # salvate normalizzate: ".EML, *.zip" diventa ".eml .zip", "Dist/, build" "build, dist"
    kb.escludi_ext = " ".join(sorted(config.elenco_estensioni(ext)))
    kb.escludi_cartelle = ", ".join(sorted(config.elenco_cartelle(cartelle)))
    kb.save(update_fields=["escludi_tipi", "escludi_ext", "escludi_cartelle"])
    # cambia cosa conta come «non supportato» nella riga di stato
    _NON_SUPPORTATI_CACHE["scadenza"] = 0.0
    dopo = (config.estensioni_escluse(kb), config.cartelle_escluse(kb))
    return JsonResponse({**_filtro(kb), "cambiato": dopo != prima})


@require_GET
@api_login_required
def kb_files_view(request, slug):
    """Contenuto di una cartella della knowledge, per scegliere cosa eliminare.

    Una cartella alla volta (?dir=…): la posta ne ha una con migliaia di email,
    e l'albero intero non servirebbe a nessuno."""
    kb = KnowledgeBase.objects.visibili(request.user).filter(slug=slug).first()
    if kb is None:
        return JsonResponse({"detail": "Knowledge non trovata"}, status=404)
    root = kb.docs_dir.resolve()
    rel = request.GET.get("dir", "").strip().strip("/")
    cartella = (root / rel).resolve()
    if not cartella.is_relative_to(root):
        return JsonResponse({"detail": "Percorso non valido"}, status=400)
    if not cartella.is_dir():
        if rel:
            return JsonResponse({"detail": "Cartella non trovata"}, status=404)
        return JsonResponse({"dir": "", "voci": []})

    # Dei documenti indicizzati servono solo quelli sotto la cartella aperta, e
    # di quelli solo il primo pezzo di percorso: leggerli tutti e poi cercare
    # il prefisso voce per voce erano milioni di confronti per una pagina che
    # mostra una cartella sola.
    prefisso = (rel + "/") if rel else ""
    sotto = Document.objects.filter(knowledge=kb)
    if prefisso:
        sotto = sotto.filter(rel_path__startswith=prefisso)
    indicizzati_per_cartella: dict[str, int] = {}
    indicizzati_qui: set[str] = set()
    for rp in sotto.values_list("rel_path", flat=True).iterator(chunk_size=2000):
        testa, sep, _ = rp[len(prefisso):].partition("/")
        if sep:
            indicizzati_per_cartella[testa] = indicizzati_per_cartella.get(testa, 0) + 1
        else:
            indicizzati_qui.add(testa)

    voci = []
    for path in sorted(cartella.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if path.name.startswith("."):
            continue
        r = path.relative_to(root).as_posix()
        voce = {"nome": path.name, "rel": r, "cartella": path.is_dir()}
        if path.is_dir():
            voce["file"] = sum(1 for f in config.scorri(path, config.cartelle_escluse(kb)) if f.is_file())
            voce["indicizzati"] = indicizzati_per_cartella.get(path.name, 0)
        else:
            voce["byte"] = path.stat().st_size
            voce["indicizzato"] = path.name in indicizzati_qui
        voci.append(voce)
    return JsonResponse({"dir": cartella.relative_to(root).as_posix() if rel else "",
                         "voci": voci})


@require_POST
@api_login_required
def kb_elimina_view(request, slug):
    """Elimina file e cartelle dalla knowledge {paths: [...]}: indice, vettori e
    disco (i file finiscono nel Cestino, vedi elimina.py)."""
    kb = KnowledgeBase.objects.visibili(request.user).filter(slug=slug).first()
    if kb is None:
        return JsonResponse({"detail": "Knowledge non trovata"}, status=404)
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("JSON non valido")
    raw = body.get("paths") or []
    paths = [raw] if isinstance(raw, str) else [str(p) for p in raw]
    try:
        esito = elimina.elimina(kb, paths)
    except elimina.EliminaError as e:
        return JsonResponse({"detail": str(e)}, status=400)
    # la riga di stato conta i file fuori posto: dopo un'eliminazione va ricontato
    _FUORI_CACHE["scadenza"] = _NON_SUPPORTATI_CACHE["scadenza"] = 0.0
    return JsonResponse(esito)


def _reindex_stream(kb, prefix=None):
    """Avvia (o riprende) l'indicizzazione e ne trasmette l'avanzamento.

    Il lavoro vero gira in un thread (vedi jobs.py): questa risposta è solo una
    finestra su di esso. Se il browser se ne va, l'indicizzazione prosegue e la
    si ritrova ricaricando la pagina.

    prefix: evento opzionale da emettere per primo (es. esito dell'import)."""
    # se c'è già un lavoro in corso (anche su un'altra knowledge) questo si
    # mette in coda e parte da solo dopo: l'interfaccia lo vede "in coda"
    lavoro = jobs.avvia(kb, f"Indicizzazione di «{kb.name}»",
                        reindex_module.reindex_iter)
    return _stream_lavoro(lavoro, prefix)


def _righe_lavoro(lavoro, prefix=None):
    """Gli eventi di un lavoro in coda (vedi jobs.py), come righe NDJSON."""
    if prefix is not None:
        yield json.dumps(prefix) + "\n"
    yield json.dumps({"type": "job", "id": lavoro.id, "kb": lavoro.kb_slug,
                      "stato": lavoro.stato}) + "\n"
    for ev in jobs.segui(lavoro):
        yield json.dumps(ev) + "\n"
    if lavoro.stato == "errore":
        yield json.dumps({"error": lavoro.errore}) + "\n"


def _stream_lavoro(lavoro, prefix=None):
    """Trasmette in NDJSON gli eventi di un lavoro in coda (vedi jobs.py)."""
    return StreamingHttpResponse(_righe_lavoro(lavoro, prefix), content_type="application/x-ndjson")


@require_GET
@api_login_required
def reindex_state_view(request):
    """Stato dell'indicizzazione in corso, per riattaccarsi dopo un reload."""
    return JsonResponse(lavoro_per(request.user))


@require_POST
@api_login_required
def lavoro_ferma_view(request, lavoro_id):
    """Ferma un lavoro della coda (vedi jobs.ferma): quello in coda non parte,
    quello in corso si ferma dopo il documento che sta leggendo. Quel che è
    già indicizzato resta; l'indicizzazione automatica della knowledge si
    sospende finché non la si riavvia a mano.

    Può fermarlo chi vede la knowledge del lavoro: è lo stesso utente che può
    avviarlo dal pulsante Reindicizza."""
    lavoro = next((l for l in jobs.attivi() if l.id == lavoro_id), None)
    if lavoro is None or not KnowledgeBase.objects.visibili(request.user).filter(
            slug=lavoro.kb_slug).exists():
        return JsonResponse({"detail": "Nessun lavoro in corso con questo id"}, status=404)
    if jobs.ferma(lavoro_id) is None:
        # finito proprio adesso, fra la ricerca e lo stop
        return JsonResponse({"detail": "Il lavoro è già finito"}, status=409)
    return JsonResponse(lavoro.riassunto())


@require_POST
@api_login_required
def reindex_view(request):
    """Reindicizza in streaming: emette righe NDJSON di avanzamento
    (start/progress/done) così il client può mostrare una barra."""
    oll = ollama.ollama_available()
    if not oll["ok"]:
        return JsonResponse({"detail": ollama.non_raggiungibile()}, status=503)
    kb = kb_from_request(request)
    if kb is None:
        return JsonResponse({"detail": "Nessuna knowledge disponibile"}, status=400)
    return _reindex_stream(kb)


@require_POST
@api_login_required
def temi_view(request):
    """Rifà i temi della knowledge, con i nomi scelti dal modello.

    Non reindicizza niente: i gruppi nascono dai vettori già salvati, e resta
    solo da dare un nome a ciascuno. Passa dalla stessa coda dei lavori della
    reindicizzazione, con lo stesso flusso NDJSON di avanzamento."""
    if not ollama.ollama_available()["ok"]:
        return JsonResponse({"detail": "Serve il modello per dare un nome ai temi. "
                                       + ollama.non_raggiungibile()}, status=503)
    kb = kb_from_request(request)
    if kb is None:
        return JsonResponse({"detail": "Nessuna knowledge disponibile"}, status=400)
    return _stream_lavoro(jobs.avvia(kb, f"Temi di «{kb.name}»", temi.raggruppa_iter))
