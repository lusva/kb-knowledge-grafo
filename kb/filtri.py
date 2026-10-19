"""Filtri della ricerca interna per tipo, data e nodo del grafo."""
import json
from datetime import datetime, time, timezone

from django.utils import timezone as dj_timezone

from . import metadata

TIPI_VALIDI = {metadata.EMAIL, metadata.DOCUMENTO, metadata.IMMAGINE}
SQL_DOCS = "SELECT value FROM json_each(%s)"


def _data(valore, fine_giornata: bool = False):
    """Interpreta una data dall'interfaccia: "2024-05-03" o un timestamp."""
    if valore in (None, ""):
        return None
    if isinstance(valore, (int, float)) or str(valore).isdigit():
        return int(valore)
    try:
        giorno = datetime.strptime(str(valore)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    ora = time(23, 59, 59) if fine_giornata else time(0, 0, 0)
    # Il giorno è quello dell'orologio di chi guarda, non di Greenwich: con
    # l'ora legale italiana un filtro "dal 3 maggio" partirebbe dalle due del
    # mattino del 3, e le prime due ore resterebbero fuori. Lo stesso fuso che
    # usa temporale.py per "oggi" e "ieri".
    fuso = dj_timezone.get_current_timezone()
    return int(datetime.combine(giorno, ora, tzinfo=fuso).timestamp())


def _elenco(valore) -> list[str]:
    """Accetta sia una lista sia una stringa separata da virgole."""
    if not valore:
        return []
    if isinstance(valore, str):
        valore = valore.split(",")
    return [str(v).strip() for v in valore if str(v).strip()]


def _elenco_da(sorgente, chiave: str) -> list[str]:
    """Come _elenco, ma su una richiesta GET raccoglie i parametri ripetuti."""
    if hasattr(sorgente, "getlist"):
        valori = sorgente.getlist(chiave)
        if len(valori) > 1:
            return [v.strip() for v in valori if v.strip()]
    return _elenco(sorgente.get(chiave))


def docs_sql(docs) -> tuple[str, list]:
    """Sottoquery e parametro per ``colonna IN (…)`` sull'elenco dei documenti."""
    return SQL_DOCS, [json.dumps(list(docs), ensure_ascii=False)]


def docs_in(docs):
    """La stessa sottoquery come espressione Django: ``rel_path__in=docs_in(...)``."""
    from django.db.models.expressions import RawSQL
    return RawSQL(*docs_sql(docs))


def _documenti_di_entita(sorgente, kb) -> list[str]:
    """Percorsi dei documenti legati a un tema, una persona o un dominio.

    Un tema può contenere centinaia di documenti: farli viaggiare nella URL
    sarebbe assurdo, quindi il client manda il *nome* dell'entità e la
    traduzione in elenco di documenti avviene qui, con una query."""
    from django.db.models import TextField
    from django.db.models.functions import Cast

    from .models import Document

    selezione = {k: str(sorgente.get(k) or "").strip() for k in ("tema", "persona", "org", "tag")}
    entita = str(sorgente.get("entita") or "").strip()
    if kb is None:
        return []
    qs = Document.objects.filter(knowledge=kb)
    if entita:
        prefisso, separatore, valore = entita.lstrip("#").partition(":")
        if entita.startswith("#") and separatore and prefisso in selezione:
            selezione[prefisso] = valore
        else:
            return list(qs.filter(rel_path=entita).values_list("rel_path", flat=True))
    tema, persona, org, tag = (selezione[k] for k in ("tema", "persona", "org", "tag"))
    persona, org = persona.lower(), org.lower()
    if not any(selezione.values()):
        return []
    if tag:
        # json_each mantiene il confronto esatto anche per tag con punteggiatura.
        return [rel for rel, tags in qs.values_list("rel_path", "tags") if tag in (tags or [])]
    if tema:
        qs = qs.filter(topic=tema)
    if persona or org:
        # people è una lista JSON: su SQLite si interroga come testo
        ago = f'"{persona}"' if persona else f'@{org}"'
        qs = qs.annotate(_persone=Cast("people", TextField())).filter(_persone__icontains=ago)
    # Tutti, senza tetto. Prima si tagliava a 800 in ordine alfabetico: il
    # filtro di un'entità più grande restringeva la ricerca a una parte presa a
    # caso dei suoi documenti, senza che niente lo dicesse.
    return list(qs.values_list("rel_path", flat=True))


def da_parametri(sorgente, kb=None) -> dict:
    """Filtri a partire dai parametri di una richiesta (GET o corpo JSON).

    ``kb`` serve solo a tradurre i filtri per entità (tema/persona/org) nei
    documenti corrispondenti.

    Ritorna un dizionario vuoto se non è stato chiesto nessun filtro: i motori
    lo trattano come "nessuna restrizione"."""
    tipi = [t for t in _elenco_da(sorgente, "tipi") if t in TIPI_VALIDI]
    docs = _elenco_da(sorgente, "docs") or _documenti_di_entita(sorgente, kb)
    da = _data(sorgente.get("da"))
    a = _data(sorgente.get("a"), fine_giornata=True)
    filtri = {}
    if tipi:
        filtri["tipi"] = tipi
    if docs or any(sorgente.get(k) for k in ("entita", "tema", "persona", "org", "tag")):
        filtri["docs"] = docs
    if da:
        filtri["da"] = da
    if a:
        filtri["a"] = a
    return filtri


def descrizione(filtri: dict) -> str:
    """Riassunto leggibile dei filtri attivi (per i messaggi dell'interfaccia)."""
    if not filtri:
        return ""
    parti = []
    if filtri.get("tipi"):
        parti.append(", ".join(metadata.ETICHETTE.get(t, t) for t in filtri["tipi"]))
    if filtri.get("da") or filtri.get("a"):
        def giorno(ts):
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d/%m/%Y")
        if filtri.get("da") and filtri.get("a"):
            parti.append(f"dal {giorno(filtri['da'])} al {giorno(filtri['a'])}")
        elif filtri.get("da"):
            parti.append(f"dal {giorno(filtri['da'])}")
        else:
            parti.append(f"fino al {giorno(filtri['a'])}")
    if filtri.get("docs"):
        n = len(filtri["docs"])
        parti.append(f"{n} document{'o' if n == 1 else 'i'} selezionat{'o' if n == 1 else 'i'}")
    if filtri.get("etichetta"):
        parti.append(filtri["etichetta"])
    return " · ".join(parti)
