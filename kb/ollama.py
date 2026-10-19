"""Client del modello: modelli disponibili, embeddings, chat, descrizione immagini.

Chat ed embeddings vanno a Ollama oppure, con ``KB_LLM_URL`` e ``KB_EMBED_URL``,
a un server compatibile OpenAI (llama-server in rete locale); OCR e descrizione
delle immagini restano su Ollama."""
import base64
import logging
import json
import re
import threading
import time
from pathlib import Path

import requests
from django.conf import settings

from . import config


log = logging.getLogger("kb.ollama")


class OllamaError(RuntimeError):
    pass


class OllamaIrraggiungibile(OllamaError):
    """Ollama spento o non raggiungibile: inutile insistere pagina per pagina."""




# --------------------------------------------------------------------------- #
# Server compatibile OpenAI
#
# Con KB_LLM_URL la chat non va a Ollama ma a un server compatibile OpenAI
# (llama-server in rete locale, vedi MIGRAZIONE_LLM_SERVER.md), con
# KB_EMBED_URL gli embeddings. Chi chiama le funzioni di questo modulo non se
# ne accorge: cambia solo il formato sul filo. Sul server non esistono
# num_ctx, keep_alive e think: il contesto lo decide chi avvia il server, e
# ``num_ctx`` in configurazione serve solo a comporre il prompt entro quel
# tetto. A differenza di Ollama, un prompt troppo lungo il server non lo taglia
# in silenzio: risponde con un errore.
# --------------------------------------------------------------------------- #

# Attese fra un tentativo e l'altro quando il server non risponde o risponde 503
# (lo fa mentre carica il modello dopo un riavvio). Solo finché la richiesta non
# arriva: una risposta lenta non si ripete, l'attesa raddoppierebbe.
RIPROVE_SERVER = (2, 5, 10)

# opzioni di Ollama che hanno un equivalente OpenAI (num_ctx, keep_alive no)
_OPZIONI_SERVER = {"temperature": "temperature", "seed": "seed", "top_p": "top_p",
                   "top_k": "top_k", "num_predict": "max_tokens"}


def _server(tipo: str) -> tuple[str, str] | None:
    """(indirizzo, chiave) del server compatibile OpenAI per "chat" o "embed";
    None se quella parte resta su Ollama."""
    if tipo == "embed":
        return (settings.KB_EMBED_URL, settings.KB_EMBED_API_KEY) if settings.KB_EMBED_URL else None
    return (settings.KB_LLM_URL, settings.KB_LLM_API_KEY) if settings.KB_LLM_URL else None


def servizio() -> str:
    """Chi fa chat ed embeddings, per i messaggi all'utente."""
    return "Server LLM" if settings.KB_LLM_URL or settings.KB_EMBED_URL else "Ollama"


def non_raggiungibile() -> str:
    """Il messaggio per l'utente quando chat o embeddings non rispondono."""
    if servizio() == "Ollama":
        return "Ollama non raggiungibile. Avvialo con: ollama serve"
    return "Server LLM non raggiungibile: controlla che sia acceso e raggiungibile in rete"


def _intestazioni(chiave: str) -> dict:
    return {"Authorization": f"Bearer {chiave}"} if chiave else {}


def _post_server(tipo: str, percorso: str, payload: dict, timeout: int,
                 stream: bool = False) -> requests.Response:
    """POST al server di ``tipo``: attesa di almeno KB_LLM_TIMEOUT secondi e
    nuovi tentativi (RIPROVE_SERVER) se il server non risponde o sta caricando
    il modello. OllamaIrraggiungibile se non risponde neanche all'ultimo."""
    base, chiave = _server(tipo)
    # connessione breve, così un server spento si vede subito; lettura lunga,
    # perché su CPU il primo pezzo di risposta arriva dopo aver letto il prompt
    attesa = (5, max(int(timeout), settings.KB_LLM_TIMEOUT))
    for pausa in (*RIPROVE_SERVER, None):
        try:
            r = requests.post(f"{base}{percorso}", json=payload, headers=_intestazioni(chiave),
                              timeout=attesa, stream=stream)
        except requests.exceptions.ConnectionError as e:
            motivo = str(e)
        except requests.exceptions.RequestException as e:
            raise OllamaError(f"Il server LLM non ha risposto entro {attesa[1]} s: {e}") from e
        else:
            if r.status_code != 503:
                return r
            motivo = f"503 {r.text[:200]}"
            r.close()
        if pausa is None:
            raise OllamaIrraggiungibile(f"Server LLM non raggiungibile ({base}): {motivo}")
        log.info("server LLM %s non pronto (%s): riprovo fra %d s", base, motivo, pausa)
        time.sleep(pausa)


