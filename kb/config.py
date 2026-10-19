"""Impostazioni modificabili a runtime dalla sezione amministrazione.

Ogni chiave ha un default in settings.py (a sua volta sovrascrivibile con
variabili d'ambiente). Un valore salvato nel DB ha la precedenza; una
stringa vuota (o la riga assente) significa "usa il default".
"""
import contextlib
import os
import re
import threading
import time
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError, connection

from .models import Setting

# chiave -> (nome in settings, tipo, gruppo, etichetta, note per la UI)
SCHEMA = {
    "chat_model": ("KB_CHAT_MODEL", str, "Modelli", "Modello chat e temi",
                   "Risponde alle domande sui documenti e assegna un nome ai temi: su Ollama o sul server LLM se "
                   "KB_LLM_URL è impostato in .env. Vuoto = quello di .env."),
    "embed_model": ("KB_EMBED_MODEL", str, "Modelli", "Modello embeddings",
                    "Dopo averlo cambiato serve una reindicizzazione completa."),
    "embed_dim": ("KB_EMBED_DIM", int, "Modelli", "Dimensione embeddings",
                  "Deve corrispondere al modello (nomic-embed-text: 768)."),
    "vision_model": ("KB_VISION_MODEL", str, "Modelli", "Modello visione",
                     "Descrive le immagini durante l'indicizzazione (opzionale)."),
    "ocr_model": ("KB_OCR_MODEL", str, "Modelli", "Modello OCR (PDF scansionati)",
                  "Legge i PDF fatti di pagine-immagine, che non hanno testo estraibile "
                  "(es. glm-ocr:q8_0). Vuoto = OCR disattivato."),
    "ocr_width": ("KB_OCR_WIDTH", int, "Modelli", "Larghezza della pagina per l'OCR (pixel)",
                  "Quanto è larga l'immagine di una pagina scansionata passata al modello OCR. "
                  "Con glm-ocr 1100 legge più testo di 1600 (a 1600 saltava l'intestazione "
                  "delle polizze) ed è più veloce; più stretto, i caratteri piccoli si perdono. "
                  "Vale per i documenti letti dopo la modifica."),
    "ocr_min_chars": ("KB_OCR_MIN_CHARS", int, "Modelli", "Soglia OCR (caratteri)",
                      "Se un PDF produce meno caratteri di così, viene considerato una "
                      "scansione e passato all'OCR."),
    "num_ctx": ("KB_NUM_CTX", int, "Modelli", "Contesto del modello (token)",
                "Quanto testo il modello può leggere in una volta: istruzioni, storico, "
                "allegati e passaggi recuperati vengono tagliati per starci dentro. "
                "Deve corrispondere al modello (es. 8192 per un modello \"ctx8k\"). Con il "
                "server LLM conviene stare sotto il suo contesto, che non si può "
                "chiedere: un prompt più lungo dà errore (12288 su 16384)."),
    "embed_instruct": ("KB_EMBED_INSTRUCT", str, "Modelli", "Istruzione per le domande (embeddings)",
                       "Frase anteposta alla domanda prima di calcolarne il vettore, come "
                       "vogliono i modelli Qwen3-Embedding (es. \"Given a search query, "
                       "retrieve relevant passages that answer the query\"). I documenti "
                       "restano senza prefisso: non serve reindicizzare. Vuoto = nessuna."),
    "top_k": ("KB_TOP_K", int, "Ricerca interna", "Chunk recuperati (top-k)",
              "Numero massimo di passaggi restituiti dalla ricerca interna."),
    "chunks_per_doc": ("KB_CHUNKS_PER_DOC", int, "Ricerca interna", "Max chunk per documento",
                       "Quanti dei passaggi recuperati possono venire dallo stesso "
                       "documento: con un tetto basso la risposta si appoggia a più fonti "
                       "diverse. 0 = nessun limite."),
    "chunk_size": ("KB_CHUNK_SIZE", int, "Ricerca interna", "Dimensione chunk (caratteri)",
                   "Vale per i documenti indicizzati dopo la modifica."),
    "chunk_overlap": ("KB_CHUNK_OVERLAP", int, "Ricerca interna", "Sovrapposizione chunk",
                      ""),
    "chunk_header": ("KB_CHUNK_HEADER", int, "Ricerca interna", "Intestazione di contesto nei chunk",
                     "1 = ogni passaggio comincia con titolo, mittente e data del documento "
                     "a cui appartiene, così la ricerca e il modello sanno di cosa parla "
                     "anche il terzo pezzo di una email lunga. Vale per i documenti "
                     "indicizzati dopo la modifica (Reindicizza per applicarla a tutti)."),
    "hybrid": ("KB_HYBRID", int, "Ricerca interna", "Ricerca ibrida",
               "1 = alla ricerca per significato (vettori) si affianca quella per "
               "parole esatte (full-text), utile per codici, sigle e nomi propri. "
               "0 = solo vettori."),
    "fetch_k": ("KB_FETCH_K", int, "Ricerca interna", "Candidati per motore",
                "Quanti passaggi propone ciascun motore prima della fusione. "
                "Più alti = più possibilità di pescare il passaggio giusto, "
                "ricerca leggermente più lenta."),
    "escludi_ext": ("KB_EXCLUDE_EXT", str, "Documenti", "Estensioni escluse",
                    "Tipi di file da ignorare, separati da virgola o spazio (es. \".eml, "
                    ".zip\"). Non vengono indicizzati e non contano negli avvisi della riga "
                    "di stato (file fuori dalle knowledge, formati non supportati). I file "
                    "restano su disco. Quelli già indicizzati escono dall'indice alla "
                    "prossima reindicizzazione; togliendo un'estensione dal filtro "
                    "rientrano, ma vanno rielaborati. Vale per tutte le knowledge: "
                    "per una sola c'è «Filtro dei file…» nel menu della pagina "
                    "Knowledge."),
    "escludi_cartelle": ("KB_EXCLUDE_DIRS", str, "Documenti", "Cartelle escluse",
                         "Nomi di cartelle in cui import e indicizzazione non entrano, "
                         "separati da virgola (es. \"node_modules, venv\"). Conta il nome "
                         "esatto della cartella, a qualsiasi profondità, senza badare a "
                         "maiuscole e minuscole; le cartelle nascoste sono già escluse. "
                         "Vuoto = l'elenco predefinito. I file restano su disco; quelli "
                         "già indicizzati escono dall'indice alla prossima "
                         "reindicizzazione. Per una sola knowledge c'è «Filtro dei "
                         "file…» nel menu della pagina Knowledge."),
    "mail_strip_quotes": ("KB_MAIL_STRIP_QUOTES", int, "Documenti", "Togli le citazioni dai messaggi EML",
                          "1 = il testo delle risposte precedenti riportato in coda a ogni "
                          "email (\"Il giorno … ha scritto:\") non viene indicizzato: lo "
                          "stesso paragrafo non compare più in cinque messaggi diversi. "
                          "Il file su disco resta completo. Vale dalla prossima "
                          "indicizzazione."),
    "doc_auto_minutes": ("KB_DOC_AUTO_MINUTES", int, "Documenti", "Aggiornamento automatico (minuti)",
                         "Ogni quanti minuti controllare se nelle cartelle delle knowledge "
                         "sono stati aggiunti, cambiati o tolti file a mano (dal Finder, da "
                         "una cartella sincronizzata…) e indicizzarli. Il controllo confronta "
                         "solo nomi e date, senza leggere i file. 0 = solo dal pulsante "
                         "Reindicizza."),
    "temi_auto": ("KB_TEMI_AUTO", int, "Grafo", "Rifai i temi ogni N documenti",
                  "Il raggruppamento per argomento si rifà da solo quando tanti "
                  "documenti sono cambiati dall'ultima volta: è l'unico modo perché "
                  "nascano temi nuovi (i documenti nuovi, da soli, possono solo "
                  "entrare in temi che esistono già). Costa circa mezzo minuto: "
                  "il nome di ogni tema lo sceglie il modello di chat. Al primo import, o "
                  "quando cambia almeno un quarto dell'archivio, si rifà anche prima. 0 = mai, "
                  "si rifà solo dal menu («Rifai i temi») o con «manage.py temi»."),
    "disegni_model": ("KB_DISEGNI_MODEL", str, "Disegni tecnici", "Modello che legge i disegni",
                      "Modello di visione per le tavole dei PDF (pagine da A3 in su) e per i "
                      "tipi di documento letti dall'immagine. Vuoto = il modello visione. Un "
                      "modello grande (es. qwen3-vl 32B) legge molto meglio quote e cartiglio."),
    "disegni_width": ("KB_DISEGNI_WIDTH", int, "Disegni tecnici", "Risoluzione della tavola (pixel)",
                      "Lato lungo dell'immagine della tavola passata al modello. Più grande = "
                      "quote piccole leggibili, ma più lento e più memoria: 1600 per un modello "
                      "piccolo, 2500-3500 per uno grande su GPU. Vale per i documenti letti "
                      "dopo la modifica."),
    "disegni_num_ctx": ("KB_DISEGNI_NUM_CTX", int, "Disegni tecnici", "Contesto del modello (token)",
                        "Un'immagine vale circa un token ogni 32×32 pixel: a 3000 px una tavola "
                        "A3 sono circa 6000 token, più la risposta. 0 = il contesto predefinito "
                        "del modello; alzarlo insieme alla risoluzione."),
    "disegni_indice": ("KB_DISEGNI_INDICE", int, "Disegni tecnici", "Descrivi le tavole nell'indice",
                       "1 = durante l'indicizzazione le pagine da A3 in su dei PDF le descrive il "
                       "modello dei disegni (cartiglio, pezzo, quote), invece di indicizzarne il "
                       "testo, che nelle tavole sono quote sparse. Costa una lettura del modello "
                       "per tavola. Vale per i documenti indicizzati dopo la modifica: per quelli "
                       "già in indice serve una reindicizzazione completa. 0 = solo per gli "
                       "allegati in chat.")
}

