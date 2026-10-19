"""Scansione della cartella di una knowledge e (ri)indicizzazione incrementale."""
import hashlib
import threading
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import config, eml, ingest, metadata, ollama, temi, vectorstore
from .models import Chunk, Document

_lock = threading.Lock()


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# La cartella nell'intestazione dei chunk
#
# «IT01879020517A2026_fvVMd.xml.p7m - FPR 7_26» non dice niente; la cartella
# in cui l'utente l'ha messo, «Documenti/Fatture/fatture attive», sì. Senza,
# «tabella delle fatture attive» (tutte le parole nello stesso chunk) non
# trovava nessun documento. Solo per documenti e immagini fuori dalle
# cartelle dei connettori: email, eventi e righe di database hanno già
# un'intestazione che dice cosa sono, e «gmail-mario/allegati» non aggiunge
# niente.
# --------------------------------------------------------------------------- #

CON_CARTELLA = (metadata.DOCUMENTO, metadata.IMMAGINE)


def cartella_del_documento(rel: str, tipo: str) -> str:
    """La cartella da scrivere nell'intestazione dei chunk, o ""."""
    cartella = rel.rpartition("/")[0]
    if not cartella or tipo not in CON_CARTELLA:
        return ""
    return cartella


def senza_cartella(kb) -> dict[str, str]:
    """{percorso: cartella} dei documenti già indicizzati la cui intestazione
    non ha ancora la cartella. La passata li riscrive (``_aggiungi_cartella``)
    anche se il file non è cambiato, e autoindex la fa partire da sé."""
    from django.db.models.functions import Substr

    if not config.get("chunk_header"):
        return {}
    attese = {}
    for pk, rel, tipo in (Document.objects.filter(knowledge=kb, kind__in=CON_CARTELLA,
                                                  rel_path__contains="/")
                          .values_list("pk", "rel_path", "kind")):
        cartella = cartella_del_documento(rel, tipo)
        if cartella:
            attese[pk] = (rel, cartella)
    if not attese:
        return {}
    prime = (Chunk.objects.filter(document__knowledge=kb, document__kind__in=CON_CARTELLA,
                                  document__rel_path__contains="/", index=0)
             .annotate(inizio=Substr("text", 1, ingest.MAX_INTESTAZIONE + 10))
             .values_list("document_id", "inizio"))
    vecchie = {}
    for pk, inizio in prime:
        if pk not in attese:
            continue
        rel, cartella = attese[pk]
        riga = inizio.partition("\n")[0]
        # senza intestazione (indicizzato con chunk_header spento) si lascia com'è
        if ingest.e_intestazione(riga) and not ingest.ha_cartella(riga, cartella):
            vecchie[rel] = cartella
    return vecchie


def _aggiungi_cartella(kb, doc, cartella: str):
    """Riscrive l'intestazione dei chunk di ``doc`` con la cartella e ne rifà
    i vettori, senza rileggere il file: niente OCR né descrizione delle
    immagini, che per cambiare una riga costerebbero minuti. I vettori prima
    del testo: se il salvataggio si interrompe, l'intestazione resta vecchia
    e la passata dopo ci riprova."""
    chunks = list(doc.chunks.order_by("index"))
    for c in chunks:
        riga, a_capo, resto = c.text.partition("\n")
        if ingest.e_intestazione(riga):
            c.text = ingest.con_cartella(riga, cartella) + a_capo + resto
    if not chunks:
        return
    embeddings = ollama.embed_texts([c.text for c in chunks])
    vectorstore.upsert_chunks(kb, chunks, embeddings, doc)
    with transaction.atomic():
        Chunk.objects.bulk_update(chunks, ["text"], batch_size=500)
        doc.embedding = vectorstore.mean(embeddings)
        doc.save(update_fields=["embedding"])


