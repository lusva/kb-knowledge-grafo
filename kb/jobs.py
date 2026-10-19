"""Lavori lunghi che continuano anche se il browser se ne va.

Finora la reindicizzazione era *il generatore consumato dalla risposta HTTP*:
andava avanti solo finché il browser leggeva. Chiudere la scheda, ricaricare la
pagina o addormentare il portatile la interrompeva a metà, lasciando una parte
dei documenti indicizzata e nessuna traccia di dov'era arrivata.

Qui il lavoro gira in un thread e la richiesta HTTP diventa una *spettatrice*:
si attacca al lavoro in corso, ne riceve gli eventi e può staccarsi in
qualsiasi momento senza fermarlo. Ricaricando la pagina ci si riattacca.

Un lavoro alla volta, gli altri in coda: reindicizzare due knowledge insieme
non è più veloce (l'indicizzazione è già limitata dagli embeddings) e si
contenderebbero lo stesso indice. La coda serve perché le richieste arrivano
da più parti — il pulsante, una nota salvata, l'arrivo di posta — e nessuna
deve andare persa perché un'altra era in corso.

Lo stato vive in memoria: un lavoro non sopravvive al riavvio del server, ma
sopravvive a tutto ciò che può fare l'utente dal browser — che è il caso vero.

Un lavoro si può fermare (``ferma``). Quello in coda esce dalla coda; quello
in corso si ferma al primo evento che manda, cioè, per l'indicizzazione, dopo
il documento che sta leggendo: ogni evento arriva a documento salvato, quindi
non resta niente a metà. Un OCR di trecento pagine durerebbe però minuti, e
lì la lettura controlla pagina per pagina (``controlla``). Fermata
un'indicizzazione, quella automatica della stessa knowledge (file cambiati,
posta, calendari, database) resta sospesa finché non la si riavvia a mano:
altrimenti il lavoro lasciato a metà ripartirebbe da solo pochi minuti dopo.
"""
import threading
import time
import uuid
from collections import deque

from django.db import close_old_connections

_corrente = None            # il lavoro in corso, o l'ultimo concluso
_coda: list = []            # lavori in attesa, nell'ordine di arrivo
_condizione = threading.Condition()

IN_CODA, IN_CORSO, FINITO, ERRORE = "in coda", "in corso", "finito", "errore"
FERMATO = "fermato"

# knowledge la cui indicizzazione automatica è sospesa dopo uno stop
_sospese: set[str] = set()
# il lavoro che il thread sta eseguendo, per ``controlla``
_locale = threading.local()


class Fermato(BaseException):
    """Il lavoro è stato fermato dall'utente. Deriva da BaseException come
    GeneratorExit: i ``except Exception`` che proteggono un documento alla
    volta (un file illeggibile non deve fermare gli altri) non lo devono
    scambiare per un errore di quel documento."""


def controlla() -> None:
    """Solleva Fermato se il lavoro che il thread sta eseguendo è stato
    fermato. Per i passi lunghi dentro un documento (OCR pagina per pagina);
    fuori dalla coda dei lavori non fa niente."""
    lavoro = getattr(_locale, "lavoro", None)
    if lavoro is not None and lavoro.fermo.is_set():
        raise Fermato

# Quanti eventi recenti tenere di un lavoro. Ce n'è uno per file indicizzato,
# e l'oggetto resta vivo fino al riavvio del server: su cinquantamila
# documenti erano cinquantamila dizionari in memoria, con dentro il percorso
# di ogni file. Chi guarda l'avanzamento vede le ultime righe, non tutte.
MAX_EVENTI = 500