MODEL_KEYS = {"chat_model", "embed_model", "vision_model", "ocr_model", "disegni_model"}


def default(key):
    return getattr(settings, SCHEMA[key][0])


# Cache delle impostazioni salvate. `get` sta dentro i cicli caldi
# (indicizzazione, ricerca, composizione del prompt): una SELECT per chiamata
# significa centinaia di migliaia di query per una reindicizzazione. Qui le
# righe si leggono tutte insieme e si tengono per qualche secondo.
#
# La scadenza serve al caso di più processi (gunicorn): chi salva invalida la
# propria copia subito, gli altri si allineano entro il TTL.
_CACHE_TTL = 5.0
_cache: dict[str, str] | None = None
_cache_t = 0.0
_cache_lock = threading.Lock()


def _leggi() -> dict[str, str]:
    try:
        return {s.key: s.value.strip() for s in Setting.objects.all()}
    except DatabaseError:
        # tabella non ancora creata (prima di migrate): valgono i default
        return {}


def _salvate() -> dict[str, str]:
    """Le impostazioni scritte nel database, in una sola query."""
    global _cache, _cache_t
    # Dentro una transazione si vedono anche le scritture non ancora
    # confermate: se finissero in cache, un annullamento le lascerebbe in giro
    # come se fossero state salvate. Lì si legge e basta. In esercizio non
    # cambia niente — le impostazioni si leggono fuori dalle transazioni —
    # mentre nella suite di test ogni prova è una transazione annullata alla
    # fine, e senza questo le prove si passerebbero i valori l'una all'altra.
    if connection.in_atomic_block:
        return _leggi()
    ora = time.monotonic()
    with _cache_lock:
        if _cache is not None and ora - _cache_t < _CACHE_TTL:
            return _cache
    righe = _leggi()
    with _cache_lock:
        _cache, _cache_t = righe, ora
    return righe


