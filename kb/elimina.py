"""Eliminazione di file e cartelle da una knowledge.

Tre cose devono sparire insieme, altrimenti restano documenti fantasma che la
ricerca continua a trovare: le righe Document (con i Chunk e l'indice
full-text, in cascata), i vettori in Qdrant e il file su disco.

L'indice si pulisce per primo: se poi lo spostamento di un file fallisce, il
file resta dov'era e la prossima indicizzazione lo rimette nella knowledge —
nessun documento perso, nessun fantasma. I file non vengono cancellati:
finiscono nel Cestino del Mac (``KB_TRASH_DIR``), da cui si recuperano.

Gli allegati estratti da una .eml vengono rimossi insieme al messaggio.
"""
import glob
import shutil
from datetime import datetime
from pathlib import Path

from django.db import transaction

from django.conf import settings

from . import eml, vectorstore
from .models import Chunk, Document

LOTTO = 500   # id per query/cancellazione: resta sotto il limite di variabili di SQLite

class EliminaError(Exception):
    """Richiesta non valida, con un messaggio da mostrare all'utente."""


def relativo(kb, rel: str) -> str:
    """Percorso normalizzato dentro la knowledge; EliminaError se ne esce.

    Il percorso può anche non esistere più su disco (elenco vecchio, file già
    tolto dal Finder): in quel caso si ripulisce solo l'indice."""
    root = kb.docs_dir.resolve()
    full = (root / str(rel).strip().strip("/")).resolve()
    if full == root or not full.is_relative_to(root):
        raise EliminaError(f"Percorso non valido: {rel}")
    return full.relative_to(root).as_posix()


def compagni(path: Path) -> list[Path]:
    """File nati da ``path`` che, tolto lui, resterebbero orfani nell'indice."""
    ext = path.suffix.lower()
    trovati = []
    if ext in settings.KB_EML_EXT:
        cartella = path.parent / "allegati"
        # stesso nome che dà eml.explode_attachments: "<stem[:60]>__<n>_<nome>"
        trovati += cartella.glob(glob.escape(path.stem[:60]) + "__[0-9]*")
        trovati.append(eml._marker(cartella, path))
    return [p for p in trovati if p.exists()]


def _dentro(rel: str, radici: set[str]) -> bool:
    return any(rel == r or rel.startswith(r + "/") for r in radici)


def _conta_file(path: Path) -> int:
    if path.is_dir():
        return sum(1 for f in path.rglob("*") if f.is_file())
    return 1


def al_cestino(path: Path) -> Path:
    """Sposta ``path`` nel Cestino senza sovrascrivere niente; ritorna dove è finito."""
    cestino = settings.KB_TRASH_DIR
    cestino.mkdir(parents=True, exist_ok=True)
    dest = cestino / path.name
    if dest.exists() or dest.is_symlink():
        # come fa il Finder con i doppioni: il nome con l'ora
        ora = datetime.now().strftime("%H.%M.%S")
        dest = cestino / f"{path.stem} {ora}{path.suffix}"
        n = 2
        while dest.exists() or dest.is_symlink():
            dest = cestino / f"{path.stem} {ora} {n}{path.suffix}"
            n += 1
    shutil.move(str(path), str(dest))
    return dest


def elimina(kb, rel_paths) -> dict:
    """Toglie file e cartelle da indice e disco, spostandoli nel Cestino."""
    root = kb.docs_dir.resolve()
    scelti = {relativo(kb, r) for r in rel_paths if str(r).strip()}
    if not scelti:
        raise EliminaError("Niente da eliminare")
    # allegati estratti dai file scelti: vanno via con loro
    radici = set(scelti)
    for rel in scelti:
        path = root / rel
        if path.is_file():
            radici |= {p.relative_to(root).as_posix() for p in compagni(path)}
    # una cartella scelta insieme a qualcosa che contiene: basta la cartella
    radici = {r for r in radici if not _dentro(r, radici - {r})}

    # 1. indice: vettori per id dei chunk (cancellazione diretta), poi le righe
    docs = [pk for pk, rel in Document.objects.filter(knowledge=kb)
            .values_list("pk", "rel_path") if _dentro(rel, radici)]
    for i in range(0, len(docs), LOTTO):
        lotto = docs[i:i + LOTTO]
        vectorstore.delete_points(
            kb, list(Chunk.objects.filter(document_id__in=lotto).values_list("pk", flat=True)))
        # Il lotto sparisce dal database tutto insieme. Il lock di Qdrant è
        # già stato rilasciato: qui si tiene un lock solo, e l'indicizzazione
        # fa lo stesso nell'ordine opposto senza che i due si incrocino.
        with transaction.atomic():
            Document.objects.filter(pk__in=lotto).delete()

    # 2. disco: nel Cestino
    esito = {"elementi": 0, "file": 0, "documenti": len(docs), "errori": []}
    for rel in sorted(radici):
        path = root / rel
        if not path.exists() and not path.is_symlink():
            continue
        n = _conta_file(path)
        try:
            al_cestino(path)
        except OSError as e:
            esito["errori"].append(f"{rel}: {e}")
            continue
        esito["elementi"] += 1
        esito["file"] += n
    return esito