def _messaggi_server(messages: list[dict]) -> list[dict]:
    """I messaggi nel formato OpenAI. Le immagini alla Ollama (``images``, in
    base64) diventano parti ``image_url``: un server che non vede le immagini
    risponde con un errore, invece di inventare quello che non ha visto."""
    out = []
    for m in messages:
        immagini = m.get("images")
        m = {k: v for k, v in m.items() if k != "images"}
        if immagini:
            m["content"] = [{"type": "text", "text": m.get("content") or ""}] + [
                {"type": "image_url",
                 "image_url": {"url": "data:image/{};base64,{}".format(
                     "jpeg" if b.startswith("/9j/") else "png", b)}}
                for b in immagini]
        out.append(m)
    return out


# I compiti interni (chat_once, chat_json, chat_tools) non devono ragionare,
# come think=False su Ollama. Con KB_LLM_SPEGNI_RAGIONAMENTO il client lo
# chiede al template del modello (Qwen3: enable_thinking), come vuole vLLM che
# ragiona di default; spento, non manda niente (il llama-server in rete ha il
# ragionamento spento da sé e chiede di non mandare quel parametro).
def _senza_ragionamento() -> dict:
    if not settings.KB_LLM_SPEGNI_RAGIONAMENTO:
        return {}
    return {"chat_template_kwargs": {"enable_thinking": False}}


_PENSIERO = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


def _senza_pensiero(testo: str) -> str:
    """Il testo senza il ragionamento in testa: vLLM avviato senza
    --reasoning-parser lo lascia in ``content``, e il JSON non si legge più."""
    return _PENSIERO.sub("", testo or "").strip()


def _completamento(payload: dict, timeout: int) -> str:
    """Il testo di una risposta del server di chat, non in streaming."""
    r = _post_server("chat", "/chat/completions", {**payload, **_senza_ragionamento()}, timeout)
    if r.status_code != 200:
        raise OllamaError(f"Errore chat ({r.status_code}): {r.text[:300]}")
    scelta = (r.json().get("choices") or [{}])[0]
    return _senza_pensiero((scelta.get("message") or {}).get("content") or "")




def _modelli_server(tipo: str) -> list[dict]:
    """[{"name", "capabilities"}] del server di ``tipo``. Le capacità le dà il
    ruolo del server (llama-server dichiara "completion" anche per un modello
    di embeddings), più "vision" se il modello accetta immagini."""
    base, chiave = _server(tipo)
    r = requests.get(f"{base}/models", headers=_intestazioni(chiave), timeout=3)
    r.raise_for_status()
    modelli = []
    for m in r.json().get("data", []):
        capacita = ["embedding"] if tipo == "embed" else ["completion"]
        if "image" in ((m.get("architecture") or {}).get("input_modalities") or []):
            capacita.append("vision")
        modelli.append({"name": m["id"], "capabilities": capacita})
    return modelli


# La riga di stato dell'interfaccia chiede questo controllo ogni 15 secondi
# per ogni scheda aperta. Con Ollama spento ogni chiamata aspetta i 3 secondi
# del timeout, e con tre schede aperte sono dodici attese al minuto dentro il
# thread delle richieste. Il risultato cambia solo quando si accende Ollama o
# si installa un modello: tenerlo per qualche secondo basta e avanza.
_STATO_TTL = 10.0
_stato_cache = {"scadenza": 0.0, "valore": None}
_stato_lock = threading.Lock()


