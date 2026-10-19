"""La chat usa documenti autorizzati e conserva la cronologia del solo utente."""
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from kb.models import Conversation, Message
from kb.ollama import OllamaError

from .test_core import TemporaryArchive


class DocumentChatTests(TemporaryArchive, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.document(self.kb, "manuale.md", text="La manutenzione si esegue ogni sei mesi.")
        self.document(self.private, "riservato.md", text="CONTENUTO RISERVATO")
        self.hit = {
            "doc": "manuale.md", "titolo": "Manuale manutenzione", "chunk": 0,
            "text": "La manutenzione si esegue ogni sei mesi.",
            "score": 0.85, "origine": "entrambi", "rrf": 0.03,
        }
        search_patcher = mock.patch("kb.retrieval.search", return_value=[self.hit])
        llm_patcher = mock.patch(
            "kb.ollama.chat_once", return_value="La manutenzione va eseguita ogni sei mesi [1].",
        )
        self.search = search_patcher.start()
        self.llm = llm_patcher.start()
        self.addCleanup(search_patcher.stop)
        self.addCleanup(llm_patcher.stop)

    def ask(self, question="Quando si esegue la manutenzione?", **fields):
        return self.post_json("/api/chat", {"kb": self.kb.slug, "question": question, **fields})

    def conversation(self, *, user=None, knowledge=None, title="Manutenzione", filters=None):
        return Conversation.objects.create(
            user=user or self.user, knowledge=knowledge or self.kb,
            title=title, filters=filters or {},
        )

    def prompt(self):
        call = self.llm.call_args
        return call.args[0] if call.args else call.kwargs["messages"]

    def test_answer_persists_a_conversation_and_numbered_document_sources(self):
        response = self.ask(conversation_id=None)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        conversation = Conversation.objects.get(pk=result["conversation"]["id"])
        self.assertEqual(conversation.user, self.user)
        self.assertEqual(conversation.knowledge, self.kb)
        self.assertEqual(result["conversation"]["kb"], self.kb.slug)
        self.assertTrue(result["conversation"]["title"])
        self.assertEqual(result["conversation"]["filters"], {})
        messages = result["messages"]
        self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["content"], "Quando si esegue la manutenzione?")
        self.assertEqual(messages[1]["content"], self.llm.return_value)
        self.assertEqual(messages[1]["sources"], [{
            "number": 1, "doc": "manuale.md", "title": "Manuale manutenzione",
            "text": self.hit["text"],
        }])
        self.assertEqual(Message.objects.filter(conversation=conversation).count(), 2)
        history = self.client.get(f"/api/chats/{conversation.pk}")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["messages"], messages)
        self.search.assert_called_once()
        called_kb = self.search.call_args.args[0] if self.search.call_args.args else self.search.call_args.kwargs["kb"]
        self.assertEqual(called_kb.pk, self.kb.pk)
        self.assertIn(self.hit["text"], json.dumps(self.prompt(), ensure_ascii=False))
        self.assertNotIn("CONTENUTO RISERVATO", json.dumps(result))

    def test_followup_uses_prior_turns_and_preserves_conversation(self):
        first = self.ask().json()
        conversation_id = first["conversation"]["id"]
        self.llm.return_value = "Ogni sei mesi corrisponde a due interventi all'anno [1]."
        second = self.ask("Quante volte all'anno quindi?", conversation_id=conversation_id)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["conversation"]["id"], conversation_id)
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(Message.objects.filter(conversation_id=conversation_id).count(), 4)
        prompt = json.dumps(self.prompt(), ensure_ascii=False)
        self.assertIn(first["messages"][0]["content"], prompt)
        self.assertIn("La manutenzione va eseguita ogni sei mesi", prompt)
        self.assertIn("Quante volte all'anno quindi?", prompt)
        history = self.client.get(f"/api/chats/{conversation_id}").json()["messages"]
        self.assertEqual([message["role"] for message in history], ["user", "assistant", "user", "assistant"])

    def test_graph_document_filters_are_kept_for_followup_questions(self):
        filters = {"docs": ["manuale.md"]}
        first = self.ask(filters=filters).json()
        self.assertEqual(first["conversation"]["filters"], filters)
        self.assertEqual(self.search.call_args.kwargs.get("filtri"), filters)
        second = self.ask("Puoi riassumere?", conversation_id=first["conversation"]["id"])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["conversation"]["filters"], filters)
        self.assertEqual(self.search.call_args.kwargs.get("filtri"), filters)

    def test_followup_cannot_silently_change_the_selected_documents(self):
        conversation = self.conversation(filters={"docs": ["manuale.md"]})
        response = self.ask(conversation_id=conversation.pk, filters={})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Message.objects.exists())
        self.search.assert_not_called()

    def test_without_matching_documents_the_model_is_not_called(self):
        self.search.return_value = []
        response = self.ask()
        self.assertEqual(response.status_code, 200)
        answer = response.json()["messages"][-1]
        self.assertEqual(answer["role"], "assistant")
        self.assertTrue(answer["content"].strip())
        self.assertEqual(answer["sources"], [])
        self.llm.assert_not_called()

    def test_llm_failure_does_not_save_partial_turns(self):
        conversation = self.conversation()
        previous = Message.objects.create(
            conversation=conversation, role="user", content="Una domanda precedente", sources=[],
        )
        self.llm.side_effect = OllamaError("Modello temporaneamente non disponibile")
        for conversation_id in (None, conversation.pk):
            with self.subTest(conversation_id=conversation_id):
                response = self.ask(conversation_id=conversation_id)
                self.assertIn(response.status_code, (502, 503))
                self.assertEqual(list(Message.objects.values_list("pk", flat=True)), [previous.pk])

    def test_retrieval_failure_does_not_save_messages_or_call_the_model(self):
        self.search.side_effect = OllamaError("Embedding non disponibile")
        response = self.ask()
        self.assertIn(response.status_code, (502, 503))
        self.assertFalse(Message.objects.exists())
        self.llm.assert_not_called()

    def test_empty_or_malformed_requests_are_rejected_without_generation(self):
        bodies = [
            "{", "[]", "null", '"testo"',
            json.dumps({"kb": self.kb.slug, "question": "  "}),
            json.dumps({"kb": self.kb.slug}),
            json.dumps({"kb": self.kb.slug, "question": 42}),
            json.dumps({"kb": self.kb.slug, "question": "Domanda", "filters": []}),
            json.dumps({"kb": self.kb.slug, "question": "Domanda", "filters": {"docs": []}}),
            json.dumps({"kb": self.kb.slug, "question": "Domanda", "conversation_id": []}),
        ]
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.post("/api/chat", body, content_type="application/json")
                self.assertEqual(response.status_code, 400)
        self.search.assert_not_called()
        self.llm.assert_not_called()
        self.assertFalse(Message.objects.exists())

    def test_unknown_or_forbidden_knowledge_cannot_be_queried(self):
        for kb in ("inesistente", self.private.slug):
            with self.subTest(kb=kb):
                response = self.ask(kb=kb)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(self.client.get("/api/chats", {"kb": kb}).status_code, 404)
        self.search.assert_not_called()
        self.llm.assert_not_called()

    def test_chat_list_is_scoped_to_user_and_selected_knowledge(self):
        own = self.conversation()
        other = get_user_model().objects.create_user("altro", password="prova")
        self.conversation(user=other, title="Conversazione di un altro utente")
        self.private.utenti.add(self.user)
        private = self.conversation(knowledge=self.private, title="Conversazione riservata")
        response = self.client.get("/api/chats", {"kb": self.kb.slug})
        self.assertEqual(response.status_code, 200)
        summaries = response.json()["conversations"]
        self.assertEqual([item["id"] for item in summaries], [own.pk])
        self.assertTrue(summaries[0]["updated_at"])
        self.assertEqual(summaries[0]["kb"], self.kb.slug)
        self.assertEqual(summaries[0]["filters"], {})
        response = self.client.get("/api/chats", {"kb": self.private.slug})
        self.assertEqual([item["id"] for item in response.json()["conversations"]], [private.pk])

    def test_another_users_history_cannot_be_read_answered_or_deleted(self):
        other = get_user_model().objects.create_user("altro", password="prova")
        conversation = self.conversation(user=other)
        secret = Message.objects.create(
            conversation=conversation, role="assistant", content="CRONOLOGIA PRIVATA", sources=[],
        )
        self.assertEqual(self.client.get(f"/api/chats/{conversation.pk}").status_code, 404)
        self.assertEqual(self.ask(conversation_id=conversation.pk).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/chats/{conversation.pk}").status_code, 404)
        self.assertTrue(Message.objects.filter(pk=secret.pk).exists())
        self.search.assert_not_called()
        self.llm.assert_not_called()

    def test_conversation_cannot_be_reused_in_a_different_knowledge(self):
        self.private.utenti.add(self.user)
        conversation = self.conversation(knowledge=self.private)
        response = self.ask(conversation_id=conversation.pk)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Message.objects.exists())
        self.search.assert_not_called()
        self.llm.assert_not_called()

    def test_revoked_knowledge_access_also_revokes_saved_history(self):
        self.private.utenti.add(self.user)
        conversation = self.conversation(knowledge=self.private)
        secret = Message.objects.create(
            conversation=conversation, role="assistant", content="SEGRETO STORICO", sources=[],
        )
        self.private.utenti.remove(self.user)
        responses = [
            self.client.get(f"/api/chats/{conversation.pk}"),
            self.client.get("/api/chats", {"kb": self.private.slug}),
            self.ask(kb=self.private.slug, conversation_id=conversation.pk),
            self.client.delete(f"/api/chats/{conversation.pk}"),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("SEGRETO STORICO", response.content.decode())
        self.assertTrue(Message.objects.filter(pk=secret.pk).exists())
        self.search.assert_not_called()
        self.llm.assert_not_called()

    def test_access_revoked_during_generation_discards_the_response(self):
        self.private.utenti.add(self.user)
        conversation = self.conversation(knowledge=self.private)

        def revoke_access(*args, **kwargs):
            self.private.utenti.remove(self.user)
            return "RISPOSTA RISERVATA", []

        with mock.patch("kb.domande.rispondi", side_effect=revoke_access):
            response = self.ask(kb=self.private.slug, conversation_id=conversation.pk)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("RISPOSTA RISERVATA", response.content.decode())
        self.assertFalse(Message.objects.exists())

    def test_concurrent_update_does_not_append_an_answer_to_stale_history(self):
        conversation = self.conversation()
        previous = Message.objects.create(conversation=conversation, role="user", content="Prima domanda")

        def another_request_finishes(*args, **kwargs):
            Conversation.objects.filter(pk=conversation.pk).update(
                updated_at=timezone.now() + timedelta(seconds=1),
            )
            return "Risposta generata sullo storico precedente", []

        with mock.patch("kb.domande.rispondi", side_effect=another_request_finishes):
            response = self.ask(conversation_id=conversation.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(list(Message.objects.values_list("pk", flat=True)), [previous.pk])

    def test_deleting_a_chat_removes_its_messages_only(self):
        selected = self.conversation()
        other = self.conversation(title="Da mantenere")
        removed = Message.objects.create(conversation=selected, role="user", content="Da eliminare")
        kept = Message.objects.create(conversation=other, role="user", content="Da mantenere")
        response = self.client.delete(f"/api/chats/{selected.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertFalse(Conversation.objects.filter(pk=selected.pk).exists())
        self.assertFalse(Message.objects.filter(pk=removed.pk).exists())
        self.assertTrue(Message.objects.filter(pk=kept.pk).exists())
        self.assertTrue(self.kb.documents.exists())
