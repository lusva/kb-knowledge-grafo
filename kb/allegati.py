"""Riconoscimento degli allegati decorativi nelle email.

I client di posta incorporano nelle firme loghi, icone e separatori che tecnicamente
sono allegati come gli altri; lo stesso rumore arriva importando cartelle di
materiale esportato dalla posta. Indicizzarli riempie la knowledge di documenti inutili
("logo aziendale su sfondo bianco") che rubano spazio ai risultati veri, e con un
modello di visione attivo costano anche parecchi secondi di descrizione ciascuno.

Il segnale più affidabile non è il nome del file — cambia da client a client — ma
la **dimensione in pixel**: loghi e icone stanno sotto i 300 px di lato, mentre
schermate e foto stanno abbondantemente sopra. Il nome e l'assenza di dimensioni
leggibili servono da conferma.
"""
import io
import re
from pathlib import Path

from django.conf import settings

# Nomi con cui Outlook, Word e i client Apple salvano le immagini incorporate:
# image001.png, immagine002.jpg, logo.gif, signature1.png, firma.png…, e i
# nomi inventati da Outlook e Exchange per le immagini della firma
# (Outlook-4lqhs4cy.png, ATT00001.png), che arrivano come allegati veri
_NOMI_DECORATIVI = re.compile(
    r"^(?:(image|immagine|logo|signature|firma|icon|ole|spacer|banner)[\W_]*\d*"
    r"|outlook-[a-z0-9]{6,12}|att\d{5})$", re.I)

# Immagini più leggere di così sono icone anche quando non riusciamo a leggerne
# le dimensioni (formato esotico, file troncato).
_PESO_TRASCURABILE = 15 * 1024


def _dimensioni(sorgente):
    """Larghezza e altezza dell'immagine, o None se non sono leggibili."""
    try:
        from PIL import Image
        apri = io.BytesIO(sorgente) if isinstance(sorgente, bytes) else sorgente
        with Image.open(apri) as img:
            return img.size          # legge solo l'intestazione, non decodifica
    except Exception:                # noqa: BLE001 — formato ignoto o file rotto
        return None


def _peso(sorgente) -> int:
    if isinstance(sorgente, bytes):
        return len(sorgente)
    try:
        return Path(sorgente).stat().st_size
    except OSError:
        return 0


def immagine_decorativa(nome: str, sorgente, inline: bool = False) -> bool:
    """True se l'immagine è un elemento di firma e non contenuto da indicizzare.

    ``sorgente`` sono i byte dell'immagine (posta in arrivo, ancora in memoria)
    oppure il percorso di un file già su disco (import da cartella): nel secondo
    caso il file non viene caricato, si legge solo l'intestazione.

    ``inline`` va passato quando il messaggio la dichiara incorporata nel corpo
    (Content-Id, isInline): in quel caso è decorativa per definizione."""
    if inline:
        return True

    stem = nome.rsplit(".", 1)[0]
    if _NOMI_DECORATIVI.match(stem):
        return True

    lato_minimo = settings.KB_MAIL_MIN_IMAGE_PX
    misure = _dimensioni(sorgente)
    if misure is not None:
        larghezza, altezza = misure
        # il lato lungo decide: un logo è piccolo in entrambe le direzioni,
        # una schermata stretta e alta resta contenuto legittimo
        return max(larghezza, altezza) < lato_minimo

    # dimensioni illeggibili: ci resta il peso come indizio
    return _peso(sorgente) < _PESO_TRASCURABILE
