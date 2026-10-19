# NIKI · Knowledge base e grafo

Progetto autonomo estratto da `kb-app`. Comprende gestione delle knowledge,
importazione di file e cartelle locali, indicizzazione, ricerca interna, chat e grafo
interattivo. Il grafo mostra documenti, tag, collegamenti Markdown, temi, persone
e organizzazioni ricavate dai documenti. Un clic apre il documento o filtra
l'elenco dei documenti collegati.

Non comprende connettori di posta, calendari, database esterni, Telegram,
gestionale, workflow o ricerca su internet. I file `.eml` esportati possono
essere importati dal disco come documenti; non viene collegata alcuna casella.

## Avvio su macOS

1. Installa Python 3.10 o successivo (consigliato 3.13).
2. Avvia `avvia.command` con un doppio clic oppure `bash avvia.command`.
3. Al primo avvio vengono installate le dipendenze e richiesti nome e password
   del tuo amministratore.
4. Apri **http://127.0.0.1:8010**.

Lo script usa un ambiente `.venv` proprio. L'installazione delle dipendenze e il
download iniziale dei modelli richiedono accesso alla rete. Il funzionamento con
Ollama locale e modelli già installati non richiede ricerche o servizi internet.

Per indicizzazione e ricerca semantica serve Ollama attivo, con il modello
`nomic-embed-text`. Il modello `qwen3.5:9b` risponde nella chat e assegna i nomi ai temi; `llava` e
`glm-ocr:q8_0` sono opzionali per immagini e PDF scansionati. È possibile usare
modelli diversi modificando `.env` o la pagina Impostazioni. La dimensione degli
embeddings deve corrispondere al modello scelto.

```sh
ollama pull nomic-embed-text
ollama pull qwen3.5:9b
```

La navigazione, la lettura dei documenti già indicizzati e il grafo funzionano
anche quando il motore AI è spento. L'import copia comunque i file e segnala
quando occorre reindicizzare dopo l'avvio del motore.

## Dati e configurazione separati

- `data/db.sqlite3`: knowledge, metadati, utenti, conversazioni e impostazioni di questa copia.
- `data/qdrant/`: indice vettoriale locale.
- `data/cestino/`: file rimossi dalla knowledge, recuperabili manualmente.
- `documents/<slug>/`: documenti importati nella knowledge.
- `.env`: configurazione facoltativa; partire da `.env.example`.

Il progetto nasce con la knowledge vuota **Principale**. Non sono stati copiati
database, documenti, account o segreti dal progetto originale. Non impostare
`KB_DATA_DIR` o `KB_DOCS_DIR` sulle sue cartelle. La porta predefinita è `8010` e
il cookie di sessione è distinto, per usare le due applicazioni in parallelo.

Le uniche connessioni applicative previste sono ai motori di embeddings/LLM
configurati; font e libreria del grafo sono inclusi nel progetto. Qdrant funziona
di default in modalità incorporata, senza un server da installare.

## Uso

Dal menu account aggiungi file o cartelle, scegli i filtri di importazione e
avvia l'indicizzazione. Dal selettore in alto puoi creare knowledge separate.
Usa la ricerca per trovare passaggi nei documenti oppure apri l'elenco completo.
Il grafo documentale si popola dopo l'indicizzazione; **Rifai i temi** aggiunge i
raggruppamenti semantici quando ci sono abbastanza documenti.

Apri **Chat** per fare domande sulla knowledge selezionata. Le risposte si basano
sui passaggi recuperati dai documenti e mostrano le fonti numerate, apribili in
anteprima o come file originali. Se la ricerca non trova passaggi, la chat lo
segnala senza chiedere una risposta generale al modello. Per ottenere risposte
servono documenti indicizzati e il modello chat configurato disponibile.

Ogni conversazione resta associata alla sua knowledge e agli eventuali filtri.
Usa **Chiedi su questo** nell'anteprima per limitare le domande a un documento,
oppure seleziona un nodo del grafo e apri la chat sui documenti collegati.
Per cambiare ambito avvia una nuova conversazione. La cronologia è salvata nel
database locale ed è privata per utente; perdere l'accesso a una knowledge
impedisce anche di riaprire le relative conversazioni. **Nuova chat** conserva
le precedenti nella cronologia, da cui possono essere eliminate.

Le risposte dei modelli possono contenere errori: le fonti permettono di
controllare il contenuto. Il motore riceve domanda, breve storico e passaggi
documentali; con un server LLM diverso da Ollama locale questi dati vengono
inviati al server configurato. Nessuna ricerca internet viene eseguita.

L'aggiornamento automatico controlla soltanto le cartelle locali della knowledge
ogni cinque minuti; impostare `KB_DOC_AUTO_MINUTES=0` per usare solo il comando
manuale. L'importazione di percorsi del disco è riservata agli accessi da
localhost. Le knowledge riservate si gestiscono in `/admin/`.

## Comandi manuali

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver 127.0.0.1:8010 --noreload
```

```sh
.venv/bin/python manage.py check
.venv/bin/python manage.py test kb.tests
.venv/bin/python manage.py reindex --help
.venv/bin/python manage.py temi --help
.venv/bin/python manage.py utente --help
```

Con Qdrant incorporato, fermare il server prima dei comandi di indicizzazione
da terminale: l'indice può essere aperto da un solo processo. L'interfaccia usa
la coda di lavori del server e non ha questa limitazione.
