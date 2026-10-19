"""Selettore grafico di file e cartelle (Finder).

L'app gira sul Mac dell'utente (avvia.command apre localhost), quindi il
dialogo aperto dal server via AppleScript compare sullo schermo di chi sta
usando il browser. Se il server non è su macOS, o la richiesta arriva da
un'altra macchina, il selettore non è disponibile e l'interfaccia torna a
chiedere il percorso a mano.
"""
import subprocess
import sys

# Oltre questo tempo il dialogo viene chiuso: l'utente si è distratto o
# il dialogo è rimasto invisibile dietro a un'altra finestra.
TIMEOUT_S = 300

_COMMON = """on run argv
	activate
	{choose}
	set AppleScript's text item delimiters to linefeed
	return out as text
end run"""

_SCRIPTS = {
    "file": _COMMON.format(choose="""set sel to choose file with prompt (item 1 of argv) with multiple selections allowed
	set out to {}
	repeat with f in sel
		set end of out to POSIX path of f
	end repeat"""),
    # anche le cartelle a più d'una: ⌘-clic nel Finder, come per i file
    "folder": _COMMON.format(choose="""set sel to choose folder with prompt (item 1 of argv) with multiple selections allowed
	set out to {}
	repeat with f in sel
		set end of out to POSIX path of f
	end repeat"""),
}


class PickerError(Exception):
    """Errore del selettore mostrabile all'utente."""


def available() -> bool:
    """True se il selettore nativo si può usare su questa macchina."""
    return sys.platform == "darwin"


def pick(kind: str, prompt: str) -> list[str] | None:
    """Apre il Finder e ritorna i percorsi scelti.

    ``kind`` è "file" o "folder", entrambi a selezione multipla. Ritorna None se
    l'utente annulla. Solleva PickerError negli altri casi di errore.
    """
    script = _SCRIPTS.get(kind)
    if script is None:
        raise PickerError(f"Tipo di selezione non valido: {kind}")
    if not available():
        raise PickerError("Il selettore grafico è disponibile solo su macOS.")

    try:
        proc = subprocess.run(
            ["osascript", "-", prompt],
            input=script, capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise PickerError("Selezione annullata: il Finder è rimasto aperto troppo a lungo.") from None
    except OSError as e:
        raise PickerError(f"Impossibile aprire il Finder: {e}") from e

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "-128" in err:  # "User canceled"
            return None
        raise PickerError(err or "Il Finder non è riuscito ad aprirsi.")

    paths = [line for line in proc.stdout.splitlines() if line.strip()]
    return paths or None
