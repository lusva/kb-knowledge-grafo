"""Contratti della versione autonoma: documenti, chat locale e grafo."""
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import Resolver404, resolve

from kb import config
from kb.models import Document, KnowledgeBase


class TemporaryArchive:
    """Ogni prova usa documenti e dati temporanei, mai quelli dell'installazione."""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="kb-grafo-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        settings = override_settings(
            KB_DOCS_DIR=self.root / "documents",
            KB_DATA_DIR=self.root / "data",
            KB_QDRANT_URL="",
            PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
        )
        settings.enable()
        self.addCleanup(settings.disable)
        config.invalida_cache()
        self.addCleanup(config.invalida_cache)
        # Le fixture sostituiscono la knowledge vuota creata dalla migrazione.
        KnowledgeBase.objects.all().delete()
        self.user = get_user_model().objects.create_user("lettore", password="prova")
        self.staff = get_user_model().objects.create_user(
            "amministratore", password="prova", is_staff=True,
        )
        self.kb = KnowledgeBase.objects.create(
            name="Pubblica", slug="pubblica", is_default=True,
        )
        self.private = KnowledgeBase.objects.create(
            name="Riservata", slug="riservata", riservata=True,
        )
        self.kb.docs_dir.mkdir(parents=True)
        self.private.docs_dir.mkdir(parents=True)

    def document(self, kb, path="nota.md", text="Documento pubblico", **fields):
        destination = kb.docs_dir / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        return Document.objects.create(
            knowledge=kb, rel_path=path, title=destination.stem,
            ext=destination.suffix, file_hash="a" * 64, preview=text, **fields,
        )

    def post_json(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")


class ApplicationScopeTests(TemporaryArchive, TestCase):
    def test_only_knowledge_models_are_installed(self):
        names = {model.__name__ for model in apps.get_app_config("kb").get_models()}
        self.assertEqual(names, {
            "KnowledgeBase", "Document", "Chunk", "Setting", "Conversation", "Message",
        })

    def test_connectors_web_and_workflows_have_no_routes(self):
        removed = (
            "/api/conversations", "/api/web", "/api/web/search",
            "/posta", "/api/mail_accounts", "/api/sync_mail", "/api/calendar_accounts",
            "/api/databases", "/api/telegram", "/workflow", "/api/workflows",
        )
        for path in removed:
            with self.subTest(path=path), self.assertRaises(Resolver404):
                resolve(path)

    def test_document_chat_has_routes(self):
        for path in ("/chat", "/api/chat", "/api/chats", "/api/chats/1"):
            with self.subTest(path=path):
                self.assertIsNotNone(resolve(path))

    def test_pages_require_login(self):
        for path in ("/", "/rag", "/chat", "/config"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("login", response["Location"])

    def test_read_apis_require_login(self):
        for path in (
            "/api/kbs", "/api/kbs/pubblica/files", "/api/status", "/api/config",
            "/api/graph", "/api/search", "/api/doc", "/api/file", "/api/documents",
            "/api/chats", "/api/chats/1",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_chat_questions_require_login(self):
        response = self.post_json("/api/chat", {"kb": self.kb.slug, "question": "Cosa contiene?"})
        self.assertEqual(response.status_code, 401)

    def test_config_requires_staff(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/config").status_code, 403)
        self.assertEqual(self.post_json("/api/config", {}).status_code, 403)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/api/config").status_code, 200)

    def test_create_knowledge_validates_names(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.post_json("/api/kbs", {"name": " "}).status_code, 400)
        created = self.post_json("/api/kbs", {"name": "Manuali tecnici"})
        self.assertIn(created.status_code, (200, 201))
        kb = KnowledgeBase.objects.get(slug=created.json()["slug"])
        self.assertEqual(kb.name, "Manuali tecnici")
        self.assertTrue(kb.docs_dir.is_dir())
        self.assertEqual(self.post_json("/api/kbs", {"name": "Manuali tecnici"}).status_code, 409)

    def test_invalid_json_does_not_create_knowledge(self):
        self.client.force_login(self.staff)
        response = self.client.post("/api/kbs", "{", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(KnowledgeBase.objects.count(), 2)


class ArchiveAuthorizationTests(TemporaryArchive, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.document(self.kb, text="Informazioni condivise", topic="Manuali")
        self.document(self.private, text="SEGRETO RISERVATO", topic="Confidenziale")

    def test_visible_knowledge_list_respects_membership(self):
        def slugs():
            response = self.client.get("/api/kbs")
            self.assertEqual(response.status_code, 200)
            return {item["slug"] for item in response.json()["knowledges"]}

        self.assertEqual(slugs(), {self.kb.slug})
        self.private.utenti.add(self.user)
        self.assertEqual(slugs(), {self.kb.slug, self.private.slug})

    def test_explicit_unknown_or_forbidden_knowledge_does_not_fall_back(self):
        from kb.views.comune import resolve_kb

        self.assertIsNone(resolve_kb("inesistente", self.user))
        self.assertIsNone(resolve_kb(self.private.slug, self.user))
        self.assertEqual(resolve_kb(None, self.user), self.kb)

    def test_private_document_and_files_cannot_be_read_or_deleted(self):
        for endpoint in ("/api/doc", "/api/file"):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint, {"kb": self.private.slug, "path": "nota.md"})
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get("/api/kbs/riservata/files").status_code, 404)
        self.assertEqual(self.client.delete("/api/kbs/riservata").status_code, 404)
        self.assertEqual(self.post_json("/api/kbs/riservata/import", {"paths": []}).status_code, 404)
        self.assertTrue(self.private.docs_dir.exists())

    def test_public_graph_and_documents_do_not_include_private_content(self):
        for endpoint in ("/api/graph", "/api/documents"):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint, {"kb": self.kb.slug})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Confidenziale", response.content.decode())
                self.assertNotIn("SEGRETO", response.content.decode())

    def test_granted_user_reads_the_selected_knowledge(self):
        self.private.utenti.add(self.user)
        response = self.client.get("/api/doc", {"kb": self.private.slug, "path": "nota.md"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "SEGRETO RISERVATO")

    def test_entity_filter_returns_only_its_documents(self):
        self.document(self.kb, "altro.md", text="Altro argomento", topic="Procedure")
        response = self.client.get("/api/documents", {"kb": self.kb.slug, "entita": "#tema:Manuali"})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["total"], 1)
        self.assertEqual([item["rel_path"] for item in result["documents"]], ["nota.md"])
        self.assertEqual(result["documents"][0]["topic"], "Manuali")

    def test_traversal_paths_are_rejected_even_if_indexed(self):
        outside = self.root / "segreto.txt"
        outside.write_text("CONTENUTO ESTERNO", encoding="utf-8")
        for path in ("../../segreto.txt", str(outside)):
            Document.objects.create(
                knowledge=self.kb, rel_path=path, title="Illegittimo", ext=".txt", file_hash="x",
            )
            for endpoint in ("/api/doc", "/api/file"):
                with self.subTest(path=path, endpoint=endpoint):
                    response = self.client.get(endpoint, {"kb": self.kb.slug, "path": path})
                    self.assertEqual(response.status_code, 400)
                    self.assertNotIn("CONTENUTO ESTERNO", response.content.decode())

    def test_symlinks_cannot_escape_the_knowledge_directory(self):
        (self.kb.docs_dir / "collegamento.md").symlink_to(self.private.docs_dir / "nota.md")
        Document.objects.create(
            knowledge=self.kb, rel_path="collegamento.md", title="Collegamento", ext=".md", file_hash="x",
        )
        for endpoint in ("/api/doc", "/api/file"):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint, {"kb": self.kb.slug, "path": "collegamento.md"})
                self.assertEqual(response.status_code, 400)

    @mock.patch("kb.vectorstore.delete_collection")
    def test_delete_cascades_only_the_selected_knowledge(self, delete_collection):
        from kb.models import Chunk

        document = self.kb.documents.get()
        chunk = Chunk.objects.create(document=document, index=0, text="Passaggio")
        response = self.client.delete("/api/kbs/pubblica")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Document.objects.filter(pk=document.pk).exists())
        self.assertFalse(Chunk.objects.filter(pk=chunk.pk).exists())
        self.assertFalse(self.kb.docs_dir.exists())
        self.assertTrue(self.private.docs_dir.exists())
        self.assertTrue(self.private.documents.exists())
        delete_collection.assert_called_once()

