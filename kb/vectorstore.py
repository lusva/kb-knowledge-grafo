"""Database vettoriale (Qdrant): embedded nei file di data/qdrant, oppure un server.

Ogni knowledge ha la propria collection ``kb_<slug>`` (vedi
``KnowledgeBase.collection``), così i vettori restano isolati per knowledge.

Con ``KB_QDRANT_URL`` vuoto Qdrant gira dentro il processo. Quel client non
regge più thread insieme, e qui lo usano la ricerca (thread delle richieste),
l'indicizzazione (thread dei lavori) e l'aggiornamento della posta (thread
proprio): tutte le chiamate passano quindi da un unico lock, e una ricerca
aspetta che l'indicizzazione finisca di scrivere un lotto.

Con un server il client parla HTTP e più thread lo usano insieme: il lock non
serve e non si prende (``_accesso``). Il calcolo degli embeddings, che è la
parte lenta, resta comunque fuori.
"""
import atexit
import contextlib
import math
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path

from django.conf import settings
from qdrant_client import QdrantClient
from qdrant_client.local.qdrant_local import QdrantLocal
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    Range,
    VectorParams,
)

from . import config, ollama

_client = None
_lock = threading.RLock()


def in_locale() -> bool:
    """True se Qdrant è quello embedded nei file di data/qdrant."""
    return not settings.KB_QDRANT_URL


def _accesso():
    """Il lock attorno alle chiamate, solo dove serve: al client locale."""
    return _lock if in_locale() else contextlib.nullcontext()


def get_client() -> QdrantClient:
    global _client
    with _lock:
        if _client is None:
            if in_locale():
                _client = QdrantClient(path=str(settings.KB_DATA_DIR / "qdrant"))
            else:
                _client = QdrantClient(url=settings.KB_QDRANT_URL,
                                       api_key=settings.KB_QDRANT_API_KEY or None, timeout=60)
            # chiuso in modo ordinato all'uscita: lasciato al garbage collector
            # finale, il client stampa un traceback innocuo ("Python is likely
            # shutting down") alla fine di ogni comando di manutenzione
            atexit.register(_chiudi)
        return _client


def _chiudi():
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:          # noqa: BLE001 — stiamo uscendo comunque
                pass
            _client = None


def usa_copia() -> Path:
    """Copia i vettori di data/qdrant in una cartella temporanea e da qui in
    poi cerca lì. Solo per leggere: ciò che si scrive nella copia si perde.

    Serve alle misure (``evalrag``) con il server acceso, che tiene il lock
    del Qdrant embedded. I file ``storage.sqlite`` si copiano con il backup di
    SQLite, che dà una fotografia coerente anche mentre il server scrive; la
    cartella sparisce all'uscita del processo."""
    global _client
    origine = settings.KB_DATA_DIR / "qdrant"
    copia = Path(tempfile.mkdtemp(prefix="kb-qdrant-"))
    atexit.register(shutil.rmtree, copia, ignore_errors=True)
    for file in origine.rglob("*"):
        if file.is_dir() or file.name == ".lock":
            continue
        destinazione = copia / file.relative_to(origine)
        destinazione.parent.mkdir(parents=True, exist_ok=True)
        if file.suffix == ".sqlite":
            sorgente = sqlite3.connect(f"{file.as_uri()}?mode=ro", uri=True)
            copiato = sqlite3.connect(destinazione)
            try:
                sorgente.backup(copiato)
            finally:
                copiato.close()
                sorgente.close()
        else:
            shutil.copy2(file, destinazione)
    with _lock:
        _chiudi()
        _client = QdrantClient(path=str(copia))
        # registrato dopo la cancellazione della cartella, quindi eseguito prima
        atexit.register(_chiudi)
    return copia


# Campi del payload su cui si filtra: il documento di provenienza, la famiglia
# (email/documento/…) e la data del contenuto come timestamp intero.
INDICI = {
    "doc": PayloadSchemaType.KEYWORD,
    "kind": PayloadSchemaType.KEYWORD,
    "date": PayloadSchemaType.INTEGER,
}


# Collection su cui gli indici di payload sono già stati richiesti in questo
# processo: in modalità locale non servono (Qdrant filtra scorrendo i punti) e
# richiederli a ogni ricerca stampa un avviso per ogni campo.
_indicizzate = set()


def _indici(client, kb):
    if kb.collection in _indicizzate:
        return
    _indicizzate.add(kb.collection)
    if isinstance(client._client, QdrantLocal):
        return                 # in locale non hanno effetto: inutile chiederli
    for campo, schema in INDICI.items():
        try:
            client.create_payload_index(kb.collection, field_name=campo, field_schema=schema)
        except Exception:      # noqa: BLE001 — indice già presente: va benissimo
            pass


def ensure_collection(kb):
    with _accesso():
        client = get_client()
        if not client.collection_exists(kb.collection):
            client.create_collection(
                kb.collection,
                vectors_config=VectorParams(size=config.get("embed_dim"), distance=Distance.COSINE),
            )
        _indici(client, kb)


def reset_if_dim_changed(kb) -> bool:
    """Se la dimensione configurata degli embeddings non corrisponde più alla
    collection della knowledge (cambio di modello), ricrea la collection da
    zero. Ritorna True se l'indice è stato azzerato."""
    with _accesso():
        client = get_client()
        if not client.collection_exists(kb.collection):
            return False
        size = client.get_collection(kb.collection).config.params.vectors.size
        if size == config.get("embed_dim"):
            return False
        client.delete_collection(kb.collection)
        ensure_collection(kb)
        return True


