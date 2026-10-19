"""Temi: raggruppamento dei documenti per argomento, e sua manutenzione.

Un tema non si chiede a un modello documento per documento — con migliaia di
documenti sarebbero migliaia di generazioni, ore di attesa, e un vocabolario
pieno di quasi-sinonimi (fattura / fatture / fatturazione) che nel grafo
diventano nodi diversi per la stessa cosa. I gruppi si ricavano invece dai
**vettori medi già salvati** durante l'indicizzazione: un secondo di calcolo, e
un vocabolario chiuso e coerente per costruzione. Al modello si chiede solo il
nome di ogni gruppo: una domanda per tema, mezzo minuto in tutto.

Tre operazioni, che insieme tengono i temi vivi senza che nessuno se ne occupi:

``raggruppa``           rifà i gruppi da zero e dà loro un nome. È l'unico modo
                        in cui un tema *nuovo* può nascere.
``assegna_nuovi``       dà un tema ai documenti appena arrivati copiandolo dal
                        più simile: istantaneo, ma non inventa temi nuovi.
``dopo_indicizzazione`` ciò che accade da solo dopo ogni indicizzazione: il
                        secondo sempre, il primo quando l'archivio è cambiato
                        abbastanza da giustificarlo.
"""
import logging
import math
import re
from collections import Counter

import numpy as np
from django.utils import timezone

from .models import Document

log = logging.getLogger("kb.temi")

# Parole che nei titoli non distinguono niente (prefissi di risposta, rumore
# dei nomi file generati dai connettori).
VUOTE = {
    "re", "r", "fwd", "fw", "i", "il", "lo", "la", "le", "gli", "un", "una", "di",
    "del", "della", "delle", "dei", "degli", "da", "dal", "in", "nel", "con", "per",
    "su", "al", "alla", "allo", "ai", "e", "ed", "o", "che", "tuo", "tua",
    "your", "the", "for", "and", "to", "of", "you", "we", "is", "are", "gmail",
    "com", "it", "pdf", "docx", "md", "allegato", "allegati", "msg", "mail", "email",
}

MIN_DOCUMENTI = 8        # sotto, raggruppare non ha senso
SOGLIA = 0.30            # vicinanza minima assoluta al centro del gruppo
SIGMA = 1.5              # e minima rispetto ai compagni di gruppo
SOGLIA_EREDITA = 0.55    # vicinanza minima per ereditare il tema da un simile
# Quanti temi: uno ogni ~30 documenti. Con uno ogni 150 (com'era) 562 documenti
# davano 4 temi, e uno raccoglieva 158 documenti di clienti diversi sotto il
# nome di uno solo; da ~16 in su i gruppi si separano meglio e restano stabili
# cambiando il seme. Il tetto tiene leggibile la mappa.
DOCUMENTI_PER_TEMA = 30
MIN_TEMI, MAX_TEMI = 4, 40
# Quota dell'archivio oltre la quale i temi si rifanno comunque, anche sotto la
# soglia "ogni N documenti": una cartella nuova che pesa un quarto dell'archivio
# porta quasi sempre argomenti che nei temi di prima non c'erano, e per
# ereditarietà potrebbe solo finire senza tema.
QUOTA_CAMBIATI = 0.25


# --------------------------------------------------------------------------- #
# Raggruppamento
# --------------------------------------------------------------------------- #

def raggruppa(kb, **opzioni) -> list[dict]:
    """Rifà i temi della knowledge da zero. Ritorna un riepilogo per gruppo.
    Le opzioni sono quelle di ``raggruppa_iter``."""
    for evento in raggruppa_iter(kb, **opzioni):
        if evento["type"] == "done":
            return evento["stats"]["gruppi"]
    return []


