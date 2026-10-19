"""Tentativi di accesso sbagliati: dopo troppi errori, una pausa.

Senza un limite la pagina di login accetta password all'infinito, alla
velocità della rete. Qui si contano gli errori per nome utente (5) e per
indirizzo di provenienza (30, più alto: da localhost arrivano tutti dallo
stesso indirizzo) in una finestra di 15 minuti; oltre, l'accesso è rifiutato
anche con la password giusta finché la finestra non scade. Un accesso
riuscito azzera il conto di quel nome utente.

Il conto sta nella cache di Django, che qui è in memoria: vale per il
processo del server (uno solo, con --noreload) e si azzera riavviandolo.
Vale sia per la pagina di accesso dell'app sia per /admin/login/, che
altrimenti sarebbe una porta laterale senza limite.
"""
from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache

MAX_PER_UTENTE = 5
MAX_PER_INDIRIZZO = 30
FINESTRA = 15 * 60          # secondi

BLOCCATO = ("Troppi tentativi di accesso sbagliati. Riprova fra qualche minuto "
            "(o chiedi a un amministratore di reimpostare la password).")
SBAGLIATO = "Nome utente o password non validi. Riprova."


def _chiavi(username: str, indirizzo: str) -> list[tuple[str, int]]:
    return [(f"kb:accesso:utente:{username.strip().lower()}", MAX_PER_UTENTE),
            (f"kb:accesso:ip:{indirizzo}", MAX_PER_INDIRIZZO)]


def bloccato(username: str, indirizzo: str) -> bool:
    return any((cache.get(k) or 0) >= massimo for k, massimo in _chiavi(username, indirizzo))


def registra_errore(username: str, indirizzo: str) -> None:
    for k, _ in _chiavi(username, indirizzo):
        # add non tocca un conto esistente: la finestra parte dal primo errore
        cache.add(k, 0, FINESTRA)
        try:
            cache.incr(k)
        except ValueError:          # scaduta fra add e incr
            cache.set(k, 1, FINESTRA)


def azzera(username: str) -> None:
    cache.delete(_chiavi(username, "")[0][0])


class _ConLimite:
    """Il limite, da mettere davanti a un modulo di accesso di Django."""

    def clean(self):
        username = self.data.get("username") or ""
        indirizzo = self.request.META.get("REMOTE_ADDR", "") if self.request else ""
        if bloccato(username, indirizzo):
            raise forms.ValidationError(BLOCCATO, code="bloccato")
        try:
            dati = super().clean()
        except forms.ValidationError:
            registra_errore(username, indirizzo)
            raise
        azzera(username)
        return dati


class ModuloAccesso(_ConLimite, AuthenticationForm):
    error_messages = {**AuthenticationForm.error_messages, "invalid_login": SBAGLIATO}


class ModuloAccessoAdmin(_ConLimite, AdminAuthenticationForm):
    pass