def ollama_available(fresco: bool = False) -> dict:
    """Controlla che chi fa chat ed embeddings (Ollama o il server LLM) sia in
    esecuzione e quali modelli offre.

    ``fresco=True`` salta la cache: serve a chi sta per lanciare un lavoro
    lungo e vuole sapere com'è la situazione adesso."""
    if not fresco:
        with _stato_lock:
            if _stato_cache["valore"] is not None and time.monotonic() < _stato_cache["scadenza"]:
                return _stato_cache["valore"]
    try:
        models = []
        for tipo in ("chat", "embed"):
            if _server(tipo):
                models += [m["name"] for m in _modelli_server(tipo)]
        if not (_server("chat") and _server("embed")):
            r = requests.get(f"{settings.OLLAMA_URL}/api/tags", timeout=3)
            r.raise_for_status()
            models += [m["name"] for m in r.json().get("models", [])]
        esito = {"ok": True, "models": list(dict.fromkeys(models))}
    except Exception as e:
        esito = {"ok": False, "error": str(e), "models": []}
    esito["servizio"] = servizio()
    with _stato_lock:
        _stato_cache.update(scadenza=time.monotonic() + _STATO_TTL, valore=esito)
    return esito


# capacità dichiarate da ogni modello ("completion", "vision", "embedding"…),
# per digest: cambiano solo se il modello viene riscaricato o ricreato
_capacita_cache: dict[str, list[str]] = {}


def modelli_installati() -> list[dict]:
    """[{"name", "capabilities"}] dei modelli che si possono usare: quelli dei
    server LLM configurati e, se chat o embeddings restano a Ollama, quelli di
    Ollama; [] se nessuno risponde. ``capabilities`` è None se il modello non
    le dichiara (versioni vecchie di Ollama): chi usa l'elenco non deve escluderlo."""
    modelli = []
    for tipo in ("chat", "embed"):
        if _server(tipo):
            try:
                modelli += _modelli_server(tipo)
            except Exception:                             # noqa: BLE001
                pass
    if not (_server("chat") and _server("embed")):
        modelli += _modelli_ollama()
    return sorted(modelli, key=lambda x: x["name"])


def _modelli_ollama() -> list[dict]:
    try:
        r = requests.get(f"{settings.OLLAMA_URL}/api/tags", timeout=3)
        r.raise_for_status()
        tags = r.json().get("models", [])
    except Exception:                                     # noqa: BLE001
        return []
    modelli = []
    for m in tags:
        digest = m.get("digest") or m["name"]
        capacita = _capacita_cache.get(digest)
        if digest not in _capacita_cache:
            try:
                s = requests.post(f"{settings.OLLAMA_URL}/api/show",
                                  json={"model": m["name"]}, timeout=5)
                s.raise_for_status()
                capacita = _capacita_cache[digest] = s.json().get("capabilities")
            except Exception:                             # noqa: BLE001
                # fallimento non messo in cache: con Ollama occupato si
                # riprova alla prossima apertura della pagina
                capacita = None
        # senza ":latest", come si scrive di solito nelle impostazioni
        # (qwen3-8b-ctx8k): Ollama accetta tutte e due le forme
        modelli.append({"name": m["name"].removesuffix(":latest"),
                        "capabilities": capacita})
    return modelli


# Chunk per richiesta. Un PDF di qualche centinaio di pagine produce migliaia di
# chunk: mandarli tutti insieme sfonda il timeout e fa fallire l'intero documento,
# invece di rallentare soltanto.
EMBED_BATCH = 64


def prefisso_domanda() -> str:
    """Il prefisso da anteporre a una DOMANDA prima dell'embedding.

    I modelli Qwen3-Embedding sono addestrati con un'istruzione sul lato
    domanda ("Instruct: …\\nQuery: …") e nessuna sul lato documento: dirgli
    che cosa deve trovare migliora l'accostamento fra domanda e passaggi.
    Vuoto se in configurazione non c'è alcuna istruzione."""
    istruzione = (config.get("embed_instruct") or "").strip()
    return f"Instruct: {istruzione}\nQuery: " if istruzione else ""


