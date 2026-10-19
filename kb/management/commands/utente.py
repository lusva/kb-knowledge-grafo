"""Crea o aggiorna un utente dell'applicazione.

`createsuperuser` di Django chiede tutto a mano e fa solo superuser. Qui si
crea in una riga anche un utente normale (che usa la chat ma non tocca la
configurazione), e si cambia la password a chi l'ha dimenticata.

    manage.py utente mario --password segreta
    manage.py utente anna  --password segreta --staff
    manage.py utente capo  --password segreta --super
    manage.py utente mario --password nuova            # cambia la password
    manage.py utente mario --disattiva                 # revoca l'accesso
    manage.py utente mario --knowledge commesse        # apre una knowledge riservata
    manage.py utente mario --togli-knowledge commesse  # la richiude
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from kb.models import KnowledgeBase


class Command(BaseCommand):
    help = "Crea o aggiorna un utente (accesso, permessi, password)."

    def add_arguments(self, parser):
        parser.add_argument("username", help="nome utente per il login")
        parser.add_argument("--password", help="password; se manca viene chiesta")
        parser.add_argument("--email", default="", help="indirizzo email (facoltativo)")
        parser.add_argument("--staff", action="store_true",
                            help="può aprire configurazione, caselle e database")
        parser.add_argument("--super", action="store_true", dest="superuser",
                            help="amministratore completo (implica --staff)")
        parser.add_argument("--disattiva", action="store_true",
                            help="blocca l'accesso senza cancellare le conversazioni")
        parser.add_argument("--attiva", action="store_true", help="riabilita l'accesso")
        parser.add_argument("--knowledge", action="append", default=[], metavar="SLUG",
                            help="dà accesso a una knowledge riservata (ripetibile)")
        parser.add_argument("--togli-knowledge", action="append", default=[], metavar="SLUG",
                            dest="togli", help="toglie l'accesso a una knowledge riservata")

    def handle(self, *args, **opt):
        User = get_user_model()
        nome = opt["username"].strip()
        if not nome:
            raise CommandError("Il nome utente non può essere vuoto.")

        # le knowledge si controllano prima di creare l'utente: un nome
        # sbagliato non deve lasciare dietro di sé un utente a metà
        slugs = opt["knowledge"] + opt["togli"]
        trovate = {kb.slug: kb for kb in KnowledgeBase.objects.filter(slug__in=slugs)}
        mancanti = sorted(set(slugs) - set(trovate))
        if mancanti:
            raise CommandError("Knowledge inesistenti: " + ", ".join(mancanti))

        utente, nuovo = User.objects.get_or_create(username=nome)

        password = opt.get("password")
        if nuovo and not password:
            # Senza password l'utente esisterebbe ma non potrebbe entrare.
            from getpass import getpass
            password = getpass(f"Password per {nome}: ")
            if password != getpass("Conferma: "):
                utente.delete()   # appena creato: non lasciarlo senza password
                raise CommandError("Le due password non coincidono.")

        if password:
            try:
                validate_password(password, utente)
            except ValidationError as e:
                if nuovo:
                    utente.delete()
                raise CommandError("Password rifiutata: " + " ".join(e.messages)) from e
            utente.set_password(password)

        if opt["email"]:
            utente.email = opt["email"]
        # I permessi si tolgono solo dicendolo: un aggiornamento di password non
        # deve degradare di nascosto un amministratore a utente semplice.
        if opt["superuser"]:
            utente.is_superuser = True
            utente.is_staff = True
        elif opt["staff"]:
            utente.is_staff = True
        if opt["disattiva"]:
            utente.is_active = False
        if opt["attiva"]:
            utente.is_active = True
        utente.save()
        for slug in opt["knowledge"]:
            trovate[slug].utenti.add(utente)
            if not trovate[slug].riservata:
                self.stdout.write(self.style.WARNING(
                    f"«{trovate[slug].name}» non è riservata: la vedono già tutti."))
        for slug in opt["togli"]:
            trovate[slug].utenti.remove(utente)

        ruolo = ("amministratore" if utente.is_superuser
                 else "staff" if utente.is_staff else "utente")
        stato = "" if utente.is_active else " (accesso disattivato)"
        azione = "creato" if nuovo else "aggiornato"
        self.stdout.write(self.style.SUCCESS(
            f"{nome}: {azione} come {ruolo}{stato}."))