def raggruppa_iter(kb, gruppi: int = 0, soglia: float = SOGLIA, sigma: float = SIGMA,
                   senza_modello: bool = False, seed: int = 0):
    """Rifà i temi un passo alla volta, con un evento per ogni tema a cui dà un
    nome: è la parte lenta, una domanda al modello per tema (qualche secondo
    ciascuna). Gli eventi sono gli stessi della reindicizzazione (start,
    progress, done), così la coda dei lavori e la barra dell'interfaccia li
    mostrano senza distinzioni.

    I nomi li sceglie il modello di chat, che a differenza delle parole dei
    titoli capisce di cosa parla il gruppo («Bollette Luce e Gas» invece di
    «Luce Bolletta»). ``senza_modello`` torna alle parole dei titoli; ci si
    torna comunque, tema per tema, se il modello non risponde."""
    docs = [d for d in Document.objects.filter(knowledge=kb)
            if d.embedding is not None and len(d.embedding)]
    if len(docs) < MIN_DOCUMENTI:
        yield {"type": "start", "total": 0, "label": "Troppo pochi documenti per fare temi"}
        yield {"type": "done", "stats": {"gruppi": [], "temi": [], "senza_tema": 0}}
        return

    k = gruppi or max(MIN_TEMI, min(MAX_TEMI, round(len(docs) / DOCUMENTI_PER_TEMA)))
    yield {"type": "start", "total": k, "label": "Do un nome ai temi…"}
    etichette, vicinanza = _kmeans(docs, k, seed)

    # quanto è comune ogni parola nell'intera knowledge: serve a capire quali
    # parole *distinguono* un gruppo dagli altri
    globale = Counter()
    for d in docs:
        globale.update(set(_parole_titolo(d)))

    riepilogo, usati = [], set()
    for gruppo in range(k):
        indici = np.flatnonzero(etichette == gruppo)
        if not len(indici):
            continue
        # i più vicini al centro descrivono il gruppo meglio degli altri
        rappresentativi = indici[np.argsort(-vicinanza[indici])][:12]
        nome = _distingui(_nome(docs, rappresentativi, senza_modello, globale, len(docs)),
                          usati, docs, indici)
        # un documento resta senza tema se è lontano dal centro in assoluto
        # oppure rispetto ai suoi compagni di gruppo
        minimo = soglia
        if sigma and len(indici) > 4:
            minimo = max(minimo, vicinanza[indici].mean() - sigma * vicinanza[indici].std())
        tenuti = 0
        for i in indici:
            docs[i].topic = nome if vicinanza[i] >= minimo else ""
            tenuti += int(vicinanza[i] >= minimo)
        riepilogo.append({"nome": nome, "tenuti": tenuti, "totale": int(len(indici)),
                          "vicinanza": float(vicinanza[indici].mean())})
        yield {"type": "progress", "done": gruppo + 1, "total": k, "file": nome, "action": "tema"}

    # si salva tutto alla fine: interrotto a metà, l'archivio resta con i temi di prima
    adesso = timezone.now()
    for d in docs:
        d.topic_at = adesso              # valutati tutti, anche quelli lasciati senza
    Document.objects.bulk_update(docs, ["topic", "topic_at"], batch_size=200)
    kb.topics_at = adesso
    kb.save(update_fields=["topics_at"])
    yield {"type": "done", "stats": {
        "gruppi": riepilogo,
        "temi": [r["nome"] for r in riepilogo],
        "senza_tema": sum(r["totale"] - r["tenuti"] for r in riepilogo),
    }}


def _kmeans(docs, k: int, seed: int):
    """k-means sferico sui vettori medi: etichetta e vicinanza di ognuno."""
    X = np.asarray([d.embedding for d in docs], dtype=np.float32)
    X /= np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-9)
    rng = np.random.default_rng(seed)
    centri = X[rng.choice(len(X), k, replace=False)].copy()
    etichette = np.zeros(len(X), dtype=int)
    for passaggio in range(30):
        nuove = (X @ centri.T).argmax(1)
        if passaggio and (nuove == etichette).all():
            break
        etichette = nuove
        for gruppo in range(k):
            membri = X[etichette == gruppo]
            if len(membri):
                media = membri.mean(0)
                centri[gruppo] = media / max(np.linalg.norm(media), 1e-9)
    return etichette, (X @ centri.T).max(1)


# --------------------------------------------------------------------------- #
# Nomi dei gruppi
# --------------------------------------------------------------------------- #

def _parole_titolo(doc) -> list[str]:
    """Parole utili nel titolo di un documento (senza prefissi e codici)."""
    titolo = re.sub(r"^\d{6,}_|__[0-9a-f]{6,}$|^[0-9a-f]{8,}_", " ", doc.title)
    return [p for p in re.findall(r"[A-Za-zÀ-ÿ]{3,}", titolo.lower()) if p not in VUOTE]


def _parole(docs, indici, globale=None, totale=0) -> list[str]:
    """Le parole che descrivono il gruppo, prese dai titoli.

    Il punteggio è TF-IDF: quante volte la parola ricorre nel gruppo, pesato per
    quanto è rara nell'archivio. Servono entrambe le metà — la sola frequenza dà
    nomi generici ("Documenti"), la sola distintività dà nomi marginali ("Mese
    Maggio" al posto di "Fatture")."""
    conta = Counter()
    for i in indici:
        conta.update(set(_parole_titolo(docs[i])))
    if not conta:
        return []
    n = len(indici)
    candidate = {p: q for p, q in conta.items() if q >= max(2, n * 0.15)}
    if not candidate:
        return [p for p, _ in conta.most_common(6)]
    if not (globale and totale):
        return sorted(candidate, key=lambda p: -candidate[p])[:6]
    punteggio = {p: q * math.log(totale / max(globale.get(p, 1), 1))
                 for p, q in candidate.items()}
    return sorted(punteggio, key=lambda p: -punteggio[p])[:6]


