/* Elenco di nomi modificabile: ogni voce è un'etichetta con la × per
   toglierla, e sotto un campo con + per aggiungerne. Al posto dei campi di
   testo con i valori separati da virgola (cartelle ed estensioni escluse),
   dove bastava una virgola di troppo per sbagliare. Senza dipendenze.

     const w = Elenco.crea({tipo, valori, segnaposto, id, vuoto, onchange})
       w.el          il nodo da inserire nella pagina
       w.valori()    le voci, nell'ordine in cui sono state messe
       w.imposta(v)  sostituisce le voci (senza chiamare onchange)
     Elenco.dividi(tipo, testo) → [voci]   da un testo salvato alle voci
     Elenco.unisci(tipo, voci)  → testo    il contrario, nel formato del server

   tipo: 'cartelle' (nomi di cartella, separati da virgola) o 'estensioni'
   (".eml", separate da spazio). Le voci si normalizzano come fa il server
   (config.elenco_cartelle / elenco_estensioni): minuscole, «*.EML» diventa
   «.eml», «Node_Modules/» diventa «node_modules». Incollare «a, b, c»
   aggiunge tre voci. Il testo scritto e non ancora aggiunto entra quando il
   campo perde il fuoco: chi scrive e poi preme Salva non lo perde. */
(() => {
  const TIPI = {
    cartelle: {
      separa: /[,;\n]+/, unisci: ', ',
      voce: t => {
        const v = t.trim().replace(/^\/+|\/+$/g, '').trim().toLowerCase();
        if (v.includes('/')) return {errore: `«${t.trim()}» è un percorso: scrivi solo il nome della cartella.`};
        return {voce: v};
      },
    },
    estensioni: {
      separa: /[\s,;]+/, unisci: ' ',
      voce: t => {
        let v = t.trim().replace(/^\*+/, '').toLowerCase();
        if (v === '.') v = '';
        if (v.includes('/')) return {errore: `«${t.trim()}» non è un'estensione.`};
        return {voce: v && !v.startsWith('.') ? '.' + v : v};
      },
    },
  };

  const el = (tag, cls, testo) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (testo != null) n.textContent = testo;
    return n;
  };

  function dividi(tipo, testo){
    const t = TIPI[tipo];
    const voci = [];
    for (const pezzo of String(testo || '').split(t.separa)){
      const {voce} = t.voce(pezzo);
      if (voce && !voci.includes(voce)) voci.push(voce);
    }
    return voci;
  }

  const unisci = (tipo, voci) => voci.join(TIPI[tipo].unisci);

  function crea(o){
    const t = TIPI[o.tipo];
    let voci = [...(o.valori || [])];

    const box = el('div', 'elenco');
    const lista = el('ul', 'elenco-voci');
    lista.setAttribute('aria-live', 'polite');
    const riga = el('div', 'elenco-aggiungi');
    const campo = el('input', 'elenco-nuovo');
    campo.type = 'text';
    campo.autocomplete = 'off';
    campo.spellcheck = false;
    campo.placeholder = o.segnaposto || '';
    if (o.id) campo.id = o.id;
    const piu = el('button', 'elenco-piu');
    piu.type = 'button';
    piu.title = 'Aggiungi';
    piu.setAttribute('aria-label', 'Aggiungi');
    piu.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
      + 'stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';
    const errore = el('div', 'elenco-errore');
    errore.hidden = true;
    riga.append(campo, piu);
    box.append(lista, riga, errore);

    function disegna(){
      lista.innerHTML = '';
      if (!voci.length){
        lista.append(el('li', 'elenco-vuoto', o.vuoto || 'Nessuna voce'));
        return;
      }
      for (const v of voci){
        const li = el('li', 'elenco-voce');
        li.dataset.voce = v;
        const x = el('button', 'elenco-togli');
        x.type = 'button';
        x.title = `Togli ${v}`;
        x.setAttribute('aria-label', `Togli ${v}`);
        x.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
          + 'stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';
        x.onclick = () => {
          voci = voci.filter(w => w !== v);
          disegna();
          o.onchange?.(voci);
          campo.focus();
        };
        li.append(el('span', 'elenco-nome', v), x);
        lista.append(li);
      }
    }

    // aggiunge quel che è scritto nel campo; false se c'era un errore
    function aggiungi(){
      const testo = campo.value;
      if (!testo.trim()) return true;
      const nuove = [];
      let gia = null;
      for (const pezzo of testo.split(t.separa)){
        if (!pezzo.trim()) continue;
        const r = t.voce(pezzo);
        if (r.errore){
          errore.textContent = r.errore;
          errore.hidden = false;
          campo.classList.add('sbagliato');
          return false;
        }
        if (!r.voce) continue;
        if (voci.includes(r.voce) || nuove.includes(r.voce)) gia = r.voce;
        else nuove.push(r.voce);
      }
      campo.value = '';
      if (nuove.length){
        voci = [...voci, ...nuove];
        disegna();
        o.onchange?.(voci);
      }
      // una voce che c'era già: la si fa vedere invece di duplicarla
      if (gia){
        const li = lista.querySelector(`[data-voce="${CSS.escape(gia)}"]`);
        li?.classList.remove('gia');
        void li?.offsetWidth;
        li?.classList.add('gia');
      }
      return true;
    }

    const pulisci = () => { errore.hidden = true; campo.classList.remove('sbagliato'); };
    campo.addEventListener('input', pulisci);
    // Invio aggiunge (e dentro un dialogo non lo invia); a campo vuoto lascia fare
    campo.addEventListener('keydown', e => {
      if (e.key !== 'Enter' || !campo.value.trim()) return;
      e.preventDefault();
      e.stopPropagation();
      aggiungi();
    });
    campo.addEventListener('blur', () => aggiungi());
    piu.onclick = () => { aggiungi(); campo.focus(); };

    disegna();
    return {
      el: box,
      valori: () => { aggiungi(); return [...voci]; },
      imposta: v => { voci = [...v]; campo.value = ''; pulisci(); disegna(); },
    };
  }

  window.Elenco = {crea, dividi, unisci};
})();