def embed_texts(texts: list[str], query: bool = False) -> list[list[float]]:
    """Calcola gli embeddings con Ollama o con il server degli embeddings, a lotti.

    ``query`` va messo a True per le domande: ricevono il prefisso di
    istruzione (se configurato), i documenti no. Restituisce un vettore per
    ogni testo, nello stesso ordine."""
    if query:
        prefisso = prefisso_domanda()
        if prefisso:
            texts = [prefisso + t for t in texts]
    vettori: list[list[float]] = []
    # contesto tarato sulla lunghezza massima di un chunk: senza, Ollama alloca
    # la finestra intera del modello (16k) per testi da 500 token, e su un Mac
    # da 16 GB quella memoria manca all'OCR e al modello di chat
    num_ctx = max(2048, int((config.get("chunk_size") + config.get("chunk_overlap") + 400) * 1.1))
    server = _server("embed")
    for inizio in range(0, len(texts), EMBED_BATCH):
        lotto = texts[inizio:inizio + EMBED_BATCH]
        if server:
            r = _post_server("embed", "/embeddings",
                             {"model": config.get("embed_model"), "input": lotto}, 300)
        else:
            try:
                r = requests.post(
                    f"{settings.OLLAMA_URL}/api/embed",
                    json={"model": config.get("embed_model"), "input": lotto,
                          "options": {"num_ctx": num_ctx}},
                    timeout=300,
                )
            except requests.exceptions.RequestException as e:
                raise OllamaError(f"Ollama non raggiungibile: {e}") from e
        if r.status_code != 200:
            raise OllamaError(f"Errore embeddings ({r.status_code}): {r.text[:300]}")
        if server:
            # ogni vettore porta l'indice del suo testo: l'ordine non è garantito
            parziali = [d["embedding"] for d in sorted(r.json().get("data") or [],
                                                       key=lambda d: d.get("index", 0))]
        else:
            parziali = r.json().get("embeddings") or []
        if len(parziali) != len(lotto):
            raise OllamaError(
                f"{'Il server degli embeddings' if server else 'Ollama'} ha restituito "
                f"{len(parziali)} vettori su {len(lotto)} richiesti")
        vettori.extend(parziali)
    return vettori




def contesto(num_ctx: int = 0) -> int:
    """La finestra di contesto da chiedere a Ollama: quella data o quella
    configurata (``num_ctx``). Va detta sempre: senza, Ollama usa il default del
    modello (spesso 2048 o 4096) e scarta in silenzio l'inizio del prompt, cioè
    le istruzioni. Funziona solo il modello di sviluppo ``-ctx8k``, che ha il
    contesto nel Modelfile; un modello di produzione no. Al server LLM non si
    chiede: il suo contesto lo fissa chi lo avvia."""
    return int(num_ctx or config.get("num_ctx"))


