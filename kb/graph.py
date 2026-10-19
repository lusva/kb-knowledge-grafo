"""Grafo locale dei documenti, dei temi, dei tag e delle persone dei file EML.

Ogni documento è visibile fin dal primo caricamento. I collegamenti espliciti
Markdown e i tag uniscono i file; i temi aggiungono il raggruppamento semantico.
"""
import re
import threading
from collections import Counter
from pathlib import PurePosixPath

from django.db.models import Count, Max

from .models import Document

# Domini di posta personale: sono provider, non organizzazioni con cui si ha
# a che fare. Un nodo "gmail.com" collegato a mezzo archivio non dice nulla.
DOMINI_GENERICI = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.it", "outlook.com",
    "outlook.it", "live.com", "live.it", "yahoo.com", "yahoo.it", "libero.it",
    "alice.it", "tiscali.it", "virgilio.it", "icloud.com", "me.com", "pec.it",
    "aol.com", "protonmail.com", "fastwebnet.it", "tin.it",
}

# Indirizzi che nessuno legge: notifiche, rimbalzi, newsletter.
AUTOMATICI = re.compile(
    r"no-?reply|do-?not-?reply|"
    r"^(mailer-daemon|postmaster|bounces?|newsletters?|notifich[ea]|notifications?|alerts?)"
    r"([-._+].*)?$", re.I)

# Caselle di ruolo: "info" da solo non dice chi è, "info@csmt.it" sì.
CASELLE_DI_RUOLO = {
    "info", "admin", "amministrazione", "support", "supporto", "assistenza",
    "contatti", "contact", "sales", "commerciale", "segreteria", "office",
    "ufficio", "hello", "mail", "posta", "ordini", "acquisti", "fatture",
    "billing", "comunicazioni", "contratti", "app",
}

MIN_DOCUMENTI = 3         # sotto: non è un'entità, è un caso isolato
QUOTA_ONNIPRESENTE = 0.4  # sopra questa frazione di documenti: è la casella stessa
TEMI_PER_PERSONA = 3      # archi persona-tema: solo i temi dove compare di più

# Ultimo grafo costruito per knowledge, con la firma dell'archivio da cui è
# nato: si ricostruisce solo quando cambia l'indice o cambiano i temi.
_cache: dict[int, tuple[tuple, dict]] = {}
_cache_lock = threading.Lock()


def _firma(kb) -> tuple:
    """Ciò da cui dipende il grafo: se non cambia, non cambia nemmeno lui."""
    agg = Document.objects.filter(knowledge=kb).aggregate(
        n=Count("id"), ultimo=Max("indexed_at"), tema=Max("topic_at"))
    return (agg["n"], agg["ultimo"], agg["tema"], kb.topics_at)


def build_graph(kb) -> dict:
    firma = _firma(kb)
    with _cache_lock:
        memorizzato = _cache.get(kb.pk)
    if memorizzato and memorizzato[0] == firma:
        return memorizzato[1]
    grafo = _costruisci(list(Document.objects.filter(knowledge=kb)
                             .only("rel_path", "title", "topic", "people", "tags", "links")))
    with _cache_lock:
        _cache[kb.pk] = (firma, grafo)
    return grafo


def _indirizzi(doc) -> set[str]:
    return {a for a in (doc.people or []) if "@" in a}


def _etichette(persone: set[str]) -> dict[str, str]:
    """Il nome prima della chiocciola, oppure l'indirizzo intero quando il nome
    da solo non basta: una casella di ruolo o due "andrea" di aziende diverse."""
    locale = {a: a.split("@")[0] for a in persone}
    ripetuti = Counter(n.lower() for n in locale.values())
    return {a: a if n.lower() in CASELLE_DI_RUOLO or ripetuti[n.lower()] > 1 else n
            for a, n in locale.items()}