def _distingui(nome: str, usati: set, docs, indici) -> str:
    """Evita due temi con lo stesso nome: nel grafo sarebbero un nodo solo."""
    if nome.lower() not in usati:
        usati.add(nome.lower())
        return nome
    for parola in _parole(docs, indici):
        candidato = f"{nome} · {parola.capitalize()}"
        if candidato.lower() not in usati and parola.lower() not in nome.lower():
            usati.add(candidato.lower())
            return candidato[:80]
    n = 2
    while f"{nome} {n}".lower() in usati:
        n += 1
    usati.add(f"{nome} {n}".lower())
    return f"{nome} {n}"[:80]


PROMPT_NOME = (
    "Ricevi i titoli di un gruppo di documenti simili tra loro. Rispondi con "
    "una sola etichetta in italiano di 1-4 parole che descriva l'argomento "
    "comune, adatta a essere usata come categoria. Se i documenti riguardano "
    "un'azienda, un cliente o un progetto preciso, metti il suo nome "
    "nell'etichetta (es. «Collaudi Meridiana», non «Collaudi»). "
    "Solo l'etichetta, senza punteggiatura né commenti."
)


def _nome(docs, indici, senza_modello: bool, globale=None, totale=0) -> str:
    parole = _parole(docs, indici, globale, totale)
    ripiego = " ".join(p.capitalize() for p in parole[:2]) or "Varie"
    if senza_modello:
        return ripiego[:80]

    from . import ollama

    titoli = "\n".join(
        f"- {re.sub(r'^[0-9a-f]{8,}_|^\\d{6,}_', '', docs[i].title)[:80]}" for i in indici)
    messaggi = [{"role": "system", "content": PROMPT_NOME},
                {"role": "user", "content": f"TITOLI:\n{titoli}\n\nETICHETTA:"}]
    try:
        # generoso di proposito: i modelli "thinking" spendono la prima parte
        # della generazione a ragionare, e con un tetto basso la risposta non
        # arriverebbe mai
        risposta = ollama.chat_once(messaggi, timeout=120, num_predict=900)
    except ollama.OllamaError as e:
        log.warning("nome del tema dal modello non disponibile (%s): uso i titoli", e)
        return ripiego[:80]
    righe = [r.strip(" .:-\"'«»") for r in risposta.splitlines() if r.strip()]
    nome = righe[-1] if righe else ""
    if not nome or len(nome) > 60 or len(nome.split()) > 5:
        return ripiego[:80]
    return nome[:80]


# --------------------------------------------------------------------------- #
# Manutenzione automatica
# --------------------------------------------------------------------------- #

# Le sole colonne che servono per i temi: senza questo elenco ogni documento
# arriva con testo e anteprima al seguito.
_CAMPI_TEMA = ("id", "topic", "topic_at", "embedding")


def _vettori_etichettati(kb):
    """Temi e vettori dei documenti già etichettati, letti a blocchi.

    Ritorna la lista dei temi e la matrice normalizzata delle righe
    corrispondenti, allineate per posizione."""
    righe = Document.objects.filter(knowledge=kb).exclude(topic="")
    capienza = righe.count()
    if not capienza:
        return [], np.empty((0, 0), dtype=np.float32)

    temi, matrice, usate = [], None, 0
    for d in righe.only(*_CAMPI_TEMA).iterator(chunk_size=500):
        if d.embedding is None or not len(d.embedding):
            continue
        if matrice is None:
            matrice = np.empty((capienza, len(d.embedding)), dtype=np.float32)
        if len(d.embedding) != matrice.shape[1]:
            continue      # vettore di un modello diverso: non confrontabile
        matrice[usate] = d.embedding
        temi.append(d.topic)
        usate += 1
    if not usate:
        return [], np.empty((0, 0), dtype=np.float32)
    return temi, _normalizza(matrice[:usate])


