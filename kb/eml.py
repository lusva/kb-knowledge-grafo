"""Lettura di messaggi EML locali. Gli allegati vengono estratti accanto al messaggio e indicizzati come documenti separati."""
import hashlib
import re
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from django.conf import settings

from . import allegati, citazioni, config
from .ingest import html_to_text as _html_to_text


def _parse(path: Path):
    """Legge il file .eml. policy.default decodifica da sola intestazioni
    MIME (=?utf-8?B?…?=) e charset del corpo."""
    with open(path, "rb") as f:
        return BytesParser(policy=policy.default).parse(f)


def _body(msg) -> str:
    """Corpo del messaggio: testo semplice, con fallback su HTML ripulito."""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:                      # noqa: BLE001 — messaggio malformato
        part = None
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:                      # noqa: BLE001 — charset dichiarato male
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        return _html_to_text(content)
    return content.strip()


def _data(msg):
    """Data del messaggio come datetime, o None se assente/illeggibile."""
    raw = msg.get("date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _intestazione(msg, key: str, default: str = "") -> str:
    """Valore di un header su una riga sola (gli header lunghi vanno a capo)."""
    valore = msg.get(key)
    return re.sub(r"\s+", " ", str(valore)).strip() if valore else default


def extract_text(path: Path) -> str:
    """Testo indicizzabile di una email .eml: intestazione, corpo, allegati."""
    msg = _parse(path)
    quando = _data(msg)
    oggetto = _intestazione(msg, "subject", "(senza oggetto)")
    righe = [
        f"# {oggetto}",
        "",
        f"**Da:** {_intestazione(msg, 'from', '(mittente sconosciuto)')}  ",
        f"**A:** {_intestazione(msg, 'to')}  ",
    ]
    cc = _intestazione(msg, "cc")
    if cc:
        righe.append(f"**Cc:** {cc}  ")
    if quando:
        righe.append(f"**Data:** {quando.strftime('%Y-%m-%d %H:%M')}  ")
    corpo = _body(msg)
    if config.get("mail_strip_quotes"):
        corpo = citazioni.togli_citazioni(corpo)
    righe += ["", corpo]

    nomi = [a for a in (_nome_allegato(p) for p in _allegati(msg)) if a]
    if nomi:
        righe += ["", "## Allegati", ""] + [f"- {n}" for n in nomi]
    return "\n".join(righe)


def _allegati(msg):
    """Gli allegati veri del messaggio (esclusi loghi e immagini della firma)."""
    try:
        parti = list(msg.iter_attachments())
    except Exception:                      # noqa: BLE001 — struttura MIME rotta
        return []
    fuori = []
    for part in parti:
        nome = _nome_allegato(part)
        if not nome:
            continue
        ext = Path(nome).suffix.lower()
        if ext in settings.KB_IMAGE_EXT:
            # il Content-Id da solo non basta: Gmail lo mette su tutti gli
            # allegati, anche sulle foto allegate apposta
            disposizione = part.get_content_disposition()
            inline = disposizione == "inline" or (
                bool(part.get("content-id")) and disposizione != "attachment")
            dati = part.get_payload(decode=True) or b""
            if allegati.immagine_decorativa(nome, dati, inline=inline):
                continue
        fuori.append(part)
    return fuori


def _nome_allegato(part) -> str:
    """Nome file dell'allegato, ripulito per l'uso su disco."""
    nome = part.get_filename()
    if not nome:
        return ""
    nome = Path(str(nome)).name
    return re.sub(r"[^\w.\-]+", "_", nome).strip("_")


def _firma(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def _marker(out_dir: Path, path: Path) -> Path:
    """File-testimone dell'estrazione di QUESTA email dentro allegati/.

    La cartella è condivisa da tutti i .eml vicini (una per messaggio sarebbe
    ingestibile con centinaia di email), quindi il testimone porta il nome del
    file più un hash, per non collidere fra messaggi omonimi."""
    h = hashlib.sha1(str(path.name).encode("utf-8")).hexdigest()[:10]
    return out_dir / f".{h}.src"


def explode_attachments(path: Path) -> int:
    """Estrae gli allegati di una .eml in ``allegati/`` accanto al messaggio.

    Idempotente: se il file non è cambiato dall'ultima estrazione non fa nulla.
    Salva solo gli allegati con estensione supportata (KB_ALL_EXT), per non
    riempire la cartella di file non indicizzabili.
    Restituisce il numero di allegati scritti in questa esecuzione."""
    out_dir = path.parent / "allegati"
    marker = _marker(out_dir, path)
    firma = _firma(path)
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == firma:
        return 0                            # già estratto per questa versione

    msg = _parse(path)
    scritti = 0
    for i, part in enumerate(_allegati(msg), 1):
        nome = _nome_allegato(part)
        if Path(nome).suffix.lower() not in settings.KB_ALL_EXT:
            continue
        dati = part.get_payload(decode=True)
        if not dati:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        # prefisso dal messaggio di origine: due email possono allegare
        # "contratto.pdf" e devono restare due documenti distinti
        dest = out_dir / f"{path.stem[:60]}__{i:02d}_{nome}"
        dest.write_bytes(dati)
        scritti += 1

    out_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(firma, encoding="utf-8")
    return scritti
