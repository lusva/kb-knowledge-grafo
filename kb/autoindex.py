"""Aggiornamento automatico dei file locali nelle knowledge. Una scansione periodica mette in coda i documenti nuovi o modificati."""
import logging
import threading
import time

from django.db import close_old_connections
from django.utils import timezone

log = logging.getLogger("kb.autoindex")

stato = {"attivo": False, "ultimo": None, "esito": ""}

_thread = None
_avvio = threading.Lock()
ATTESA_INIZIALE = 45      # secondi: prima si lascia partire il server
ASSESTAMENTO = 60         # secondi di quiete prima di leggere un file nuovo

# {slug: {percorso relativo: data di modifica}} dei file andati in errore
_falliti: dict[str, dict[str, float]] = {}
_falliti_lock = threading.Lock()


def _date(path) -> tuple[float, float] | None:
    """(modifica, ultimo tocco) del file. Il Finder e ``cp -p`` rimettono la
    data di modifica originale a copia finita: l'ultimo tocco (ctime) dice
    invece quando il file è stato scritto davvero."""
    try:
        st = path.stat()
    except OSError:
        return None                                  # sparito durante la scansione
    return st.st_mtime, max(st.st_mtime, st.st_ctime)


def _mtime(path) -> float | None:
    date = _date(path)
    return date[0] if date else None


def cambiamenti(kb, adesso: float | None = None) -> dict:
    """Cosa è cambiato su disco rispetto all'indice della knowledge:
    {"nuovi", "modificati", "rimossi", "in_copia"} (conteggi)."""
    from . import config
    from .models import Document
    from .reindex import file_indicizzabili, senza_cartella

    adesso = time.time() if adesso is None else adesso
    esito = {"nuovi": 0, "modificati": 0, "rimossi": 0, "in_copia": 0}
    if not kb.docs_dir.is_dir():
        return esito
    trovati = file_indicizzabili(kb.docs_dir, config.estensioni_escluse(kb),
                                 config.cartelle_escluse(kb))
    indicizzati = dict(Document.objects.filter(knowledge=kb)
                       .values_list("rel_path", "indexed_at").iterator(chunk_size=5000))
    with _falliti_lock:
        falliti = dict(_falliti.get(kb.slug, {}))
    # indicizzati prima che la cartella entrasse nell'intestazione: la passata
    # la aggiunge senza rileggerli (reindex._aggiungi_cartella)
    da_completare = senza_cartella(kb)

    for rel, path in trovati.items():
        indicizzato = indicizzati.get(rel)
        date = _date(path)
        if date is None:
            continue
        mtime, toccato = date
        # stesso criterio di reindex._immutato, altrimenti un file sembrerebbe
        # cambiato qui e invariato là, e si ripartirebbe a ogni giro
        if indicizzato is not None and mtime <= indicizzato.timestamp() and rel not in da_completare:
            continue
        if falliti.get(rel) == mtime:
            continue
        if adesso - toccato < ASSESTAMENTO:
            esito["in_copia"] += 1
            continue
        esito["modificati" if indicizzato is not None else "nuovi"] += 1
    esito["rimossi"] = sum(1 for rel in indicizzati if rel not in trovati)
    return esito


def _passata(kb):
    """L'indicizzazione incrementale, annotando i file che non riesce a leggere."""
    from .reindex import reindex_iter

    errori = {}
    for evento in reindex_iter(kb, incrementale=True):
        rel = evento.get("file")
        if rel and evento.get("action") == "errore":
            mtime = _mtime(kb.docs_dir / rel)
            if mtime is not None:
                errori[rel] = mtime
        yield evento
    with _falliti_lock:
        _falliti[kb.slug] = errori


def controlla(kb) -> str | None:
    """Un controllo su una knowledge; mette in coda l'indicizzazione se serve.
    Ritorna la descrizione di cosa è cambiato, None se niente."""
    from . import jobs

    # fermata dall'utente: si aspetta che la riavvii lui (vedi jobs.ferma)
    if jobs.occupata(kb) or jobs.sospesa(kb):
        return None
    c = cambiamenti(kb)
    if not (c["nuovi"] or c["modificati"] or c["rimossi"]):
        return None
    parti = [f"{c[k]} {k}" for k in ("nuovi", "modificati", "rimossi") if c[k]]
    descrizione = ", ".join(parti)
    jobs.avvia(kb, f"File cambiati in «{kb.name}»", _passata, chiave=f"autoindex:{kb.slug}",
               automatico=True)
    return descrizione


def _giro() -> str:
    from . import ollama
    from .models import KnowledgeBase

    if not ollama.ollama_available()["ok"]:
        # senza embeddings i file nuovi andrebbero tutti in errore
        return f"{ollama.servizio()} non raggiungibile"
    esiti = []
    for kb in KnowledgeBase.objects.all():
        try:
            cambiato = controlla(kb)
        except Exception as e:                           # noqa: BLE001
            log.exception("controllo dei file di %s fallito", kb.slug)
            esiti.append(f"{kb.name}: errore {e}")
            continue
        if cambiato:
            esiti.append(f"{kb.name}: {cambiato}")
    return "; ".join(esiti) or "nessun file cambiato"


def _ciclo():
    from . import config

    time.sleep(ATTESA_INIZIALE)
    while True:
        try:
            minuti = config.get("doc_auto_minutes")
        except Exception:                                # noqa: BLE001
            minuti = 0                                   # database non pronto
        if minuti < 1:
            stato["attivo"] = False
            close_old_connections()
            time.sleep(60)
            continue
        stato["attivo"] = True
        try:
            esito = _giro()
        except Exception as e:                           # noqa: BLE001
            log.exception("controllo dei documenti fallito")
            esito = f"errore: {e}"
        if esito != "nessun file cambiato":
            log.info("documenti: %s", esito)
        stato.update(ultimo=timezone.now().isoformat(timespec="seconds"), esito=esito[:300])
        close_old_connections()
        time.sleep(minuti * 60)


def avvia():
    """Avvia il thread di controllo, una volta sola per processo."""
    global _thread
    with _avvio:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_ciclo, name="kb-autoindex", daemon=True)
        _thread.start()
        log.info("aggiornamento automatico dei documenti avviato")