def invalida_cache() -> None:
    """Rilegge le impostazioni alla prossima `get`. Da chiamare dopo ogni
    scrittura su Setting fatta fuori da `set_values`."""
    global _cache_t
    with _cache_lock:
        _cache_t = 0.0


# Valori imposti dentro un solo processo, sopra a tutto il resto (vedi `forza`).
_forzati: dict = {}


@contextlib.contextmanager
def forza(**valori):
    """Dentro il blocco ``get`` restituisce questi valori, ma solo in questo
    processo: le misure (evalrag) provano una configurazione senza scriverla
    nel database, dove la leggerebbe anche il server acceso."""
    for key in valori:
        if key not in SCHEMA:
            raise KeyError(key)
    prima = dict(_forzati)
    _forzati.update(valori)
    try:
        yield
    finally:
        _forzati.clear()
        _forzati.update(prima)


def get(key):
    if key in _forzati:
        return _forzati[key]
    _, typ, *_ = SCHEMA[key]
    raw = _salvate().get(key, "")
    if raw == "":
        return default(key)
    try:
        return typ(raw)
    except (TypeError, ValueError):
        return default(key)


# Tipi di file del filtro di ogni knowledge (KnowledgeBase.escludi_tipi):
# chiave salvata -> (nome mostrato, estensioni). Raggruppano le estensioni
# che la pipeline sa leggere, così chi sceglie «niente email» non deve sapere
# che le email sono anche .pst e .mbox.
TIPI_FILE = {
    "testo": ("Testo", settings.KB_TEXT_EXT),
    "pdf": ("PDF", settings.KB_PDF_EXT),
    "word": ("Word", settings.KB_DOCX_EXT),
    "powerpoint": ("PowerPoint", settings.KB_PPTX_EXT),
    "excel": ("Excel", settings.KB_XLSX_EXT),
    "html": ("Pagine web", settings.KB_HTML_EXT),
    "immagini": ("Immagini", settings.KB_IMAGE_EXT),
    "email": ("Messaggi EML", settings.KB_EML_EXT),
    "cad": ("Disegni CAD", settings.KB_CAD_EXT),
}


