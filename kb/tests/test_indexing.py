"""Importazione e ricerca con SQLite/FTS e Qdrant reali in cartelle temporanee.

Si simulano soltanto il server embeddings e la generazione dei nomi dei temi.
"""
import json
from unittest import mock

from django.test import TestCase, TransactionTestCase

from kb import config, graph, lexical, reindex, temi, vectorstore
from kb.models import Chunk, Document
from kb.tests.test_core import TemporaryArchive


def embeddings(texts, **kwargs):
    """Vettori ripetibili: documenti dello stesso argomento sono vicini."""
    groups = ("motore", "fattura", "manuale", "sicurezza")
    result = []
    for text in texts:
        scores = [float(text.lower().count(word)) for word in groups]
        if not any(scores):
            scores[0] = 1.0
        result.append(scores)
    return result


class IndexingIntegrationTests(TemporaryArchive, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        vectorstore._chiudi()
        self.addCleanup(vectorstore._chiudi)
        graph._cache.clear()
        self.addCleanup(graph._cache.clear)
        self.enterContext(config.forza(
            embed_dim=4, hybrid=1, temi_auto=0, chunk_size=600, chunk_overlap=40,
        ))
        self.embed = self.enterContext(mock.patch("kb.ollama.embed_texts", side_effect=embeddings))
        self.name_model = self.enterContext(mock.patch("kb.ollama.chat_once", return_value="Argomento di prova"))

    def write(self, kb, name, text):
        destination = kb.docs_dir / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        return destination

    def index(self, kb):
        events = list(reindex.reindex_iter(kb))
        self.assertEqual(events[-1]["type"], "done")
        stats = events[-1]["stats"]
        self.assertEqual(stats["errori"], [])
        return stats

    def search(self, query, **params):
        response = self.client.get("/api/search", {"kb": self.kb.slug, "q": query, **params})
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def test_index_search_and_graph_work_without_connectors(self):
        for index, subject in enumerate(("motore", "fattura", "manuale", "sicurezza")):
            for copy in range(2):
                self.write(
                    self.kb, f"{subject}-{copy}.md",
                    f"# {subject.capitalize()}\n\n{subject} {subject} codice ART{index}{copy} locale.",
                )
        stats = self.index(self.kb)
        self.assertEqual(stats["aggiunti"], 8)
        self.assertEqual(self.kb.documents.count(), 8)
        self.assertEqual(Chunk.objects.filter(document__knowledge=self.kb).count(), 8)
        self.assertEqual(vectorstore.collection_info(self.kb)["punti"], 8)

        # La ricerca letterale prova anche che i trigger FTS della migrazione
        # sono presenti: un indice vettoriale funzionante da solo non basta.
        exact = lexical.search_ids(self.kb, "ART00", 5)
        self.assertEqual(len(exact), 1)
        self.assertEqual(Chunk.objects.get(pk=exact[0][0]).document.rel_path, "motore-0.md")
        hits = self.search("motore ART00")
        # I due documenti motore hanno embedding identici: il pareggio RRF
        # non impone un ordine fra loro, ma il risultato esatto deve esserci.
        exact_hit = next(hit for hit in hits if hit["doc"] == "motore-0.md")
        self.assertEqual(exact_hit["origine"], "entrambi")

        groups = temi.raggruppa(self.kb, gruppi=4)
        self.assertTrue(groups)
        self.assertTrue(self.name_model.called)
        response = self.client.get("/api/graph", {"kb": self.kb.slug})
        self.assertEqual(response.status_code, 200)
        topics = [node for node in response.json()["nodes"] if node["type"] == "tema"]
        self.assertTrue(topics)
        self.assertEqual(sum(node["documenti"] for node in topics), 8)
        self.assertTrue(any(entity.startswith("#tema:") for entity in self.search("motore")[0]["entita"]))

    def test_indexing_same_file_is_idempotent_and_updates_replace_chunks(self):
        path = self.write(self.kb, "manuale.md", "manuale VERSIONEUNO motore")
        self.index(self.kb)
        original = self.kb.documents.get()
        calls = self.embed.call_count
        self.assertEqual(self.index(self.kb)["invariati"], 1)
        self.assertEqual(self.embed.call_count, calls)
        self.assertEqual(vectorstore.collection_info(self.kb)["punti"], 1)

        path.write_text("manuale VERSIONEDUE sicurezza", encoding="utf-8")
        self.assertEqual(self.index(self.kb)["aggiornati"], 1)
        updated = self.kb.documents.get()
        self.assertEqual(updated.pk, original.pk)
        self.assertNotEqual(updated.file_hash, original.file_hash)
        self.assertEqual(updated.chunks.count(), 1)
        self.assertEqual(vectorstore.collection_info(self.kb)["punti"], 1)
        self.assertEqual(lexical.search_ids(self.kb, "VERSIONEUNO", 5), [])
        self.assertTrue(lexical.search_ids(self.kb, "VERSIONEDUE", 5))

        path.unlink()
        self.assertEqual(self.index(self.kb)["rimossi"], 1)
        self.assertFalse(self.kb.documents.exists())
        self.assertEqual(vectorstore.collection_info(self.kb)["punti"], 0)
        self.assertEqual(self.search("manuale"), [])

    def test_search_keeps_collections_and_full_text_isolated(self):
        self.write(self.kb, "nota.md", "motore INFORMAZIONEPUBBLICA")
        self.write(self.private, "nota.md", "motore SEGRETOAZIENDALE")
        self.index(self.kb)
        self.index(self.private)
        self.assertEqual(vectorstore.collection_info(self.kb)["punti"], 1)
        self.assertEqual(vectorstore.collection_info(self.private)["punti"], 1)
        self.assertEqual(lexical.search_ids(self.kb, "SEGRETOAZIENDALE", 5), [])
        hits = self.search("motore")
        self.assertTrue(hits)
        self.assertTrue(all("SEGRETOAZIENDALE" not in hit["text"] for hit in hits))
        self.assertIn("INFORMAZIONEPUBBLICA", hits[0]["text"])

    def test_file_and_preview_return_indexed_local_content(self):
        text = "# Manuale\n\nContenuto locale con accenti: è già pronto."
        self.write(self.kb, "manuale.md", text)
        self.index(self.kb)
        preview = self.client.get("/api/doc", {"kb": self.kb.slug, "path": "manuale.md"})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.content.decode(), text)
        original = self.client.get("/api/file", {"kb": self.kb.slug, "path": "manuale.md"})
        self.assertEqual(original.status_code, 200)
        try:
            self.assertEqual(b"".join(original.streaming_content).decode(), text)
        finally:
            original.close()


class ImportIntegrationTests(TemporaryArchive, TransactionTestCase):
    @mock.patch("kb.ollama.ollama_available", return_value={"ok": False, "error": "spento", "models": []})
    def test_local_import_copies_files_even_when_model_is_offline(self, availability):
        self.client.force_login(self.staff)
        source = self.root / "sorgente.md"
        source.write_text("# Documento importato\n\nContenuto originale.", encoding="utf-8")
        response = self.post_json("/api/kbs/pubblica/import", {"paths": [str(source)]})
        self.assertEqual(response.status_code, 200)
        try:
            body = b"".join(response.streaming_content).decode()
        finally:
            response.close()
        events = [json.loads(line) for line in body.splitlines() if line.strip()]
        self.assertFalse([event for event in events if event.get("error")], events)
        imports = [event for event in events if event.get("type") == "import"]
        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0]["import"]["copiati"], 1)
        self.assertEqual((self.kb.docs_dir / source.name).read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
        self.assertTrue(source.exists())
        self.assertFalse(Document.objects.filter(knowledge=self.kb).exists())