def assegna_nuovi(kb, soglia: float = SOGLIA_EREDITA) -> int:
    """Dà un tema ai documenti nuovi copiandolo dal documento più simile.

    Un confronto di vettori, nessun modello, millisecondi: è ciò che farebbe il
    raggruppamento per un punto vicino a un gruppo esistente. Non tocca i
    documenti che il raggruppamento ha valutato e lasciato senza tema di
    proposito (li riconosce da ``topic_at``): quelli devono restare senza,
    altrimenti la garanzia "meglio nessuna etichetta che una sbagliata"
    salterebbe al primo arrivo di posta."""
    # Questa funzione gira alla fine di ogni indicizzazione, e l'aggiornamento
    # automatico della posta ne fa una ogni pochi minuti: quasi sempre non c'è
    # niente da etichettare. Chiederlo prima costa una query e risparmia la
    # lettura in memoria degli embedding di tutto l'archivio.
    candidati = Document.objects.filter(knowledge=kb, topic="", topic_at__isnull=True)
    if not candidati.exists():
        return 0
    orfani = [d for d in candidati.only(*_CAMPI_TEMA).iterator(chunk_size=500)
              if d.embedding is not None and len(d.embedding)]
    if not orfani:
        return 0

    # Degli etichettati serve solo il tema e il vettore: tenerne gli oggetti
    # Document significa trascinarsi dietro titolo, percorso e anteprima di
    # ogni documento dell'archivio. Il vettore va dritto in una matrice
    # float32, un quarto dello spazio della lista di float di Python.
    temi_noti, noti = _vettori_etichettati(kb)
    if not temi_noti:
        return 0

    nuovi = _normalizza(np.asarray([d.embedding for d in orfani], dtype=np.float32))
    somiglianze = nuovi @ noti.T
    vicini, migliori = somiglianze.argmax(1), somiglianze.max(1)

    adesso = timezone.now()
    da_salvare = []
    for i, doc in enumerate(orfani):
        if migliori[i] >= soglia:
            doc.topic = temi_noti[vicini[i]]
            doc.topic_at = adesso
            da_salvare.append(doc)
    if da_salvare:
        Document.objects.bulk_update(da_salvare, ["topic", "topic_at"], batch_size=200)
        log.info("%d nuovi documenti etichettati per somiglianza", len(da_salvare))
    return len(da_salvare)


def da_raggruppare(kb) -> int:
    """Quanti documenti sono cambiati dall'ultimo raggruppamento.

    Se sono tanti, i gruppi calcolati allora non descrivono più l'archivio di
    adesso: un cliente nuovo o un progetto nuovo non hanno un tema in cui
    stare, perché per ereditarietà si può solo entrare in temi già esistenti."""
    documenti = Document.objects.filter(knowledge=kb)
    if kb.topics_at is None:
        return documenti.count()
    return documenti.filter(indexed_at__gt=kb.topics_at).count()


def soglia_raggruppamento(kb, ogni: int) -> int:
    """Quanti documenti cambiati servono perché i temi si rifacciano da soli.

    Al massimo ``ogni`` (la configurazione), ma meno quando l'archivio è
    piccolo: con la sola soglia fissa una knowledge di 40 documenti non
    avrebbe mai temi, perché i nuovi possono solo ereditarli da temi che
    esistono già. Il primo raggruppamento parte appena ci sono abbastanza
    documenti; i successivi quando i cambiati sono una quota dell'archivio."""
    totale = Document.objects.filter(knowledge=kb).count()
    if kb.topics_at is None:
        return min(ogni, MIN_DOCUMENTI)
    return min(ogni, max(MIN_DOCUMENTI, math.ceil(totale * QUOTA_CAMBIATI)))


def dopo_indicizzazione(kb) -> dict:
    """Manutenzione dei temi dopo un'indicizzazione. Ritorna cosa ha fatto.

    Il raggruppamento completo scatta da solo quando i documenti cambiati
    superano la soglia in configurazione ("Rifai i temi ogni N documenti",
    0 = mai), o prima se l'archivio è piccolo (``soglia_raggruppamento``):
    è l'unico modo perché nascano temi nuovi. Costa circa mezzo
    minuto, quasi tutto per i nomi chiesti al modello."""
    from . import config

    esito = {"ereditati": assegna_nuovi(kb)}

    ogni = config.get("temi_auto")
    if ogni < 1:
        return esito
    cambiati = da_raggruppare(kb)
    soglia = soglia_raggruppamento(kb, ogni)
    if cambiati < soglia:
        esito["mancano"] = soglia - cambiati
        return esito

    log.info("%s: %d documenti cambiati dall'ultimo raggruppamento, rifaccio i temi",
             kb.name, cambiati)
    riepilogo = raggruppa(kb)
    esito["raggruppati"] = len(riepilogo)
    esito["temi"] = [r["nome"] for r in riepilogo]
    log.info("%s: %d temi — %s", kb.name, len(riepilogo),
             ", ".join(r["nome"] for r in riepilogo))
    return esito


def _normalizza(matrice: np.ndarray) -> np.ndarray:
    norme = np.linalg.norm(matrice, axis=1, keepdims=True)
    return matrice / np.maximum(norme, 1e-9)
