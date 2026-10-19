"""Indice full-text (FTS5) sul testo dei chunk, per la ricerca ibrida.

La ricerca per soli vettori sbaglia sistematicamente quando la domanda contiene
un termine *letterale* — numero di fattura, sigla, codice prodotto, cognome —
perché l'embedding di un codice non assomiglia a niente. L'indice full-text di
SQLite (già disponibile, nessuna dipendenza in più) copre esattamente quel caso
e le due classifiche vengono poi fuse (vedi kb/retrieval.py).

La tabella è "external content": non duplica il testo, punta alle righe di
kb_chunk, e tre trigger la tengono allineata a ogni insert/update/delete —
compresi quelli della reindicizzazione, che cancella e riscrive i chunk.
"""
from django.db import migrations

CREA = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunk_fts USING fts5(
        text,
        content='kb_chunk',
        content_rowid='id',
        tokenize="unicode61 remove_diacritics 2"
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS kb_chunk_fts_ai AFTER INSERT ON kb_chunk BEGIN
        INSERT INTO kb_chunk_fts(rowid, text) VALUES (new.id, new.text);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS kb_chunk_fts_ad AFTER DELETE ON kb_chunk BEGIN
        INSERT INTO kb_chunk_fts(kb_chunk_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS kb_chunk_fts_au AFTER UPDATE ON kb_chunk BEGIN
        INSERT INTO kb_chunk_fts(kb_chunk_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO kb_chunk_fts(rowid, text) VALUES (new.id, new.text);
    END;
    """,
    # popolamento iniziale con i chunk già indicizzati
    "INSERT INTO kb_chunk_fts(rowid, text) SELECT id, text FROM kb_chunk;",
]

ELIMINA = [
    "DROP TRIGGER IF EXISTS kb_chunk_fts_au;",
    "DROP TRIGGER IF EXISTS kb_chunk_fts_ad;",
    "DROP TRIGGER IF EXISTS kb_chunk_fts_ai;",
    "DROP TABLE IF EXISTS kb_chunk_fts;",
]


def crea(apps, schema_editor):
    """Crea l'indice. Se la build di SQLite non ha FTS5 non è un errore
    fatale: la ricerca ibrida si spegne da sola e restano i soli vettori."""
    from django.db.utils import OperationalError

    with schema_editor.connection.cursor() as cur:
        try:
            for sql in CREA:
                cur.execute(sql)
        except OperationalError as e:            # pragma: no cover — build senza fts5
            print(f"  ! indice full-text non creato ({e}): la ricerca resterà solo vettoriale")


def elimina(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        for sql in ELIMINA:
            cur.execute(sql)


class Migration(migrations.Migration):

    dependencies = [("kb", "0001_initial")]

    operations = [migrations.RunPython(crea, elimina)]
