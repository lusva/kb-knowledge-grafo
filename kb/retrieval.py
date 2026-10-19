"""Recupero dei passaggi: punto d'ingresso unico della ricerca.

Due motori guardano la stessa domanda da due lati diversi:

* ``vectorstore``  — vicinanza di *significato* (embeddings). Trova la risposta
  anche quando è scritta con parole diverse da quelle della domanda, ma non
  vede i termini letterali: un numero di fattura o una sigla, per un modello di
  embedding, sono rumore.
* ``lexical``      — presenza delle *parole* (BM25 su FTS5). L'esatto opposto:
  infallibile sui termini rari, cieco sui sinonimi.

Le due classifiche si fondono con RRF (Reciprocal Rank Fusion): ogni motore
contribuisce ``1 / (K + posizione)``, quindi conta *dove* un chunk si piazza,
non il punteggio grezzo — che fra coseno e BM25 non sarebbe confrontabile. Un
passaggio trovato da entrambi sale in cima; uno trovato da uno solo entra
comunque se è nelle primissime posizioni. È la fusione usata di norma nei
sistemi ibridi perché non richiede né taratura né normalizzazione dei punteggi.

Dopo la fusione, un tetto ai chunk per documento: sei passaggi presi tutti
dalla stessa email lunga dicono una cosa sola, sei passaggi da tre o quattro
documenti danno al modello più da cui scegliere.
"""
from datetime import datetime, timezone as tz

from . import config, lexical, ollama, vectorstore

# Costante classica di RRF: smorza il vantaggio delle prime posizioni, così un
# secondo posto in entrambe le liste batte un primo posto in una sola.
RRF_K = 60

# Quanti documenti elencare per una domanda su un periodo. Più alto del top_k
# normale: chi chiede "cosa è arrivato oggi" vuole l'elenco, non un campione.
MAX_ELENCO = 20


def fondi(densi: list[tuple[int, float]], lessicali: list[tuple[int, float]]):
    """Fusione RRF delle due classifiche.

    Ritorna (punteggi per chunk, coseno per chunk, motori che l'hanno trovato)."""
    punteggi: dict[int, float] = {}
    coseno: dict[int, float] = {}
    origini: dict[int, set] = {}
    for posizione, (cid, score) in enumerate(densi):
        punteggi[cid] = punteggi.get(cid, 0.0) + 1.0 / (RRF_K + posizione + 1)
        coseno[cid] = score
        origini.setdefault(cid, set()).add("vettori")
    for posizione, (cid, _bm25) in enumerate(lessicali):
        punteggi[cid] = punteggi.get(cid, 0.0) + 1.0 / (RRF_K + posizione + 1)
        origini.setdefault(cid, set()).add("testo")
    return punteggi, coseno, origini


def scegli(ordinati: list[int], documento_di: dict[int, int], top_k: int,
           per_doc: int) -> list[int]:
    """I primi ``top_k`` chunk nell'ordine dato, con al più ``per_doc`` chunk
    per documento (0 = nessun limite). Un chunk di cui non si conosce il
    documento (cancellato nel frattempo) viene saltato."""
    scelti, conteggio = [], {}
    for cid in ordinati:
        did = documento_di.get(cid)
        if did is None:
            continue
        if per_doc and conteggio.get(did, 0) >= per_doc:
            continue
        conteggio[did] = conteggio.get(did, 0) + 1
        scelti.append(cid)
        if len(scelti) >= top_k:
            break
    return scelti


