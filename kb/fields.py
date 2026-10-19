"""Vettori dei documenti salvati come float32 binari."""
import json

from django.db import models


class VettoreField(models.BinaryField):
    """Un vettore di float salvato come float32 binari, letto come array numpy.

    ``Document.embedding`` era un campo JSON: 1024 numeri scritti in testo
    (~20 KB a documento) da rileggere con il parser JSON e trasformare in una
    lista di oggetti float di Python ogni volta che i temi confrontano i
    documenti. In binario sono 4 KB, e la lettura è una copia di memoria.

    In scrittura accetta liste o array; in lettura restituisce un
    ``numpy.ndarray`` float32 (o None). Attenzione: ``if vettore:`` su un array
    solleva un errore, si usa ``vettore is not None``."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("editable", False)
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop("editable", None)
        return name, path, args, kwargs

    def from_db_value(self, value, expression, connection):
        if value is None:
            return None
        import numpy as np
        if isinstance(value, str):
            # colonna ancora in JSON: server riavviato senza `manage.py migrate`
            # (la migrazione 0016 converte). Si legge lo stesso invece di rompere.
            return np.asarray(json.loads(value), dtype=np.float32)
        return np.frombuffer(bytes(value), dtype=np.float32)

    def to_python(self, value):
        if value is None or isinstance(value, (bytes, bytearray, memoryview)):
            return super().to_python(value)
        import numpy as np
        return np.asarray(value, dtype=np.float32)

    def get_prep_value(self, value):
        if value is None or isinstance(value, (bytes, bytearray, memoryview)):
            return super().get_prep_value(value)
        import numpy as np
        return np.asarray(value, dtype=np.float32).tobytes()