def chat_once(messages: list[dict], model: str = "", timeout: int = 60,
              num_predict: int = 200, num_ctx: int = 0) -> str:
    """Risposta completa per temi e chat documentale, senza streaming.

    La lunghezza massima viene scelta dal chiamante con num_predict.
    Temperatura 0 e ragionamento spento per risposte aderenti alle fonti."""
    if _server("chat"):
        return _completamento({"model": model or config.get("chat_model"),
                               "messages": _messaggi_server(messages), "stream": False,
                               "temperature": 0, "max_tokens": num_predict}, timeout)
    payload = {
        "model": model or config.get("chat_model"),
        "messages": messages,
        "stream": False,
        "think": False,
        # tetto alla generazione: questi compiti producono una riga, e senza
        # limite un modello che "ragiona" brucerebbe minuti prima di rispondere
        "options": {"temperature": 0, "num_predict": num_predict, "num_ctx": contesto(num_ctx)},
    }
    try:
        r = requests.post(f"{settings.OLLAMA_URL}/api/chat", json=payload, timeout=timeout)
        if r.status_code == 400 and "think" in r.text.lower():
            del payload["think"]
            r = requests.post(f"{settings.OLLAMA_URL}/api/chat", json=payload, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise OllamaError(f"Ollama non raggiungibile: {e}") from e
    if r.status_code != 200:
        raise OllamaError(f"Errore chat ({r.status_code}): {r.text[:300]}")
    return (r.json().get("message", {}).get("content") or "").strip()




# --------------------------------------------------------------------------- #
def pulisci_ocr(testo: str) -> str:
    """Toglie la coda di un ciclo: righe di soli ``` e righe uguali di fila,
    che un modello OCR piccolo ripete senza fermarsi quando la pagina è finita."""
    righe = [r for r in (testo or "").splitlines() if r.strip() not in ("```", "```markdown")]
    pulite = []
    for r in righe:
        if pulite and r.strip() and r.strip() == pulite[-1].strip():
            continue
        pulite.append(r)
    return "\n".join(pulite).strip()


# Tetto ai token scritti per una pagina. Una pagina fitta di testo vero ne
# vale 1.500-2.500 (la più lunga di una polizza Unipol: 4.500 caratteri, circa
# 1.300 token); un ciclo che nessun controllo riconosce, invece, va avanti fino
# a riempire il contesto: 6.000 token, un minuto buttato. Con il tetto una
# pagina costa al massimo una trentina di secondi.
OCR_MAX_TOKEN = 3000


def _in_ciclo(testo: str) -> bool:
    """True se le ultime righe complete sono tutte la stessa: il modello sta
    girando a vuoto.

    La riga dopo l'ultimo a capo non conta: è quella che si sta scrivendo. Il
    modello manda spesso l'a capo attaccato all'inizio della riga seguente
    («⏎COPIA»), e contandola il confronto vedeva sette righe uguali più una
    a metà, e non riconosceva mai il ciclo (393 «COPIA PER L'AGENZIA» di fila)."""
    fine = testo.rfind("\n")
    if fine < 0:
        return False
    righe = [r.strip() for r in testo[:fine].splitlines() if r.strip()]
    coda = righe[-OCR_RIGHE_RIPETUTE:]
    return len(coda) == OCR_RIGHE_RIPETUTE and len(set(coda)) == 1


def ocr_image(path: Path) -> str | None:
    """Legge il testo di una pagina rasterizzata con il modello OCR.

    Serve per i PDF fatti di sole immagini (scansioni), da cui pypdf non
    estrae nulla. Ritorna None se l'OCR è disattivato o non ha letto nulla.

    La risposta arriva in streaming, e non per estetica: glm-ocr, finita la
    pagina, spesso non si ferma e ripete l'ultima riga (un piè di pagina, una
    fila di ```) all'infinito. Ollama se ne accorge e interrompe con «token
    repeat limit reached», e aspettando la risposta intera si perdeva anche
    la pagina letta bene fino a lì. Così il testo ricevuto resta, il ciclo lo
    tronchiamo noi appena si vede, e la coda ripetuta si toglie."""
    model = config.get("ocr_model")
    if not model:
        return None
    payload = {
        "model": model,
        "prompt": OCR_PROMPT,
        "images": [base64.b64encode(path.read_bytes()).decode()],
        "stream": True,
        # trascrizione, non interpretazione: nessuna creatività, nessun ragionamento.
        # Una pagina a 1600 px vale circa 4100 token di immagine, più il testo
        # letto: 8192 bastano, e rispetto ai 16k del modello dimezzano la memoria
        "options": {"temperature": 0, "num_ctx": OCR_NUM_CTX, "num_predict": OCR_MAX_TOKEN},
        "think": False,
        # il modello OCR pesa diversi GB: appena finito il documento va
        # scaricato in fretta, per lasciare memoria a embeddings e chat
        "keep_alive": "2m",
    }

    def _post(p):
        try:
            return requests.post(f"{settings.OLLAMA_URL}/api/generate", json=p, timeout=900,
                                 stream=True)
        except requests.exceptions.RequestException as e:
            raise OllamaIrraggiungibile(f"Ollama non raggiungibile durante l'OCR: {e}") from e

    r = _post(payload)
    if r.status_code == 400 and "think" in r.text.lower():
        del payload["think"]
        r = _post(payload)
    if r.status_code != 200:
        raise OllamaError(f"Errore OCR ({r.status_code}): {r.text[:300]}")
    testo, interrotto = "", ""
    try:
        for riga in r.iter_lines():
            if not riga:
                continue
            dati = json.loads(riga)
            if dati.get("error"):
                interrotto = dati["error"]
                break
            pezzo = dati.get("response", "")
            testo += pezzo
            if "\n" in pezzo and _in_ciclo(testo):
                interrotto = "ciclo di righe ripetute"
                testo = testo[:testo.rfind("\n")]      # via la riga lasciata a metà
                break
            if dati.get("done"):
                if dati.get("done_reason") == "length":
                    interrotto = f"tetto di {OCR_MAX_TOKEN} token"
                break
    except requests.exceptions.RequestException as e:
        raise OllamaIrraggiungibile(f"Ollama non raggiungibile durante l'OCR: {e}") from e
    finally:
        r.close()
    testo = pulisci_ocr(testo)
    if interrotto:
        if not testo:
            raise OllamaError(f"Errore OCR: {interrotto}")
        log.info("OCR di %s interrotto (%s): tenuti %d caratteri", path.name, interrotto, len(testo))
    return testo or None


DISEGNO_PROMPT = (
    "Questa è una tavola di un disegno tecnico. Descrivila in italiano, in modo "
    "che chi non la vede capisca di che pezzo si tratta e possa ritrovare "
    "disegni simili:\n"
    "1. Cartiglio: codice o numero del disegno, denominazione del pezzo, "
    "materiale, massa, scala, date, revisione, autore (trascrivi i valori "
    "esatti, anche se in un'altra lingua).\n"
    "2. Il pezzo: che oggetto è (flangia, staffa, albero, piastra, getto, "
    "lamiera piegata…), forma generale, lavorazioni evidenti (fori, "
    "filettature, smussi, scanalature).\n"
    "3. Viste e sezioni presenti.\n"
    "4. Quote principali, tolleranze, rugosità e note leggibili.\n"
    "Scrivi solo quello che leggi davvero: se un valore non si legge, dillo."
)


def modello_disegni() -> str:
    """Il modello di visione per i disegni tecnici: il suo, o quello generico."""
    return config.get("disegni_model") or config.get("vision_model")


def describe_image(path: Path, prompt: str | None = None, model: str = "",
                   num_ctx: int = 0) -> str | None:
    """Descrive un'immagine con il modello di visione, se installato.

    ``prompt`` sostituisce la richiesta generica (es. ``DISEGNO_PROMPT``),
    ``model`` il modello visione, ``num_ctx`` il contesto predefinito del
    modello (serve per le immagini grandi, che valgono migliaia di token)."""
    try:
        img_b64 = base64.b64encode(path.read_bytes()).decode()
        modello = model or config.get("vision_model")
        payload = {
            "model": modello,
            "prompt": prompt or "Descrivi dettagliatamente questa immagine in italiano, "
                                "includendo eventuale testo visibile.",
            "images": [img_b64],
            "stream": False,
            # niente ragionamento per le descrizioni: serve solo velocità
            "think": False,
        }
        # Un modello di visione a parte si scarica appena possibile, come per
        # l'OCR, per lasciare memoria alla chat. Se è lo stesso modello della
        # chat (un modello solo per tutto) no: scaricarlo farebbe aspettare il
        # caricamento alla domanda successiva.
        if modello != config.get("chat_model"):
            payload["keep_alive"] = "2m"
        payload["options"] = {"num_ctx": contesto(num_ctx)}
        r = requests.post(f"{settings.OLLAMA_URL}/api/generate", json=payload, timeout=600)
        if r.status_code == 400 and "think" in r.text.lower():
            del payload["think"]
            r = requests.post(f"{settings.OLLAMA_URL}/api/generate", json=payload, timeout=600)
        if r.status_code == 200:
            return r.json().get("response", "").strip() or None
        log.warning("descrizione immagine %s: il modello ha risposto %s",
                    path.name, r.status_code)
    except Exception as e:                                # noqa: BLE001
        # Silenziare qui faceva sembrare uguali tre cose diverse: modello di
        # visione non installato, Ollama spento e file illeggibile. Il
        # documento viene salvato lo stesso con il solo nome del file, ma
        # almeno nel log resta scritto perché.
        log.warning("descrizione immagine %s non riuscita: %s", path.name, e)
    return None