def payload_documento(doc) -> dict:
    """Parte di payload comune a tutti i chunk di un documento (per i filtri)."""
    return {
        "doc": doc.rel_path,
        "kind": doc.kind or "documento",
        "date": int(doc.doc_date.timestamp()) if doc.doc_date else 0,
    }


def upsert_chunks(kb, chunk_objs: list, embeddings: list[list[float]], doc):
    """Inserisce/aggiorna i vettori dei chunk. chunk_objs devono già avere un pk (salvati)."""
    comune = payload_documento(doc)
    points = [
        PointStruct(id=c.pk, vector=vec, payload={**comune, "chunk": c.index})
        # strict: un vettore in meno non deve lasciare un chunk senza vettore in silenzio
        for c, vec in zip(chunk_objs, embeddings, strict=True)
    ]
    if not points:
        return
    with _accesso():
        ensure_collection(kb)
        get_client().upsert(kb.collection, points=points)


def aggiorna_payload(kb, doc) -> int:
    """Riscrive i metadati nel payload dei punti di un documento già indicizzato.

    Serve a portare i metadati sui documenti indicizzati prima che esistessero,
    senza ricalcolare gli embeddings (che è la parte cara)."""
    # i punti si indirizzano per id (sono le chiavi dei Chunk): in modalità
    # locale un filtro costringerebbe Qdrant a scorrere l'intera collection per
    # ogni documento, mentre gli id li abbiamo già nel database
    ids = list(doc.chunks.values_list("pk", flat=True))
    if not ids:
        return 0
    with _accesso():
        ensure_collection(kb)
        get_client().set_payload(kb.collection, payload=payload_documento(doc), points=ids)
    return len(ids)


def delete_points(kb, ids: list[int]):
    """Toglie i punti con gli id dati (le chiavi dei Chunk)."""
    ids = [int(i) for i in ids]
    if not ids:
        return
    with _accesso():
        ensure_collection(kb)
        get_client().delete(kb.collection, points_selector=ids)


def delete_doc(kb, doc):
    """Toglie dall'indice i vettori di un documento.

    ``doc`` è un Document: i suoi chunk danno gli id dei punti, e la
    cancellazione è diretta. Con un solo percorso (stringa), o quando i chunk
    non ci sono più, si ricade sul filtro per nome del documento, che in
    modalità locale costa una scansione dell'intera collection: va bene come
    ripiego, non come strada normale."""
    ids = []
    rel_path = doc if isinstance(doc, str) else doc.rel_path
    if not isinstance(doc, str):
        ids = list(doc.chunks.values_list("pk", flat=True))
    if ids:
        delete_points(kb, ids)
        return
    with _accesso():
        ensure_collection(kb)
        get_client().delete(
            kb.collection,
            points_selector=Filter(must=[FieldCondition(key="doc", match=MatchValue(value=rel_path))]),
        )


def delete_collection(kb):
    """Rimuove completamente la collection di una knowledge (all'eliminazione)."""
    with _accesso():
        client = get_client()
        if client.collection_exists(kb.collection):
            client.delete_collection(kb.collection)


def condizioni(filtri: dict | None) -> Filter | None:
    """Traduce i filtri dell'interfaccia in un filtro Qdrant. None = nessuno."""
    if not filtri:
        return None
    must = []
    if filtri.get("tipi"):
        must.append(FieldCondition(key="kind", match=MatchAny(any=list(filtri["tipi"]))))
    if "docs" in filtri:
        must.append(FieldCondition(key="doc", match=MatchAny(any=list(filtri["docs"]))))
    da, a = filtri.get("da"), filtri.get("a")
    if da or a:
        # i documenti senza data hanno timestamp 0: un filtro di periodo li
        # esclude, ed è quello che ci si aspetta chiedendo "ultimo anno"
        must.append(FieldCondition(key="date", range=Range(gte=da or None, lte=a or None)))
    return Filter(must=must) if must else None


def search_ids(kb, query: str, limit: int, filtri: dict | None = None) -> list[tuple[int, float]]:
    """Chunk semanticamente più vicini alla domanda, dentro la knowledge ``kb``.

    Ritorna coppie (id del chunk, similarità coseno) dal più vicino. La parte
    letterale della ricerca sta in ``lexical.py``; a fondere le due classifiche
    è ``retrieval.py``, che è il punto d'ingresso da usare."""
    with _accesso():
        ensure_collection(kb)
        if get_client().count(kb.collection).count == 0:
            return []
    qemb = ollama.embed_texts([query], query=True)[0]      # fuori dal lock: è lento
    with _accesso():
        hits = get_client().query_points(kb.collection, query=qemb, limit=limit, with_payload=False,
                                         query_filter=condizioni(filtri)).points
    return [(int(h.id), float(h.score)) for h in hits]


def collection_info(kb) -> dict:
    """Stato della collection Qdrant di una knowledge (per /api/status)."""
    with _accesso():
        client = get_client()
        if not client.collection_exists(kb.collection):
            return {"esiste": False}
        info = client.get_collection(kb.collection)
    vp = info.config.params.vectors
    return {
        "esiste": True,
        "punti": info.points_count,
        "vettori_indicizzati": info.indexed_vectors_count,
        "dimensione": vp.size,
        "distanza": str(vp.distance),
        "status": str(info.status),
        "percorso": str(settings.KB_DATA_DIR / "qdrant") if in_locale() else settings.KB_QDRANT_URL,
    }


def mean(vectors: list[list[float]]) -> list[float]:
    """Vettore medio di un documento. Con numpy, perché in Python puro un
    documento da millecinquecento chunk costa oltre un milione di somme."""
    import numpy as np

    return np.asarray(vectors, dtype=np.float32).mean(axis=0).tolist()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0