class Lavoro:
    """Una reindicizzazione in coda, in corso o appena conclusa."""

    def __init__(self, kb, descrizione: str, sorgente, chiave: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.kb_slug = kb.slug
        self.kb_nome = kb.name
        self.kb_pk = kb.pk
        self.descrizione = descrizione
        # Su cosa si riconoscono due lavori uguali. Di norma la descrizione,
        # che è anche quel che si legge nella barra; ma due lavori possono
        # chiamarsi allo stesso modo e fare cose diverse, e allora chi li
        # mette in coda passa una chiave propria.
        self.chiave = chiave or descrizione
        self.sorgente = sorgente
        self.eventi = deque(maxlen=MAX_EVENTI)   # gli ultimi eventi di reindex_iter
        self.n_eventi = 0                 # quanti ne sono passati in tutto
        self.evento_start = {}            # il primo, che esce presto dalla finestra
        self.stato = IN_CODA
        self.errore = ""
        self.stats = None
        self.creato = time.time()
        self.iniziato = None
        self.finito = None
        self.fermo = threading.Event()     # chiesto lo stop (vedi ferma)

    @property
    def attivo(self) -> bool:
        """True finché il lavoro non è concluso (in coda o in corso)."""
        return self.stato in (IN_CODA, IN_CORSO)

    def registra(self, evento: dict) -> None:
        """Aggiunge un evento alla finestra recente, tenendo il conto totale."""
        if evento.get("type") == "start" and not self.evento_start:
            self.evento_start = evento
        self.eventi.append(evento)
        self.n_eventi += 1

    def riassunto(self) -> dict:
        """Stato sintetico, per chi si riattacca o interroga da fuori."""
        ultimo = next((e for e in reversed(self.eventi) if e.get("type") == "progress"), {})
        inizio = self.evento_start
        partenza = self.iniziato or time.time()
        return {
            "id": self.id,
            "kb": self.kb_slug,
            "kb_nome": self.kb_nome,
            "descrizione": self.descrizione,
            "stato": self.stato,
            "errore": self.errore,
            "stats": self.stats,
            "done": ultimo.get("done", 0),
            "total": inizio.get("total", 0),
            "file": ultimo.get("file", ""),
            "eventi": self.n_eventi,
            "secondi": round((self.finito or time.time()) - partenza),
            # stop chiesto, ma il documento in corso non è ancora finito
            "fermando": self.stato == IN_CORSO and self.fermo.is_set(),
        }


def corrente() -> Lavoro | None:
    return _corrente


def attivi() -> list[Lavoro]:
    """I lavori ancora da concludere: quello in corso e quelli in coda."""
    with _condizione:
        candidati = ([_corrente] if _corrente else []) + _coda
        return [l for l in candidati if l.attivo]


def stato() -> dict:
    """Stato del lavoro in corso (o dell'ultimo concluso), più la coda.
    {} se non c'è mai stato un lavoro."""
    with _condizione:
        lavoro, coda = _corrente, list(_coda)
    if lavoro is None:
        return {}
    esito = lavoro.riassunto()
    esito["in_coda"] = [{"id": l.id, "kb": l.kb_slug, "descrizione": l.descrizione}
                        for l in coda]
    return esito


def occupata(kb) -> bool:
    """True se c'è un lavoro in coda o in corso su questa knowledge."""
    with _condizione:
        candidati = ([_corrente] if _corrente else []) + _coda
        return any(l.attivo and l.kb_slug == kb.slug for l in candidati)


def sospesa(kb) -> bool:
    """True se l'indicizzazione automatica di ``kb`` è sospesa da uno stop."""
    with _condizione:
        return kb.slug in _sospese


def ferma(lavoro_id: str) -> Lavoro | None:
    """Ferma un lavoro: se è in coda lo toglie, se è in corso gli chiede di
    fermarsi al prossimo evento. Sospende l'indicizzazione automatica della
    sua knowledge. None se non c'è un lavoro attivo con quell'id."""
    with _condizione:
        lavoro = next((l for l in _coda if l.id == lavoro_id), None)
        if lavoro is not None:
            _coda.remove(lavoro)
            lavoro.stato = FERMATO
            lavoro.finito = time.time()
            lavoro.registra({"type": "stopped", "done": 0, "total": 0})
        elif _corrente is not None and _corrente.id == lavoro_id and _corrente.stato == IN_CORSO:
            lavoro = _corrente
            if not lavoro.fermo.is_set():
                lavoro.fermo.set()
                lavoro.registra({"type": "stopping"})
        else:
            return None
        _sospese.add(lavoro.kb_slug)
        _condizione.notify_all()
        return lavoro


def avvia(kb, descrizione: str, sorgente, chiave: str = "",
          automatico: bool = False) -> Lavoro:
    """Mette in coda ``sorgente(kb)`` — un generatore di eventi — e lo avvia
    appena non c'è altro in corso.

    Se lo stesso lavoro (stessa knowledge, stessa chiave) è già in coda o in
    corso lo restituisce invece di accodarne un altro: due note salvate di
    seguito hanno bisogno di una sola indicizzazione incrementale.

    ``chiave`` distingue lavori che si presentano con la stessa descrizione ma
    non fanno la stessa cosa: una passata incrementale guarda le date dei
    file, una completa ne riconta le impronte, e chi ha chiesto la seconda non
    deve ricevere in cambio la prima.

    ``automatico``: il lavoro non l'ha chiesto l'utente (file cambiati, posta
    arrivata). Se l'utente ha fermato un'indicizzazione di questa knowledge,
    non parte: torna un lavoro già fermato, che chi aspetta l'esito tratta
    come un'indicizzazione senza statistiche. Un lavoro chiesto dall'utente,
    invece, toglie la sospensione."""
    with _condizione:
        chiave = chiave or descrizione
        if automatico and kb.slug in _sospese:
            lavoro = Lavoro(kb, descrizione, sorgente, chiave)
            lavoro.stato = FERMATO
            lavoro.finito = time.time()
            return lavoro
        if not automatico:
            _sospese.discard(kb.slug)
        candidati = ([_corrente] if _corrente else []) + _coda
        for esistente in candidati:
            if (esistente.attivo and esistente.kb_slug == kb.slug
                    and esistente.chiave == chiave):
                return esistente
        lavoro = Lavoro(kb, descrizione, sorgente, chiave)
        _coda.append(lavoro)
        _avvia_prossimo_bloccato()
    return lavoro


def _avvia_prossimo_bloccato():
    """Fa partire il primo lavoro in coda, se nessuno è in corso. Va chiamata
    tenendo ``_condizione``."""
    global _corrente
    if _corrente is not None and _corrente.stato == IN_CORSO:
        return
    if not _coda:
        return
    lavoro = _coda.pop(0)
    lavoro.stato = IN_CORSO
    lavoro.iniziato = time.time()
    _corrente = lavoro
    _condizione.notify_all()
    threading.Thread(target=_esegui, args=(lavoro,),
                     name=f"kb-lavoro-{lavoro.id}", daemon=True).start()


def _esegui(lavoro: Lavoro):
    from .models import KnowledgeBase

    _locale.lavoro = lavoro
    eventi = None
    try:
        # la knowledge si ricarica qui, nel thread: l'istanza ricevuta
        # apparteneva a un'altra richiesta e potrebbe essere cambiata
        kb = KnowledgeBase.objects.get(pk=lavoro.kb_pk)
        eventi = lavoro.sorgente(kb)
        for evento in eventi:
            with _condizione:
                lavoro.registra(evento)
                if evento.get("type") == "done":
                    lavoro.stats = evento.get("stats")
                _condizione.notify_all()
            # un evento arriva a passo concluso (documento salvato): è il
            # punto in cui fermarsi senza lasciare niente a metà. Il «done»
            # no: il lavoro è finito comunque.
            if lavoro.fermo.is_set() and evento.get("type") != "done":
                raise Fermato
    except Fermato:
        with _condizione:
            lavoro.stato = FERMATO
            ultimo = next((e for e in reversed(lavoro.eventi) if e.get("type") == "progress"), {})
            lavoro.registra({"type": "stopped", "done": ultimo.get("done", 0),
                             "total": lavoro.evento_start.get("total", 0)})
    except Exception as e:                                # noqa: BLE001
        with _condizione:
            lavoro.stato = ERRORE
            lavoro.errore = str(e)
            lavoro.registra({"type": "error", "error": str(e)})
    else:
        with _condizione:
            lavoro.stato = FINITO
    finally:
        # un generatore lasciato a metà va chiuso qui, nel suo thread: i suoi
        # ``with`` (il lock dell'indicizzazione) si sciolgono adesso e non
        # quando lo raccoglie il garbage collector
        if eventi is not None and hasattr(eventi, "close"):
            eventi.close()
        _locale.lavoro = None
        lavoro.finito = time.time()
        close_old_connections()
        with _condizione:
            _condizione.notify_all()
            _avvia_prossimo_bloccato()


def segui(lavoro: Lavoro, da: int = 0):
    """Eventi del lavoro a partire dal numero ``da``, man mano che arrivano.

    Il generatore finisce quando il lavoro finisce. Interromperlo (browser
    chiuso) non tocca il lavoro, che prosegue per conto suo."""
    indice = da
    while True:
        with _condizione:
            while indice >= lavoro.n_eventi and lavoro.attivo:
                # il timeout serve a non restare appesi se il thread muore in
                # un modo che non riesce a notificare
                _condizione.wait(timeout=5)
            # I numeri d'evento sono assoluti, la finestra tiene solo gli
            # ultimi: chi si riattacca dopo una pausa lunga riparte dal più
            # vecchio ancora disponibile invece di chiedere eventi buttati via.
            primo = lavoro.n_eventi - len(lavoro.eventi)
            if indice < primo:
                indice = primo
            nuovi = list(lavoro.eventi)[indice - primo:]
            indice = lavoro.n_eventi
            concluso = not lavoro.attivo
        for evento in nuovi:
            yield evento
        if concluso and indice >= lavoro.n_eventi:
            return


def attendi(lavoro: Lavoro, timeout: float | None = None) -> dict | None:
    """Aspetta che il lavoro finisca e ne ritorna le statistiche.

    Serve a chi deve *usare* l'esito — la sincronizzazione della posta, che
    riferisce quanti messaggi sono stati indicizzati — invece di guardarlo."""
    scadenza = None if timeout is None else time.time() + timeout
    with _condizione:
        while lavoro.attivo:
            resto = None if scadenza is None else max(0.0, scadenza - time.time())
            if resto == 0.0:
                break
            _condizione.wait(timeout=5 if resto is None else min(5, resto))
    if lavoro.stato == ERRORE:
        raise RuntimeError(lavoro.errore)
    return lavoro.stats