def reindex_iter(kb, incrementale: bool = False):
    """Come reindex(), ma generatore: emette eventi di avanzamento e, per
    ultimo, un evento con le statistiche finali. Gli eventi sono dizionari:

    ``incrementale``: si fida della data di modifica del file invece di
    ricalcolarne l'impronta. Un file già indicizzato e non toccato dall'ultima
    indicizzazione viene saltato senza nemmeno leggerlo — indispensabile per
    l'aggiornamento automatico della posta, che gira di continuo e non può
    rileggere l'intero archivio ogni volta. La verifica completa (impronta di
    ogni file) resta quella del pulsante Reindicizza.

      {"type": "start",    "total": N}
      {"type": "progress", "done": i, "total": N, "file": rel, "action": …}
      {"type": "done",     "stats": {...}}

    ``action`` ∈ aggiunto | aggiornato | invariato | rimosso | errore.
    """
    with _lock:
        stats = {"aggiunti": 0, "aggiornati": 0, "rimossi": 0, "invariati": 0, "errori": []}
        docs_dir = kb.docs_dir
        docs_dir.mkdir(parents=True, exist_ok=True)

        # cambio di modello/dimensione embeddings: l'indice viene azzerato e
        # tutti i documenti riprocessati (gli hash azzerati li marcano "modificati")
        if vectorstore.reset_if_dim_changed(kb):
            Document.objects.filter(knowledge=kb).update(file_hash="")

        # estensioni tolte dall'utente (impostazione «Estensioni escluse» e
        # filtro della knowledge): un file già indicizzato che non viene più
        # trovato esce dall'indice
        escluse = config.estensioni_escluse(kb)
        # e le cartelle (node_modules…) in cui non si entra
        cartelle = config.cartelle_escluse(kb)

        # Gli allegati dei messaggi EML locali entrano nella stessa knowledge.
        for path in list(config.scorri(docs_dir, cartelle)):
            if path.suffix.lower() in settings.KB_EML_EXT and path.suffix.lower() not in escluse:
                try:
                    eml.explode_attachments(path)
                except Exception as e:
                    stats["errori"].append(f"{path.name} (allegati): {e}")

        found = file_indicizzabili(docs_dir, escluse, cartelle)
        # prima i file economici (testo, email, office), poi i PDF (che possono
        # finire in OCR: minuti a pagina), per ultime le immagini (descrizione
        # con il modello di visione): così il grosso dell'archivio è cercabile
        # presto, e un documento lento non tiene in ostaggio tutti gli altri
        found = dict(sorted(found.items(), key=lambda kv: (_costo(kv[1]), kv[0])))

        existing = {d.rel_path: d for d in Document.objects.filter(knowledge=kb)}

        # file cancellati: i vettori si tolgono per id dei chunk, che è una
        # cancellazione diretta (per nome sarebbe una scansione dell'indice)
        for rel, doc in list(existing.items()):
            if rel not in found:
                vectorstore.delete_doc(kb, doc)
                doc.delete()
                del existing[rel]
                stats["rimossi"] += 1

        total = len(found)
        yield {"type": "start", "total": total}

        con_intestazione = bool(config.get("chunk_header"))
        da_completare = senza_cartella(kb)

        # file nuovi / modificati
        done = 0
        for rel, path in found.items():
            done += 1
            action = "invariato"
            try:
                doc = existing.get(rel)
                if not path.exists():
                    # eliminato mentre l'indicizzazione era in corso (vedi
                    # elimina.py): l'indice l'ha già ripulito chi l'ha tolto
                    yield {"type": "progress", "done": done, "total": total,
                           "file": rel, "action": "rimosso"}
                    continue
                invariato = bool(incrementale and doc and _immutato(path, doc))
                if not invariato:
                    fhash = _file_hash(path)
                    invariato = bool(doc and doc.file_hash == fhash)
                    if invariato and incrementale:
                        # file toccato ma identico (salvato senza modifiche):
                        # si segna come verificato, altrimenti la data lo
                        # farebbe sembrare cambiato a ogni controllo automatico
                        Document.objects.filter(pk=doc.pk).update(indexed_at=timezone.now())
                if invariato:
                    if rel in da_completare:
                        _aggiungi_cartella(kb, doc, da_completare[rel])
                        action = "aggiornato"
                        stats["aggiornati"] += 1
                    else:
                        stats["invariati"] += 1
                    yield {"type": "progress", "done": done, "total": total,
                           "file": rel, "action": action}
                    continue
                is_update = doc is not None

                ext = path.suffix.lower()
                if ext in settings.KB_IMAGE_EXT:
                    desc = ollama.describe_image(path)
                    text = desc or f"Immagine: {path.stem}"
                    links, tags = [], []
                else:
                    # le tavole di un PDF (da A3 in su) le descrive il modello
                    # dei disegni: il loro livello di testo sono quote sparse
                    text = ""
                    if ext in settings.KB_PDF_EXT and config.get("disegni_indice"):
                        text = ingest.descrivi_disegni_pdf(path, settings.KB_OCR_MAX_PAGES)
                    text = text or ingest.extract_text(path)
                    links = ingest.extract_wikilinks(text) if ext == ".md" else []
                    tags = ingest.extract_tags(text) if ext in settings.KB_TEXT_EXT else []

                meta = metadata.estrai(path, ext, text)
                titolo = (meta.get("titolo") or path.stem)[:255]

                chunks_text = ingest.chunk_documento(text)
                if not chunks_text:
                    chunks_text = [f"(documento vuoto: {rel})"]
                if con_intestazione:
                    # la data va nell'intestazione solo se è quella del contenuto
                    data = meta["data"] if meta.get("fonte_data") == "email" else None
                    chunks_text = ingest.con_intestazione(
                        chunks_text,
                        ingest.intestazione_chunk(titolo, meta["autore"], data, meta["tipo"],
                                                  cartella_del_documento(rel, meta["tipo"])))
                embeddings = ollama.embed_texts(chunks_text)
                if not path.exists():
                    # eliminato mentre lo leggevamo (un PDF in OCR ci mette
                    # minuti): salvarlo adesso lo farebbe tornare come fantasma
                    yield {"type": "progress", "done": done, "total": total,
                           "file": rel, "action": "rimosso"}
                    continue

                with transaction.atomic():
                    if is_update:
                        vectorstore.delete_doc(kb, doc)      # per id, prima di perdere i chunk
                        doc.chunks.all().delete()
                    else:
                        doc = Document(knowledge=kb, rel_path=rel)

                    doc.title = titolo
                    doc.ext = ext
                    doc.doc_date = meta["data"]
                    doc.author = meta["autore"]
                    doc.kind = meta["tipo"]
                    doc.people = meta["persone"]
                    doc.file_hash = fhash
                    doc.chunk_count = len(chunks_text)
                    doc.links = links
                    doc.tags = tags
                    doc.embedding = vectorstore.mean(embeddings)
                    doc.preview = text[:800]
                    doc.save()

                    # Un INSERT per chunk significa, su un PDF da 300 pagine,
                    # millecinquecento scritture più altrettanti scatti del
                    # trigger full-text, tutte dentro la stessa transazione.
                    chunk_objs = Chunk.objects.bulk_create(
                        [Chunk(document=doc, index=i, text=t)
                         for i, t in enumerate(chunks_text)],
                        batch_size=500)

                # I vettori si scrivono fuori dalla transazione. Dentro,
                # l'indicizzazione terrebbe insieme il lock del database e
                # quello di Qdrant, mentre un'eliminazione dall'interfaccia li
                # prende nell'ordine opposto: i due si aspettano a vicenda
                # finché SQLite non si arrende, e l'eliminazione resta a metà.
                try:
                    vectorstore.upsert_chunks(kb, chunk_objs, embeddings, doc)
                except Exception:
                    # Senza vettori il documento non è cercabile, e la passata
                    # successiva lo salterebbe perché l'impronta combacia:
                    # meglio toglierlo e rifarlo da capo la prossima volta.
                    doc.delete()
                    existing.pop(rel, None)
                    raise

                existing[rel] = doc
                action = "aggiornato" if is_update else "aggiunto"
                stats["aggiornati" if is_update else "aggiunti"] += 1
            except Exception as e:
                action = "errore"
                stats["errori"].append(f"{rel}: {e}")
            yield {"type": "progress", "done": done, "total": total,
                   "file": rel, "action": action}

        # i temi si tengono al passo qui, dove passa ogni indicizzazione da
        # qualunque parte sia stata avviata (pulsante, nota, arrivo di posta)
        if stats["aggiunti"] or stats["aggiornati"] or stats["rimossi"]:
            try:
                stats["temi"] = temi.dopo_indicizzazione(kb)
            except Exception as e:                       # noqa: BLE001
                stats["errori"].append(f"temi: {e}")

        yield {"type": "done", "stats": stats}