def _costruisci(docs) -> dict:
    # quante volte compare ogni indirizzo, e chi resta come persona
    conteggio = Counter(a for d in docs for a in _indirizzi(d))
    massimo = max(1, len(docs)) * QUOTA_ONNIPRESENTE
    persone = {a for a, n in conteggio.items()
               if MIN_DOCUMENTI <= n <= massimo and not AUTOMATICI.search(a.split("@")[0])}

    # un solo passaggio sui documenti: temi, organizzazioni e coppie persona-tema
    per_tema, per_org, persona_tema = Counter(), Counter(), Counter()
    for d in docs:
        if d.topic:
            per_tema[d.topic] += 1
        presenti = _indirizzi(d) & persone
        domini = {a.split("@")[-1] for a in presenti} - DOMINI_GENERICI
        per_org.update(domini)          # un documento conta una volta per organizzazione
        if d.topic:
            persona_tema.update((a, d.topic) for a in presenti)

    organizzazioni = set(per_org)

    nodes = [{"id": f"#tema:{t}", "label": t, "type": "tema", "documenti": n}
             for t, n in sorted(per_tema.items())]
    etichette = _etichette(persone)
    nodes += [{"id": f"#persona:{a}", "label": etichette[a], "type": "persona",
               "titolo": a, "documenti": conteggio[a]} for a in sorted(persone)]
    nodes += [{"id": f"#org:{o}", "label": o, "type": "org", "documenti": per_org[o]}
              for o in sorted(organizzazioni)]

    links = []
    for a in sorted(persone):
        dominio = a.split("@")[-1]
        if dominio in organizzazioni:
            links.append({"source": f"#persona:{a}", "target": f"#org:{dominio}", "type": "org"})
    temi_di = {}
    for (a, tema), n in persona_tema.items():
        if n >= MIN_DOCUMENTI:
            temi_di.setdefault(a, []).append((n, tema))
    for a, temi in sorted(temi_di.items()):
        for n, tema in sorted(temi, reverse=True)[:TEMI_PER_PERSONA]:
            links.append({"source": f"#persona:{a}", "target": f"#tema:{tema}",
                          "type": "tema", "weight": n})
    # I documenti restano visibili anche prima del primo raggruppamento dei temi.
    per_tag = Counter(tag for d in docs for tag in set(d.tags or []))
    nodes += [{"id": f"#tag:{tag}", "label": tag, "type": "tag", "documenti": n}
              for tag, n in sorted(per_tag.items())]
    nodes += [{"id": d.rel_path, "label": d.title, "titolo": d.title,
               "path": d.rel_path, "type": "documento", "documenti": 1}
              for d in docs]
    ids = {n["id"] for n in nodes}
    # Percorsi espliciti prima dei titoli: un titolo ambiguo non crea un arco errato.
    candidati = {}
    for d in docs:
        path = PurePosixPath(d.rel_path)
        for nome in {d.rel_path, str(path.with_suffix("")), path.name, path.stem, d.title}:
            candidati.setdefault(nome.casefold(), set()).add(d.rel_path)
    archi_documenti = set()
    for d in docs:
        target = [(f"#tema:{d.topic}", "tema")] if d.topic else []
        target += [(f"#tag:{tag}", "tag") for tag in set(d.tags or [])]
        target += [(f"#persona:{a}", "persona") for a in _indirizzi(d)]
        for dest, tipo in target:
            if dest in ids:
                links.append({"source": d.rel_path, "target": dest, "type": tipo})
        for nome in d.links or []:
            nome = str(nome).split("|", 1)[0].split("#", 1)[0].strip()
            relativi = str(PurePosixPath(d.rel_path).parent / nome).casefold()
            trovati = candidati.get(relativi) or candidati.get(nome.casefold(), set())
            if len(trovati) == 1:
                dest = next(iter(trovati))
                coppia = tuple(sorted((d.rel_path, dest)))
                if dest != d.rel_path and coppia not in archi_documenti:
                    archi_documenti.add(coppia)
                    links.append({"source": d.rel_path, "target": dest, "type": "link"})
    return {"nodes": nodes, "links": links}


def entita_dei_documenti(kb, rel_paths) -> dict[str, list[str]]:
    """Per ogni documento, i nodi della mappa che lo riguardano (tema, persone,
    organizzazioni): servono a illuminare la mappa sulle fonti di una risposta.

    Gli id possono non essere tutti nella mappa (una persona sotto soglia):
    l'interfaccia considera solo quelli che trova."""
    esito = {}
    for d in Document.objects.filter(knowledge=kb, rel_path__in=set(rel_paths)).only(
            "rel_path", "topic", "people", "tags"):
        ids = [d.rel_path] + ([f"#tema:{d.topic}"] if d.topic else [])
        ids += [f"#tag:{tag}" for tag in d.tags or []]
        for a in sorted(_indirizzi(d)):
            ids += [f"#persona:{a}", f"#org:{a.split('@')[-1]}"]
        esito[d.rel_path] = list(dict.fromkeys(ids))
    return esito


def con_entita(kb, hits: list[dict]) -> list[dict]:
    """Aggiunge a ogni passaggio trovato i nodi della mappa del suo documento."""
    mappa = entita_dei_documenti(kb, [h["doc"] for h in hits])
    for h in hits:
        h["entita"] = mappa.get(h["doc"], [])
    return hits
