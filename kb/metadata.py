"""Metadati ricavati dai file locali: tipo, data e intestazioni EML."""
import re
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

EMAIL = "email"
DOCUMENTO = "documento"
IMMAGINE = "immagine"
ETICHETTE = {EMAIL: "Messaggi EML", DOCUMENTO: "Documenti", IMMAGINE: "Immagini"}
_INDIRIZZO = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
MAX_PERSONE = 20


def indirizzi(*intestazioni: str) -> list[str]:
    """Indirizzi presenti nelle intestazioni, senza duplicati."""
    trovati = []
    for testo in intestazioni:
        for indirizzo in _INDIRIZZO.findall(testo or ""):
            indirizzo = indirizzo.lower().strip(".")
            if indirizzo not in trovati:
                trovati.append(indirizzo)
    return trovati[:MAX_PERSONE]


def _data_file(path: Path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def estrai(path: Path, ext: str, testo: str) -> dict:
    """I metadati riguardano il file caricato; nessuna fonte esterna viene consultata."""
    tipo = IMMAGINE if ext in settings.KB_IMAGE_EXT else DOCUMENTO
    data, autore, persone, titolo = None, "", [], ""
    if ext in settings.KB_EML_EXT:
        from . import eml
        tipo = EMAIL
        msg = eml._parse(path)
        data = eml._data(msg)
        autore = eml._intestazione(msg, "from")
        persone = indirizzi(autore, eml._intestazione(msg, "to"), eml._intestazione(msg, "cc"))
        titolo = eml._intestazione(msg, "subject")
    return {"data": data or _data_file(path), "autore": autore[:200], "tipo": tipo,
            "persone": persone, "titolo": titolo[:255], "fonte_data": "email" if data else "file"}
