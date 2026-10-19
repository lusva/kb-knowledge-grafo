"""Ricerca full-text (parole esatte) sui chunk, con l'indice FTS5 di SQLite.

È la metà "letterale" della ricerca ibrida: trova i passaggi che contengono
davvero i termini della domanda — numeri di fattura, sigle, cognomi, codici
prodotto — dove la ricerca per vettori è cieca perché l'embedding di un codice
non assomiglia a nulla. La graduatoria è BM25 (più negativo = più pertinente,
è la convenzione di SQLite), e viene poi fusa con quella vettoriale in
``retrieval.py``.

L'indice è creato dalla migrazione 0008 e mantenuto allineato da trigger, quindi
qui si legge soltanto. Se la tabella non esiste (SQLite senza FTS5) la funzione
restituisce una lista vuota: la ricerca degrada a sola vettoriale.
"""
import re

from django.db import OperationalError, connection

# Parole troppo comuni per essere discriminanti: tenerle allunga la query senza
# spostare la classifica (BM25 le pesa comunque quasi zero).
_VUOTE = {
    "che", "chi", "come", "cosa", "con", "dei", "del", "della", "delle", "degli",
    "dal", "dalla", "dalle", "dagli", "dove", "gli", "hai", "hanno", "il", "in",
    "la", "le", "lo", "mi", "nel", "nella", "nelle", "negli", "non", "per", "più",
    "qual", "quale", "quali", "quando", "quanto", "sono", "sul", "sulla", "sui",
    "sugli", "tra", "fra", "una", "uno", "di", "da", "al", "ai", "alla", "alle",
    "agli", "ed", "ma", "se", "ci", "ne", "ti", "si", "un", "ha", "ho", "the",
    "and", "for", "with", "from", "this", "that",
}

MAX_TERMINI = 24


def fts_query(testo: str, unione: str = "OR") -> str:
    """Traduce una domanda in scrittura umana in una query FTS5.

    I termini vengono estratti e messi fra virgolette uno per uno: così la
    punteggiatura della domanda ("Quanto costa l'ordine #4471?") non viene letta
    come sintassi FTS, che altrimenti solleverebbe un errore. I termini lunghi
    diventano prefissi della parola intera: ``"fattura"*`` trova «fatturazione»
    ma «fatture» non trova «fattura». Togliere la vocale finale (``"fattur"*``)
    è stato provato e lasciato: «tutte le fatture» trovava ogni chunk che
    nomina una fattura e le fatture vere scendevano, e i cognomi perdevano
    precisione (``"ross"*`` trova «rosso»).
    L'unione è OR: bastano alcuni termini, BM25 premia chi ne contiene di più
    e di più rari."""
    termini = []
    for parola in re.findall(r"\w+", testo.lower(), flags=re.UNICODE):
        if len(parola) < 2 or parola in _VUOTE:
            continue
        parola = parola.replace('"', '""')
        termini.append(f'"{parola}"*' if len(parola) >= 4 else f'"{parola}"')
        if len(termini) >= MAX_TERMINI:
            break
    return f" {unione} ".join(termini)


SQL = """
SELECT c.id, bm25(kb_chunk_fts) AS rank
  FROM kb_chunk_fts
  JOIN kb_chunk c ON c.id = kb_chunk_fts.rowid
  JOIN kb_document d ON d.id = c.document_id
 WHERE kb_chunk_fts MATCH %s
   AND d.knowledge_id = %s
   {filtri}
 ORDER BY rank
 LIMIT %s
"""


def _condizioni(filtri: dict | None) -> tuple[str, list]:
    """Traduce i filtri nella parte WHERE aggiuntiva, con i suoi parametri.

    Le date sono confrontate come testo: SQLite salva i datetime in formato ISO
    UTC, che in ordine alfabetico è anche l'ordine cronologico."""
    if not filtri:
        return "", []
    from datetime import datetime, timezone

    pezzi, parametri = [], []
    if filtri.get("tipi"):
        segnaposto = ", ".join(["%s"] * len(filtri["tipi"]))
        pezzi.append(f"AND d.kind IN ({segnaposto})")
        parametri += list(filtri["tipi"])
    if "docs" in filtri:
        from .filtri import docs_sql
        sottoquery, valori = docs_sql(filtri["docs"])
        pezzi.append(f"AND d.rel_path IN ({sottoquery})")
        parametri += valori
    for chiave, operatore in (("da", ">="), ("a", "<=")):
        if filtri.get(chiave):
            istante = datetime.fromtimestamp(filtri[chiave], tz=timezone.utc)
            pezzi.append(f"AND d.doc_date {operatore} %s")
            # Le date si confrontano come testo, e Django le scrive con i
            # microsecondi: senza la frazione, "fino alle 23:59:59" escluderebbe
            # tutto ciò che è arrivato dentro quell'ultimo secondo.
            frazione = ".999999" if operatore == "<=" else ".000000"
            parametri.append(istante.strftime("%Y-%m-%d %H:%M:%S") + frazione)
    return " ".join(pezzi), parametri