def elenco_estensioni(testo: str) -> set[str]:
    """Estensioni scritte a mano, normalizzate: ".EML", "eml" e "*.eml"
    diventano tutte ".eml"."""
    escluse = set()
    for voce in re.split(r"[\s,;]+", testo or ""):
        voce = voce.lstrip("*").lower()
        if voce and voce != ".":
            escluse.add(voce if voce.startswith(".") else "." + voce)
    return escluse


def estensioni_escluse(kb=None) -> set[str]:
    """Le estensioni da non importare né indicizzare: il filtro globale
    «Estensioni escluse» e, se c'è ``kb``, il filtro di quella knowledge
    (tipi tolti ed estensioni scritte a mano)."""
    escluse = elenco_estensioni(get("escludi_ext"))
    if kb is not None:
        escluse |= elenco_estensioni(kb.escludi_ext)
        for tipo in kb.escludi_tipi or []:
            if tipo in TIPI_FILE:
                escluse |= TIPI_FILE[tipo][1]
    return escluse


def elenco_cartelle(testo: str) -> set[str]:
    """Nomi di cartelle scritti a mano, normalizzati: minuscoli e senza le
    barre attorno ("Node_Modules/" diventa "node_modules"). Si separano con
    virgola, punto e virgola o a capo, non con lo spazio: una cartella può
    chiamarsi «Vecchi preventivi». Un percorso ("a/b") non è un nome e resta
    fuori: si confronta una cartella alla volta."""
    nomi = set()
    for voce in re.split(r"[,;\n]+", testo or ""):
        voce = voce.strip().strip("/").strip().lower()
        if voce and "/" not in voce:
            nomi.add(voce)
    return nomi


def cartelle_escluse(kb=None) -> set[str]:
    """I nomi (minuscoli) delle cartelle in cui import e indicizzazione non
    entrano: «Cartelle escluse» di /config e, se c'è ``kb``, quelle del filtro
    della knowledge. Le cartelle nascoste restano fuori comunque."""
    nomi = elenco_cartelle(get("escludi_cartelle"))
    if kb is not None:
        nomi |= elenco_cartelle(kb.escludi_cartelle)
    return nomi


def scorri(radice: Path, cartelle: set[str], saltate: list | None = None):
    """I file sotto ``radice``, in ordine di nome, senza entrare nelle
    cartelle nascoste né in quelle di ``cartelle`` (nomi minuscoli, vedi
    ``cartelle_escluse``). Le salta prima di entrarci: un node_modules sono
    decine di migliaia di file da elencare per poi scartarli tutti. Niente
    file nascosti (.DS_Store e simili).

    ``saltate``: se c'è, riceve le cartelle lasciate fuori per nome (non
    quelle nascoste), per poterlo dire all'utente."""
    for base, sottocartelle, nomi in os.walk(radice):
        visibili = sorted(c for c in sottocartelle if not c.startswith("."))
        sottocartelle[:] = [c for c in visibili if c.lower() not in cartelle]
        if saltate is not None:
            saltate += [Path(base) / c for c in visibili if c.lower() in cartelle]
        for nome in sorted(nomi):
            if not nome.startswith("."):
                yield Path(base) / nome


