"""Modelli della knowledge base documentale autonoma."""
from django.conf import settings
from django.db import models
from django.db.models import Q

from .fields import VettoreField


class KnowledgeQuerySet(models.QuerySet):
    def visibili(self, user):
        """Le knowledge che ``user`` può aprire: tutte per lo staff, altrimenti
        quelle aperte più le riservate in cui è stato aggiunto.

        Il filtro sugli utenti passa da una sottoquery e non da un join: con il
        join una knowledge aperta comparirebbe una volta per ogni utente
        aggiunto, e il conteggio dei documenti del selettore si moltiplicherebbe."""
        if not getattr(user, "is_authenticated", False) or not user.is_active:
            return self.none()
        if user.is_staff:
            return self
        scelte = KnowledgeBase.utenti.through.objects.filter(user=user) \
                                                     .values("knowledgebase_id")
        return self.filter(Q(riservata=False) | Q(pk__in=scelte))


class KnowledgeBase(models.Model):
    """Una knowledge: cartella di documenti + collection vettoriale dedicate.

    Ogni knowledge vive in ``documents/<slug>/`` con una collection Qdrant
    ``kb_<slug>``, così documenti, grafo e ricerca restano isolati."""

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True)
    is_default = models.BooleanField(default=False)
    # Una knowledge aperta la vede chiunque abbia un accesso; una riservata solo
    # lo staff e gli utenti elencati (vedi KnowledgeQuerySet.visibili).
    riservata = models.BooleanField(
        default=False,
        help_text="Se attiva, la vedono solo lo staff e gli utenti scelti qui sotto.")
    utenti = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name="knowledge_riservate", blank=True,
        help_text="Utenti non staff che possono aprire la knowledge riservata.")
    # Filtro dei file, in aggiunta a «Estensioni escluse» di /config: tipi
    # (chiavi di config.TIPI_FILE) ed estensioni che questa knowledge non
    # importa e non indicizza. I file già sul disco restano dove sono.
    escludi_tipi = models.JSONField(
        default=list, blank=True,
        help_text="Tipi di file esclusi (testo, pdf, word, powerpoint, excel, html, "
                  "immagini, email, cad).")
    escludi_ext = models.CharField(
        max_length=500, blank=True, default="",
        help_text="Altre estensioni escluse, separate da virgola o spazio (es. \".csv, .gif\").")
    # cartelle (per nome, a ogni profondità) in cui import e indicizzazione
    # di questa knowledge non entrano, in aggiunta a «Cartelle escluse»
    escludi_cartelle = models.CharField(
        max_length=500, blank=True, default="",
        help_text="Altre cartelle escluse per nome, separate da virgola (es. \"dist, build\").")
    created_at = models.DateTimeField(auto_now_add=True)
    # ultimo raggruppamento dei temi: i documenti indicizzati dopo questo
    # istante sono la misura di quanto l'archivio è cambiato da allora
    topics_at = models.DateTimeField(null=True, blank=True)

    objects = KnowledgeQuerySet.as_manager()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def docs_dir(self):
        """Cartella su disco dei documenti di questa knowledge."""
        return settings.KB_DOCS_DIR / self.slug

    @property
    def collection(self) -> str:
        """Nome della collection Qdrant di questa knowledge."""
        return f"kb_{self.slug}"


class Document(models.Model):
    """Un file indicizzato dalla cartella di una knowledge."""

    knowledge = models.ForeignKey(KnowledgeBase, related_name="documents",
                                  on_delete=models.CASCADE, null=True)
    # percorso relativo alla cartella della knowledge (documents/<slug>/…)
    rel_path = models.CharField(max_length=1024)
    title = models.CharField(max_length=255)
    ext = models.CharField(max_length=16)
    file_hash = models.CharField(max_length=64, db_index=True)
    chunk_count = models.PositiveIntegerField(default=0)
    tags = models.JSONField(default=list, blank=True)
    links = models.JSONField(default=list, blank=True)
    # vettore medio dei chunk: lo usano i temi (raggruppamento e documenti nuovi).
    # float32 binari, letto come array numpy (vedi fields.VettoreField)
    embedding = VettoreField(null=True, blank=True)
    # metadati per i filtri di ricerca (ricavati dal file, vedi metadata.py):
    # data del contenuto (per le email è quella del messaggio, non del file),
    # mittente/autore quando è noto, e famiglia di documento
    doc_date = models.DateTimeField(null=True, blank=True, db_index=True)
    author = models.CharField(max_length=200, blank=True, default="")
    kind = models.CharField(max_length=16, default="documento", db_index=True)
    # indirizzi coinvolti nel documento (mittente e destinatari, per la posta):
    # diventano i nodi persona/organizzazione del grafo
    people = models.JSONField(default=list, blank=True)
    # tema assegnato dal raggruppamento dei vettori (manage.py temi)
    topic = models.CharField(max_length=80, blank=True, default="", db_index=True)
    # quando il tema è stato valutato l'ultima volta. Distingue un documento
    # *nuovo* (mai valutato: eredita il tema dal più simile) da uno che il
    # raggruppamento ha guardato e lasciato senza di proposito, perché troppo
    # lontano dal suo gruppo: quello deve restare senza.
    topic_at = models.DateTimeField(null=True, blank=True)
    preview = models.TextField(blank=True, default="")
    indexed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rel_path"]
        unique_together = [("knowledge", "rel_path")]

    def __str__(self):
        return f"{self.knowledge_id}:{self.rel_path}"


class Setting(models.Model):
    """Impostazione modificata dalla sezione amministrazione (chiave/valore).

    Se una chiave non è presente vale il default di settings.py
    (a sua volta sovrascrivibile con variabili d'ambiente)."""

    key = models.CharField(max_length=64, unique=True)
    value = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.key}={self.value}"


class Chunk(models.Model):
    """Un frammento di testo di un Document. Il testo vive qui (non in Qdrant)."""

    document = models.ForeignKey(Document, related_name="chunks", on_delete=models.CASCADE)
    index = models.PositiveIntegerField()
    text = models.TextField()

    class Meta:
        unique_together = [("document", "index")]
        ordering = ["document_id", "index"]

    def __str__(self):
        return f"{self.document.rel_path}#{self.index}"


class Conversation(models.Model):
    """Domande di un utente su una sola knowledge e un ambito stabile."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="knowledge_conversations")
    knowledge = models.ForeignKey(KnowledgeBase, on_delete=models.CASCADE,
                                  related_name="conversations")
    title = models.CharField(max_length=120)
    filters = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE,
                                     related_name="messages")
    role = models.CharField(max_length=10, choices=[("user", "Domanda"), ("assistant", "Risposta")])
    content = models.TextField()
    sources = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
