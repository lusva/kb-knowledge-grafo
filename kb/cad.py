"""Disegni CAD in formato DXF: il testo del disegno letto dal file.

Da un DXF non serve un modello di visione: testi, quote e attributi del
cartiglio sono scritti nel file con il loro valore esatto, e ``ezdxf`` li
legge senza sbagliare una cifra. Il risultato è un testo ordinato come si
legge la tavola (dall'alto in basso, da sinistra a destra) che segue la
stessa strada di ogni altro documento: indice, chat, schede.

Cosa si tiene:
- gli attributi dei blocchi (``ATTRIB``), dove i cartigli fatti bene tengono
  codice, denominazione, materiale, revisione: «TAG: valore»;
- i testi (``TEXT``, ``MTEXT``) del modello e delle tavole di stampa;
- le quote (``DIMENSION``): il testo scritto, o la misura se il testo è quello
  automatico, con le tolleranze se ci sono;
- l'elenco dei layer, che spesso dice di che disegno si tratta.

Il DWG, formato chiuso, non si legge: va salvato in DXF dal CAD o convertito
con ODA File Converter.
"""
import logging
import re
from pathlib import Path

log = logging.getLogger("kb.cad")

MAX_RIGHE = 5000          # oltre, il resto del disegno si lascia
FASCIA = 2.0              # testi con la y entro questa distanza stanno sulla stessa riga

UNITA = {0: "", 1: "pollici", 2: "piedi", 4: "mm", 5: "cm", 6: "m"}


class CadErrore(ValueError):
    pass


def _pulisci(testo: str) -> str:
    return re.sub(r"\s+", " ", testo or "").strip()


def _testo_quota(dim) -> str:
    """Il testo che la quota mostra: quello scritto a mano, dove ``<>`` sta
    per la misura, o la misura stessa."""
    from ezdxf.tools.text import plain_mtext

    try:
        misura = dim.get_measurement()
    except Exception:                                     # noqa: BLE001
        misura = None
    if hasattr(misura, "magnitude"):      # quote angolari a tre punti: un vettore
        misura = None
    valore = ""
    if isinstance(misura, (int, float)):
        valore = f"{misura:.3f}".rstrip("0").rstrip(".")
        if dim.dimtype in (2, 5):          # angolari
            valore += "°"
        elif dim.dimtype in (3,):          # diametro
            valore = "Ø" + valore
        elif dim.dimtype in (4,):          # raggio
            valore = "R" + valore
    scritto = dim.dxf.get("text", "")
    if scritto and scritto != "<>":
        if scritto.strip() == ".":         # "." = testo della quota nascosto
            return ""
        return _pulisci(plain_mtext(scritto.replace("<>", valore)))
    return valore


def _voci(entita, righe: list):
    """Aggiunge a ``righe`` (x, y, testo) le voci leggibili delle entità."""
    for e in entita:
        tipo = e.dxftype()
        try:
            if tipo == "TEXT":
                testo, punto = e.plain_text(), e.dxf.insert
            elif tipo == "MTEXT":
                testo, punto = e.plain_text(), e.dxf.insert
            elif tipo == "DIMENSION":
                testo = _testo_quota(e)
                testo = f"quota {testo}" if testo else ""
                punto = e.dxf.get("text_midpoint") or e.dxf.defpoint
            elif tipo == "INSERT":
                for a in e.attribs:
                    valore = _pulisci(a.plain_text())
                    if valore:
                        righe.append((a.dxf.insert.x, a.dxf.insert.y,
                                      f"{a.dxf.tag}: {valore}"))
                continue
            else:
                continue
        except Exception as ex:                           # noqa: BLE001
            log.debug("entità %s non letta: %s", tipo, ex)
            continue
        testo = _pulisci(testo)
        if testo:
            righe.append((punto[0], punto[1], testo))


def _in_ordine(righe: list) -> list[str]:
    """Le voci nell'ordine di lettura: a fasce dall'alto, poi da sinistra.
    Le voci della stessa fascia finiscono sulla stessa riga, separate da
    « | », come le celle di un cartiglio."""
    ordinate = sorted(righe, key=lambda r: (-r[1], r[0]))
    uscita, fascia, y_fascia = [], [], None
    for x, y, testo in ordinate:
        if y_fascia is not None and y_fascia - y > FASCIA:
            uscita.append(" | ".join(t for _, t in sorted(fascia)))
            fascia = []
        if not fascia:
            y_fascia = y
        fascia.append((x, testo))
    if fascia:
        uscita.append(" | ".join(t for _, t in sorted(fascia)))
    return uscita


def dxf_text(path: Path) -> str:
    """Il testo del disegno: intestazione, poi una sezione per il modello e
    per ogni tavola di stampa che contiene qualcosa."""
    import ezdxf
    from ezdxf import recover

    try:
        doc = ezdxf.readfile(str(path))
    except ezdxf.DXFStructureError:
        # un DXF scritto male da qualche esportatore: si tenta il recupero
        try:
            doc, _ = recover.readfile(str(path))
        except (OSError, ezdxf.DXFStructureError) as e:
            raise CadErrore(f"DXF illeggibile: {e}") from e
    except OSError as e:
        raise CadErrore(f"DXF illeggibile: {e}") from e

    parti = [f"[Disegno CAD: {path.stem}]"]
    unita = UNITA.get(doc.header.get("$INSUNITS", 0), "")
    if unita:
        parti.append(f"Unità: {unita}")
    layer = sorted(l.dxf.name for l in doc.layers if l.dxf.name not in ("0", "Defpoints"))
    if layer:
        parti.append("Layer: " + ", ".join(layer[:200]))

    totale = 0
    for layout in doc.layouts:
        righe = []
        _voci(layout, righe)
        if not righe:
            continue
        nome = "Modello" if layout.is_modelspace else f"Tavola «{layout.name}»"
        testi = _in_ordine(righe)
        if totale + len(testi) > MAX_RIGHE:
            testi = testi[:max(0, MAX_RIGHE - totale)] + ["[… disegno troncato]"]
        parti.append(f"\n[{nome}]")
        parti.extend(testi)
        totale += len(testi)
        if totale >= MAX_RIGHE:
            break
    return "\n".join(parti)
