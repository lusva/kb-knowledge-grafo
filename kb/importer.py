"""Import di file o cartelle dal disco dentro la cartella di una knowledge.

L'utente indica un percorso qualsiasi sul proprio Mac; i file supportati
vengono COPIATI in documents/<slug>/ (la struttura delle sottocartelle è
preservata). La successiva reindicizzazione li aggiunge alla knowledge.

Reimportare un file con lo stesso nome ne aggiorna la copia: se il contenuto
è cambiato la sostituisce (la versione vecchia va nel Cestino), se è identico
lo salta. Prima un file cambiato diventava «nome (1).pdf», e la knowledge
rispondeva con la versione vecchia e la nuova insieme.

Loghi e immagini di firma vengono scartati come nella posta (vedi
``allegati.immagine_decorativa``): importando una cartella di materiale
esportato dalla posta ce ne sono a decine, e riempiono la knowledge di
documenti inutili. Non spariscono in silenzio: il conteggio "decorativi"
torna all'interfaccia insieme a copiati, sostituiti e saltati.

Allo stesso modo i file che il filtro della knowledge (o «Estensioni escluse»
di /config) tiene fuori non vengono copiati: finiscono nel conteggio "esclusi".
Nelle cartelle escluse per nome (node_modules e simili, «Cartelle escluse» e
filtro della knowledge) l'import non entra nemmeno: si contano le cartelle
saltate, non i file che contengono.
"""
import filecmp
import shutil
from pathlib import Path

from django.conf import settings

from . import allegati, config, elimina


class ImportError_(Exception):
    """Errore di import mostrabile all'utente."""


class Escluso(ImportError_):
    """Il file è di un tipo che il filtro della knowledge tiene fuori."""


class CartellaEsclusa(Escluso):
    """La cartella ha un nome che il filtro tiene fuori (node_modules…)."""


def _copia(src: Path, dest: Path) -> str:
    """Copia ``src`` in ``dest`` e dice com'è andata: "copiati" se non c'era,
    "saltati" se c'è già identico, "sostituiti" se c'era con lo stesso nome e
    un contenuto diverso.

    La copia nuova si scrive accanto prima di togliere la vecchia: se la
    lettura del file di partenza fallisce, la knowledge resta com'era."""
    if not dest.exists():
        shutil.copy2(src, dest)
        return "copiati"
    if dest.is_dir():
        raise ImportError_(f"C'è già una cartella con questo nome: {dest.name}")
    if filecmp.cmp(src, dest, shallow=False):
        return "saltati"
    nuova = dest.with_name(dest.name + ".importa")   # estensione che nessuno indicizza
    shutil.copy2(src, nuova)
    elimina.al_cestino(dest)
    nuova.replace(dest)
    return "sostituiti"


def _decorativa(path: Path) -> bool:
    """True se il file è un'immagine di firma/logo, da non importare."""
    return (path.suffix.lower() in settings.KB_IMAGE_EXT
            and allegati.immagine_decorativa(path.name, path))


def _esito() -> dict:
    return {"copiati": 0, "sostituiti": 0, "saltati": 0, "ignorati": 0,
            "decorativi": 0, "esclusi": 0, "cartelle_escluse": 0, "cartella": False}


def _piano_di(kb, raw_path: str, escluse: set[str], cartelle: set[str],
              cercando=None) -> tuple[list, dict]:
    """Cosa copiare da un file o da una cartella: [(file, destinazione)] e i
    conteggi di quel che resta fuori già da ora (file e cartelle esclusi dal
    filtro). Non copia niente. Solleva ImportError_ per input non validi.

    ``cercando(n)``: chiamata ogni tanto mentre si scorre una cartella, con
    quanti file da copiare sono stati trovati fin lì."""
    src = Path(raw_path).expanduser()
    try:
        src = src.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ImportError_(f"Percorso inesistente: {raw_path}") from None

    docs_root = settings.KB_DOCS_DIR.resolve()
    # non importare la cartella dei documenti dentro sé stessa
    if src == docs_root or src in docs_root.parents:
        raise ImportError_("Non puoi importare la cartella documents/ o una sua antenata.")

    dest_root = kb.docs_dir
    esito = _esito()

    if src.is_file():
        if src.suffix.lower() not in settings.KB_ALL_EXT:
            raise ImportError_(f"Formato non supportato: {src.suffix or '(nessuna estensione)'}")
        if src.suffix.lower() in escluse:
            raise Escluso(f"I file {src.suffix.lower()} sono esclusi dal filtro di «{kb.name}».")
        return [(src, dest_root / src.name)], esito

    if src.is_dir():
        # l'indicizzazione non la leggerebbe: documents/<slug>/node_modules/…
        if src.name.lower() in cartelle:
            raise CartellaEsclusa(f"La cartella {src.name} è esclusa dal filtro di «{kb.name}».")
        esito["cartella"] = True
        base = dest_root / src.name  # documents/<slug>/<nome cartella>/…
        voci, saltate = [], []
        for visti, path in enumerate(config.scorri(src, cartelle, saltate), 1):
            if cercando and visti % 500 == 0:
                cercando(len(voci))
            if path.suffix.lower() not in settings.KB_ALL_EXT or not path.is_file():
                continue
            if path.suffix.lower() in escluse:
                esito["esclusi"] += 1
                continue
            voci.append((path, base / path.relative_to(src)))
        esito["cartelle_escluse"] = len(saltate)
        if not voci:
            if saltate and not esito["esclusi"]:
                nomi = ", ".join(sorted({c.name for c in saltate}))
                raise ImportError_(f"Nella cartella ci sono file da importare solo dentro "
                                   f"cartelle escluse dal filtro ({nomi}).")
            if esito["esclusi"]:
                raise ImportError_(f"Nella cartella ci sono solo file esclusi dal filtro di «{kb.name}».")
            raise ImportError_("Nessun file con formato supportato trovato nella cartella.")
        return voci, esito

    raise ImportError_("Il percorso non è né un file né una cartella.")