def file_indicizzabili(docs_dir: Path, escluse: set[str],
                       cartelle: set[str]) -> dict[str, Path]:
    """{percorso relativo: file} di quel che l'indicizzazione prenderebbe:
    estensioni lette e non escluse, niente file o cartelle nascosti, niente
    cartelle escluse per nome (``config.cartelle_escluse``).

    """
    found = {}
    for path in config.scorri(docs_dir, cartelle):
        ext = path.suffix.lower()
        if ext not in settings.KB_ALL_EXT or ext in escluse or not path.is_file():
            continue
        rel = path.relative_to(docs_dir)
        found[str(rel)] = path
    return found


def _costo(path: Path) -> int:
    """Ordine di lavorazione: 0 testo e simili, 1 archivi di posta, 2 PDF, 3 immagini."""
    ext = path.suffix.lower()
    if ext in settings.KB_IMAGE_EXT:
        return 3
    if ext in settings.KB_PDF_EXT:
        return 2
    return 0


def _immutato(path: Path, doc) -> bool:
    """True se il file non è stato toccato dopo la sua ultima indicizzazione.

    È un controllo di sola data (una stat, nessuna lettura): vale come indizio,
    non come prova — un file riscritto con la stessa data sfuggirebbe. Per
    questo è il criterio dell'aggiornamento frequente, mentre la
    reindicizzazione completa continua a confrontare le impronte."""
    try:
        return path.stat().st_mtime <= doc.indexed_at.timestamp()
    except (OSError, AttributeError):
        return False


def reindex(kb, incrementale: bool = False) -> dict:
    """Scansiona la cartella della knowledge ``kb`` (documents/<slug>/) e
    (ri)indicizza i file nuovi o modificati. Rimuove dall'indice i file
    cancellati. Ritorna le statistiche finali (versione non-streaming)."""
    stats = {"aggiunti": 0, "aggiornati": 0, "rimossi": 0, "invariati": 0, "errori": []}
    for ev in reindex_iter(kb, incrementale=incrementale):
        if ev["type"] == "done":
            stats = ev["stats"]
    return stats