# Intervallo ammesso per i valori numerici, chiave -> (minimo, massimo).
# Un numero fuori scala qui non è un capriccio di stile: una dimensione di
# chunk a zero, o una sovrapposizione grande quanto il chunk, manda
# l'indicizzazione in un ciclo che non finisce (vedi chunk_text in ingest.py).
# `None` come estremo significa "nessun limite da questo lato".
LIMITI = {
    "chunk_size": (200, 100_000),
    "chunk_overlap": (0, None),
    "embed_dim": (1, 16_384),
    "num_ctx": (256, 1_000_000),
    "top_k": (1, 200),
    "chunks_per_doc": (0, 200),
    "fetch_k": (1, 1_000),
    "ocr_min_chars": (0, None),
    "ocr_width": (600, 3000),
    "disegni_width": (800, 5000),
    "disegni_num_ctx": (0, 1_000_000),
    "temi_auto": (0, None),
    "doc_auto_minutes": (0, None),
    "chunk_header": (0, 1),
    "hybrid": (0, 1),
    "mail_strip_quotes": (0, 1),
    "disegni_indice": (0, 1)
}


def _fuori_scala(key, valore) -> str | None:
    """Messaggio d'errore se il valore esce dall'intervallo ammesso."""
    minimo, massimo = LIMITI.get(key, (None, None))
    if minimo is not None and valore < minimo:
        return f"il minimo è {minimo}"
    if massimo is not None and valore > massimo:
        return f"il massimo è {massimo}"
    return None


def set_values(values: dict) -> dict:
    """Salva i valori ricevuti dalla UI. Stringa vuota = torna al default.

    Ritorna un dizionario chiave -> messaggio per i valori rifiutati."""
    errors = {}
    convertiti = {}
    for key, raw in values.items():
        if key not in SCHEMA:
            errors[key] = "impostazione sconosciuta"
            continue
        raw = str(raw).strip()
        if raw == "":
            Setting.objects.filter(key=key).delete()
            invalida_cache()
            continue
        typ = SCHEMA[key][1]
        try:
            valore = typ(raw)
        except (TypeError, ValueError):
            errors[key] = "valore non valido"
            continue
        if typ in (int, float):
            problema = _fuori_scala(key, valore)
            if problema:
                errors[key] = problema
                continue
        convertiti[key] = valore

    # La sovrapposizione va confrontata con la dimensione del chunk, che può
    # arrivare nella stessa richiesta oppure essere già salvata: senza questo
    # controllo incrociato i due valori sono validi presi uno per uno e
    # rovinosi presi insieme.
    if "chunk_overlap" in convertiti or "chunk_size" in convertiti:
        size = convertiti.get("chunk_size", get("chunk_size"))
        overlap = convertiti.get("chunk_overlap", get("chunk_overlap"))
        if overlap >= size:
            chiave = "chunk_overlap" if "chunk_overlap" in convertiti else "chunk_size"
            errors[chiave] = f"la sovrapposizione deve stare sotto la dimensione del chunk ({size})"
            convertiti.pop("chunk_overlap", None)
            convertiti.pop("chunk_size", None)

    for key, valore in convertiti.items():
        Setting.objects.update_or_create(key=key, defaults={"value": str(valore)})
    if convertiti:
        invalida_cache()
    return errors


def dump() -> list[dict]:
    """Stato completo delle impostazioni, nel formato usato da /api/config.

    Le chiavi di un gruppo escono tutte insieme, nell'ordine in cui il gruppo
    compare la prima volta in SCHEMA: la pagina apre una sezione a ogni
    cambio di gruppo, e una chiave aggiunta più in basso la duplicava."""
    overrides = {s.key: s.value.strip() for s in Setting.objects.all()}
    ordine = {}
    for _, _, group, *_ in SCHEMA.values():
        ordine.setdefault(group, len(ordine))
    chiavi = sorted(SCHEMA.items(), key=lambda kv: ordine[kv[1][2]])
    return [{
        "key": key,
        "group": group,
        "label": label,
        "help": help_,
        "type": typ.__name__,
        "value": get(key),
        "default": default(key),
        "overridden": bool(overrides.get(key)),
        "is_model": key in MODEL_KEYS,
    } for key, (_, typ, group, label, help_) in chiavi]