class Piano:
    """Un'importazione preparata e non ancora eseguita: i file da copiare e i
    conteggi di quel che è già stato scartato. Prepararla è veloce (elenca i
    file, non li legge) e dà subito gli errori; la copia, che su una cartella
    grande dura minuti, la fa poi ``copia`` dando l'avanzamento."""

    def __init__(self, kb, voci: list, esito: dict):
        self.kb = kb
        self.voci = voci
        self.esito = esito


def prepara(kb, raw_paths: list[str], cercando=None) -> Piano:
    """Prepara la copia di più file/cartelle nella knowledge ``kb``.
    ``cercando(n)`` riceve ogni tanto quanti file da copiare ha trovato.

    Con una selezione multipla un singolo file di formato non supportato non
    deve far fallire tutto l'import: viene contato in "ignorati" (o in
    "esclusi", se è il filtro della knowledge a tenerlo fuori; una cartella
    esclusa per nome in "cartelle_escluse"). Se però non resta nulla da
    importare, l'errore risale all'utente."""
    paths = [p for p in (str(p).strip() for p in raw_paths) if p]
    if not paths:
        raise ImportError_("Percorso mancante")
    escluse = config.estensioni_escluse(kb)
    cartelle = config.cartelle_escluse(kb)

    # Ogni cartella finisce in documents/<slug>/<suo nome>/: due cartelle scelte
    # insieme con lo stesso nome (le «Fatture» di due clienti) si mescolerebbero,
    # e i file omonimi dell'una sostituirebbero quelli dell'altra.
    nomi: dict[str, str] = {}
    for path in paths:
        src = Path(path).expanduser()
        if src.is_dir():
            nome = src.resolve().name.lower()
            if nome in nomi and nomi[nome] != str(src.resolve()):
                raise ImportError_(f"Hai scelto due cartelle che si chiamano «{src.name}»: "
                                   "finirebbero nella stessa cartella della knowledge. "
                                   "Rinominane una o importale separatamente.")
            nomi[nome] = str(src.resolve())

    voci, totale, errori = [], _esito(), []
    for path in paths:
        try:
            gia = len(voci)
            v, esito = _piano_di(kb, path, escluse, cartelle,
                                 cercando and (lambda n, gia=gia: cercando(gia + n)))
        except ImportError_ as e:
            if len(paths) == 1:
                raise
            totale["cartelle_escluse" if isinstance(e, CartellaEsclusa)
                   else "esclusi" if isinstance(e, Escluso) else "ignorati"] += 1
            errori.append(str(e))
            continue
        voci += v
        totale["esclusi"] += esito["esclusi"]
        totale["cartelle_escluse"] += esito["cartelle_escluse"]
        totale["cartella"] = totale["cartella"] or esito["cartella"]
    if not voci:
        raise ImportError_(errori[0] if errori else "Nessun file importabile.")
    return Piano(kb, voci, totale)


def copia(piano: Piano, avanza=None) -> dict:
    """Esegue un'importazione preparata. ``avanza(fatti, totale, file, azione)``
    viene chiamata dopo ogni file, per la barra di avanzamento.

    Ritorna {"copiati": n, "sostituiti": n, "saltati": n, "ignorati": n,
    "decorativi": n, "esclusi": n, "cartelle_escluse": n, "cartella": bool}."""
    esito = dict(piano.esito)
    totale = len(piano.voci)
    piano.kb.docs_dir.mkdir(parents=True, exist_ok=True)
    for fatti, (src, dest) in enumerate(piano.voci, 1):
        if _decorativa(src):
            azione = "decorativi"
        else:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                azione = _copia(src, dest)
            except (ImportError_, OSError):
                # una cartella con lo stesso nome, un file sparito o illeggibile:
                # si salta quello e si va avanti con gli altri
                azione = "ignorati"
        esito[azione] += 1
        if avanza:
            avanza(fatti, totale, src.name, azione)
    return esito


def copy_many(kb, raw_paths: list[str]) -> dict:
    """Prepara e copia in un colpo solo, senza avanzamento."""
    return copia(prepara(kb, raw_paths))


def copy_into(kb, raw_path: str) -> dict:
    """Copia un file o una cartella nella knowledge ``kb``."""
    return copy_many(kb, [raw_path])
