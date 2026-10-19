"""Testo citato nelle email: le risposte precedenti riportate in coda.

Ogni risposta in un thread porta con sé, dopo una riga tipo "Il giorno … ha
scritto:", tutto il messaggio a cui risponde. Su un archivio di posta questo
vuol dire che lo stesso paragrafo compare in cinque email diverse: i passaggi
recuperati per una domanda sono spesso copie dello stesso testo, e il modello
vede meno informazione di quanta ne potrebbe ricevere. Qui si taglia via la
parte citata, tenendo solo ciò che il mittente ha scritto davvero.

Il taglio avviene all'indicizzazione, non sul file: la copia su disco resta
completa, com'è arrivata. Riconosciamo:

* la riga di apertura della citazione, in italiano, inglese, francese,
  tedesco e spagnolo ("Il giorno … ha scritto:", "On … wrote:"), anche
  quando il client l'ha spezzata su due righe;
* i separatori di Outlook ("-----Messaggio originale-----", la riga di
  underscore seguita dal blocco Da:/Inviato:);
* le righe che cominciano con ">".

Un messaggio *inoltrato* è un'altra cosa: il testo sotto il separatore è il
contenuto che si voleva far leggere, non una ripetizione. Da un separatore di
inoltro in poi non si taglia niente. E se dopo il taglio non resterebbe nulla
(una risposta vuota sopra la citazione) si tiene tutto.
"""
import re

# Riga che chiude l'apertura di una citazione ("… ha scritto:").
_CHIUSURA = re.compile(
    r"(ha\s+scritto|wrote|a\s+écrit|schrieb|escribió|escreveu)\s*:\s*$", re.I)

# Riga con cui l'apertura comincia: serve a risalire quando è spezzata su
# più righe ("Il giorno ven 3 mag 2024 alle ore 10:12 Mario Rossi <\nm@x.it> ha scritto:").
_APERTURA = re.compile(
    r"^(il\s+giorno|il\s+|in\s+data|on\s+|le\s+|am\s+|el\s+|em\s+)", re.I)

# Separatori di un blocco citato per intero.
_SEPARATORE = re.compile(
    r"^\s*(-{2,}\s*(messaggio\s+originale|original\s+message|mensaje\s+original|"
    r"message\s+d'origine|ursprüngliche\s+nachricht)\s*-{2,}|_{6,})\s*$", re.I)

# Separatori di inoltro: da qui in poi il testo è contenuto, non citazione.
_INOLTRO = re.compile(
    r"^\s*-{2,}\s*(messaggio\s+inoltrato|forwarded\s+message|mensaje\s+reenviado|"
    r"message\s+transféré|weitergeleitete\s+nachricht)\s*-{2,}\s*$", re.I)

# Blocco di intestazioni ricopiato da Outlook: "Da: …" seguito entro poche
# righe da "Inviato: …" (o "Sent:", "Date:").
_DA = re.compile(r"^\s*(da|from|von|de)\s*:\s*\S", re.I)
_INVIATO = re.compile(r"^\s*(inviato|sent|date|data|gesendet|enviado|envoyé)\s*:\s*\S", re.I)

RIGHE_APERTURA = 3       # su quante righe può essere spezzata l'apertura


def _inizio_citazione(righe: list[str]) -> int | None:
    """Indice della prima riga della parte citata, o None se non c'è."""
    for i, riga in enumerate(righe):
        r = riga.strip()
        if not r:
            continue
        if _INOLTRO.match(r):
            return None
        if _SEPARATORE.match(r):
            return i
        if _CHIUSURA.search(r):
            inizio = i
            for j in range(i - 1, max(-1, i - RIGHE_APERTURA), -1):
                precedente = righe[j].strip()
                if not precedente:
                    break
                if _APERTURA.match(precedente):
                    inizio = j
                    break
                inizio = j            # riga intermedia dell'apertura spezzata
            return inizio
        if _DA.match(r):
            seguenti = [x.strip() for x in righe[i + 1:i + 4]]
            if any(_INVIATO.match(s) for s in seguenti if s):
                return i
    return None


def togli_citazioni(testo: str) -> str:
    """Il corpo di una email senza le risposte precedenti riportate in coda."""
    if not testo:
        return testo
    righe = testo.splitlines()
    taglio = _inizio_citazione(righe)
    corpo = righe[:taglio] if taglio is not None else righe
    corpo = [r for r in corpo if not r.lstrip().startswith(">")]
    risultato = "\n".join(corpo).rstrip()
    if not risultato.strip():
        return testo             # era tutta citazione: senza, non resterebbe niente
    return risultato


# --------------------------------------------------------------------------- #