def disponibile() -> bool:
    """True se l'indice full-text esiste in questo database."""
    with connection.cursor() as cur:
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kb_chunk_fts'")
        return cur.fetchone() is not None


def search_ids(kb, query: str, limit: int,
               filtri: dict | None = None) -> list[tuple[int, float]]:
    """Chunk della knowledge che contengono i termini della domanda.

    Ritorna coppie (id del chunk, punteggio BM25) dal più pertinente."""
    match = fts_query(query)
    if not match:
        return []
    dove, parametri = _condizioni(filtri)
    try:
        with connection.cursor() as cur:
            cur.execute(SQL.format(filtri=dove), [match, kb.pk] + parametri + [limit])
            return [(int(r[0]), float(r[1])) for r in cur.fetchall()]
    except OperationalError:
        # indice assente (build di SQLite senza FTS5) o query rifiutata:
        # meglio una risposta con i soli vettori che un errore in faccia
        return []


SQL_DOCUMENTI = """
SELECT DISTINCT d.id
  FROM kb_chunk_fts
  JOIN kb_chunk c ON c.id = kb_chunk_fts.rowid
  JOIN kb_document d ON d.id = c.document_id
 WHERE kb_chunk_fts MATCH %s
   AND d.knowledge_id = %s
   {filtri}
 LIMIT %s
"""


def documenti_con_tutti(kb, query: str, limit: int, filtri: dict | None = None) -> list[int]:
    """Id dei documenti in cui compaiono *tutti* i termini (nello stesso passaggio).

    Serve dove conta la completezza e non la classifica — un grafico sulle
    fatture di un fornitore vuole ogni documento che le nomina, non i più
    somiglianti — quindi i termini sono in AND e non c'è ordinamento."""
    match = fts_query(query, unione="AND")
    if not match:
        return []
    dove, parametri = _condizioni(filtri)
    try:
        with connection.cursor() as cur:
            cur.execute(SQL_DOCUMENTI.format(filtri=dove), [match, kb.pk] + parametri + [limit])
            return [int(r[0]) for r in cur.fetchall()]
    except OperationalError:
        return []


SQL_NOMINATI = """
SELECT DISTINCT d.rel_path
  FROM kb_chunk_fts
  JOIN kb_chunk c ON c.id = kb_chunk_fts.rowid
  JOIN kb_document d ON d.id = c.document_id
 WHERE kb_chunk_fts MATCH %s
   AND d.knowledge_id = %s
   {filtri}
"""


def nomi_query(nomi: list[list[str]]) -> str:
    """La query FTS5 che trova uno dei nomi scritto per intero.

    ``nomi``: le parole di ogni nome, già in minuscolo. Le parole di un nome
    stanno vicine in qualunque ordine («Marco Ferrandi» e il «FERRANDI MARCO»
    nell'intestazione di una fattura) e sono parole intere, non prefissi:
    «marco» non deve trovare «marcolini»."""
    parti = []
    for parole in nomi:
        termini = ['"' + p.replace('"', '""') + '"' for p in parole if p]
        if len(termini) == 1:
            parti.append(termini[0])
        elif termini:
            parti.append(f"NEAR({' '.join(termini)}, {len(termini) + 1})")
    return " OR ".join(parti)


def documenti_con_nomi(kb, nomi: list[list[str]], tipi=()) -> list[str]:
    """Percorsi dei documenti di ``kb`` (dei ``tipi`` dati) che scrivono uno
    dei ``nomi`` per intero, tutti: serve a restringere, non a classificare."""
    match = nomi_query(nomi)
    if not match:
        return []
    dove, parametri = _condizioni({"tipi": list(tipi)} if tipi else None)
    try:
        with connection.cursor() as cur:
            cur.execute(SQL_NOMINATI.format(filtri=dove), [match, kb.pk] + parametri)
            return [r[0] for r in cur.fetchall()]
    except OperationalError:
        return []
