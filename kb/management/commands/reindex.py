from django.core.management.base import BaseCommand

from kb.models import Document, KnowledgeBase
from kb.reindex import reindex

VUOTO = "(documento vuoto"


class Command(BaseCommand):
    help = "Reindicizza tutte le knowledge (o una sola con --kb <slug>)."

    def add_arguments(self, parser):
        parser.add_argument("--kb", dest="slug", default=None,
                            help="Slug della knowledge da reindicizzare (default: tutte).")
        parser.add_argument("--rileggi-vuoti", action="store_true", dest="rileggi_vuoti",
                            help="Azzera l'hash dei documenti risultati vuoti, così "
                                 "vengono riletti (utile dopo aver attivato l'OCR).")

    def handle(self, *args, **options):
        qs = KnowledgeBase.objects.all()
        if options["slug"]:
            qs = qs.filter(slug=options["slug"])
        if not qs.exists():
            self.stdout.write(self.style.WARNING("Nessuna knowledge da reindicizzare."))
            return

        if options["rileggi_vuoti"]:
            # la reindicizzazione salta i file il cui hash non è cambiato:
            # azzerarlo li fa rileggere da capo, stavolta con l'OCR attivo.
            # Contano come "vuoti" i documenti senza testo, i PDF con meno testo
            # della soglia OCR (scansioni indicizzate con l'OCR spento, per
            # esempio in un primo passaggio veloce) e le immagini rimaste con il
            # solo nome del file perché il modello di visione non c'era.
            from django.db.models import Q
            from django.db.models.functions import Length
            from kb import config
            vuoti = Document.objects.filter(knowledge__in=qs).annotate(
                lunghezza=Length("preview")).filter(
                Q(chunk_count=1, chunks__text__startswith=VUOTO)
                | Q(ext=".pdf", lunghezza__lt=config.get("ocr_min_chars"))
                | Q(kind="immagine", preview__startswith="Immagine: ")).distinct()
            n = vuoti.count()
            Document.objects.filter(pk__in=list(vuoti.values_list("pk", flat=True))).update(file_hash="")
            self.stdout.write(f"Documenti vuoti o scansionati da rileggere: {n}")

        for kb in qs:
            stats = reindex(kb)
            self.stdout.write(self.style.SUCCESS(
                f"[{kb.name}] Aggiunti: {stats['aggiunti']}  Aggiornati: {stats['aggiornati']}  "
                f"Rimossi: {stats['rimossi']}  Invariati: {stats['invariati']}"
            ))
            for e in stats["errori"]:
                self.stdout.write(self.style.ERROR(f"  - {e}"))