def search(kb, query: str, top_k: int = None, filtri: dict | None = None,
           per_doc: int | None = None) -> list[dict]:
    """I ``top_k`` passaggi più pertinenti della knowledge ``kb``.

    ``filtri`` restringe la ricerca per tipo di documento, periodo o elenco di
    documenti (vedi ``filtri.py``); vale per entrambi i motori. ``per_doc``
    sostituisce il tetto di passaggi per documento della configurazione: con 1
    si ottengono ``top_k`` documenti diversi (è ciò che serve ai grafici).

    Ogni risultato è {text, doc, chunk, score, origine, rrf}, dove ``score`` è
    la similarità coseno (assente se il chunk arriva solo dal full-text) e
    ``origine`` dice quale motore l'ha trovato."""
    from .models import Chunk

    if filtri is not None and "docs" in filtri and not filtri["docs"]:
        return []

    # `is None` e non `or`: uno zero esplicito vuol dire "nessun passaggio",
    # e ricadere sul valore di configurazione ne restituirebbe sei
    top_k = config.get("top_k") if top_k is None else top_k
    # ogni motore propone più candidati di quanti ne servano: la fusione ha
    # senso solo se le due liste possono sovrapporsi
    per_doc = config.get("chunks_per_doc") if per_doc is None else per_doc
    # con pochi passaggi per documento ne servono di più per coprire top_k documenti
    fetch = max(config.get("fetch_k"), top_k * (3 if per_doc == 1 else 1))

    if (filtri or {}).get("recenti"):
        # domanda su un periodo: contano le date, non la somiglianza
        return _per_data(kb, top_k, filtri)

    lessicali = lexical.search_ids(kb, query, fetch, filtri) if config.get("hybrid") else []
    try:
        densi = vectorstore.search_ids(kb, query, fetch, filtri)
    except ollama.OllamaError:
        if not (config.get("hybrid") and lexical.disponibile()):
            raise
        densi = []

    punteggi, coseno, origini = fondi(densi, lessicali)
    if not punteggi:
        return []

    ordinati = sorted(punteggi, key=punteggi.get, reverse=True)
    documento_di = dict(Chunk.objects.filter(pk__in=ordinati).values_list("pk", "document_id"))
    migliori = scegli(ordinati, documento_di, top_k, per_doc)
    chunks = {c.pk: c for c in
              Chunk.objects.filter(pk__in=migliori).select_related("document")}

    risultati = []
    for cid in migliori:
        c = chunks.get(cid)
        if c is None:          # chunk cancellato fra ricerca e lettura
            continue
        trovato = origini.get(cid, set())
        risultati.append({
            "text": c.text,
            "doc": c.document.rel_path,
            "titolo": c.document.title,
            "tipo": c.document.kind,
            "data": _data(c.document),
            "chunk": c.index,
            "score": round(coseno[cid], 3) if cid in coseno else None,
            "origine": "entrambi" if len(trovato) > 1 else next(iter(trovato)),
            "rrf": round(punteggi[cid], 5),
        })
    return risultati


def _data(documento) -> str:
    """Data del documento in forma leggibile, per le citazioni."""
    if not documento.doc_date:
        return ""
    from django.utils import timezone as tzone
    return tzone.localtime(documento.doc_date).strftime("%d/%m/%Y %H:%M")


def _per_data(kb, top_k: int, filtri: dict) -> list[dict]:
    """I documenti del periodo richiesto, dal più recente.

    Una domanda come "cosa è arrivato oggi?" vuole un *elenco*, non il
    passaggio più pertinente: si prende l'inizio di ogni documento (per una
    email è l'intestazione con mittente, data e oggetto) e si scende nel tempo.
    Il limite è più alto del solito perché un elenco con sei voci quando i
    documenti sono venti sarebbe fuorviante."""
    from .models import Chunk, Document

    documenti = Document.objects.filter(knowledge=kb)
    if filtri.get("da"):
        documenti = documenti.filter(doc_date__gte=datetime.fromtimestamp(filtri["da"], tz.utc))
    if filtri.get("a"):
        documenti = documenti.filter(doc_date__lte=datetime.fromtimestamp(filtri["a"], tz.utc))
    if filtri.get("tipi"):
        documenti = documenti.filter(kind__in=filtri["tipi"])
    if filtri.get("docs"):
        from .filtri import docs_in
        documenti = documenti.filter(rel_path__in=docs_in(filtri["docs"]))

    totale = documenti.count()
    # nel futuro (agenda) si parte dal primo impegno, nel passato dal più recente
    ordine = "doc_date" if filtri.get("crescente") else "-doc_date"
    documenti = list(documenti.order_by(ordine)[:max(top_k, MAX_ELENCO)])
    if not documenti:
        return []
    primi = {c.document_id: c for c in
             Chunk.objects.filter(document__in=documenti, index=0).select_related("document")}
    return [{
        "text": primi[d.pk].text,
        "doc": d.rel_path,
        "titolo": d.title,
        "tipo": d.kind,
        "data": _data(d),
        "chunk": 0,
        "score": None,
        "origine": "periodo",
        "rrf": 0.0,
        # quanti sono in tutto: serve a non far dire "sono arrivate 20 email"
        # quando ne sono arrivate 63 e ne stiamo mostrando 20
        "totale_periodo": totale,
    } for d in documenti if d.pk in primi]
