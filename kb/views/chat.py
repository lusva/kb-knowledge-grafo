"""Chat documentale, privata per utente e vincolata alla knowledge scelta."""
import json
import logging
import threading

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .. import domande, ollama
from ..models import Conversation, KnowledgeBase, Message
from .comune import api_login_required

log = logging.getLogger("kb.chat")
_guard = threading.Lock()
_in_progress = set()
FILTER_KEYS = {"entita", "docs", "tema", "tag", "persona", "org", "tipi", "da", "a"}


def _error(detail, status=400):
    return JsonResponse({"detail": detail}, status=status)


def _conversations(user):
    return Conversation.objects.filter(
        user=user, knowledge__in=KnowledgeBase.objects.visibili(user)).select_related("knowledge")


def _conversation_data(conversation):
    return {"id": conversation.pk, "title": conversation.title,
            "kb": conversation.knowledge.slug, "filters": conversation.filters,
            "updated_at": conversation.updated_at.isoformat()}


def _message_data(message):
    return {"id": message.pk, "role": message.role, "content": message.content,
            "sources": message.sources}


def _filters(raw):
    if not isinstance(raw, dict) or raw.keys() - FILTER_KEYS:
        raise ValueError("Filtri non validi")
    result = {}
    for key, value in raw.items():
        if isinstance(value, list) and key == "docs":
            if not value or len(value) > 100 or any(
                    not isinstance(v, str) or not v.strip() or len(v) > 2048 for v in value):
                raise ValueError("Elenco documenti non valido")
            if value:
                result[key] = value
        elif isinstance(value, str) and len(value) <= 2048:
            if value.strip():
                result[key] = value.strip()
        else:
            raise ValueError("Filtri non validi")
    return result


@require_GET
@api_login_required
def chats_view(request):
    conversations = _conversations(request.user)
    slug = request.GET.get("kb")
    if slug:
        kb = KnowledgeBase.objects.visibili(request.user).filter(slug=slug).first()
        if kb is None:
            return _error("Knowledge non trovata", 404)
        conversations = conversations.filter(knowledge=kb)
    return JsonResponse({"conversations": [_conversation_data(c) for c in conversations]})


@require_http_methods(["GET", "DELETE"])
@api_login_required
def chat_detail_view(request, conversation_id):
    conversation = _conversations(request.user).filter(pk=conversation_id).first()
    if conversation is None:
        return _error("Conversazione non trovata", 404)
    if request.method == "DELETE":
        conversation.delete()
        return JsonResponse({"ok": True})
    return JsonResponse({"conversation": _conversation_data(conversation),
                         "messages": [_message_data(m) for m in conversation.messages.all()]})


@require_POST
@api_login_required
def chat_view(request):
    try:
        body = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("JSON non valido")
    if not isinstance(body, dict):
        return _error("La richiesta deve essere un oggetto JSON")
    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        return _error("Scrivi una domanda")
    question = question.strip()
    if len(question) > 4000:
        return _error("La domanda può contenere al massimo 4000 caratteri")
    slug = body.get("kb")
    if not isinstance(slug, str) or not slug:
        return _error("Seleziona una knowledge")
    kb = KnowledgeBase.objects.visibili(request.user).filter(slug=slug).first()
    if kb is None:
        return _error("Knowledge non trovata", 404)
    conversation_id = body.get("conversation_id")
    if conversation_id is not None and (type(conversation_id) is not int or conversation_id <= 0):
        return _error("Conversazione non valida")
    conversation = None
    if conversation_id is not None:
        conversation = _conversations(request.user).filter(pk=conversation_id, knowledge=kb).first()
        if conversation is None:
            return _error("Conversazione non trovata", 404)
    try:
        filters = _filters(body.get("filters", conversation.filters if conversation else {}))
    except ValueError as error:
        return _error(str(error))
    if conversation and filters != conversation.filters:
        return _error("Per cambiare i documenti della conversazione avvia una nuova chat")

    # Non tenere una transazione SQLite aperta mentre risponde il modello.
    # L'istante updated_at viene confrontato al salvataggio anche per gestire
    # due processi che provano a rispondere alla stessa conversazione.
    key = (request.user.pk, conversation_id or f"new:{kb.pk}")
    with _guard:
        if key in _in_progress:
            return _error("Una risposta è già in preparazione per questa chat", 409)
        _in_progress.add(key)
    try:
        previous_update = conversation.updated_at if conversation else None
        history = list(conversation.messages.order_by("-id").values("role", "content")[:8])[::-1] \
            if conversation else []
        content, sources = domande.rispondi(kb, question, history, filters)
        with transaction.atomic():
            # Se i permessi vengono revocati durante la risposta, non restituire
            # il testo né ricreare una knowledge/conversazione eliminata.
            if not KnowledgeBase.objects.visibili(request.user).filter(pk=kb.pk).exists():
                return _error("Knowledge non trovata", 404)
            if conversation:
                current = _conversations(request.user).select_for_update().filter(pk=conversation.pk).first()
                if current is None:
                    return _error("Conversazione non trovata", 404)
                if current.updated_at != previous_update:
                    return _error("La conversazione è cambiata. Ricaricala e riprova", 409)
                conversation = current
            else:
                conversation = Conversation.objects.create(
                    user=request.user, knowledge=kb, title=" ".join(question.split())[:120], filters=filters)
            question_message = Message.objects.create(conversation=conversation, role="user", content=question)
            answer_message = Message.objects.create(conversation=conversation, role="assistant",
                                                    content=content, sources=sources)
            conversation.save(update_fields=["updated_at"])
        return JsonResponse({"conversation": _conversation_data(conversation),
                             "messages": [_message_data(question_message), _message_data(answer_message)]})
    except ollama.OllamaError:
        log.warning("Motore non disponibile per la chat della knowledge %s", kb.slug)
        return _error("Non riesco a generare la risposta. Controlla che Ollama o il server LLM "
                      "siano attivi e che il modello chat configurato sia disponibile, poi riprova.", 503)
    except ValueError as error:
        return _error(str(error))
    except Exception:
        log.exception("Risposta non riuscita per la knowledge %s", kb.slug)
        return _error("Risposta non riuscita. Riprova fra poco; nessun messaggio è stato salvato.", 503)
    finally:
        with _guard:
            _in_progress.discard(key)
