/* Finestre di conferma, avviso e richiesta di NIKI, al posto di confirm(),
   alert() e prompt() del browser. Condiviso da tutte le pagine (si carica
   prima dello script della pagina) e senza dipendenze.

     await Dialogo.conferma({titolo, testo, oggetto, elenco, via, resta, nota,
                             ok, annulla, tono, icona})       → true / false
     await Dialogo.avviso({titolo, testo, tono, icona, ok})    → (niente)
     await Dialogo.errore(testo, titolo)                       → (niente)
     await Dialogo.chiedi({titolo, testo, etichetta, valore, segnaposto,
                           mono, ok, icona})                   → testo / null
     await Dialogo.modulo({titolo, testo, scelte, etichetta, valore,
                           segnaposto, mono, campi, nota, ok, icona})
                           → {scelte: [valori spuntati], testo, campi: [testi]} / null

   scelte: caselle da spuntare, [{valore, nome, dettaglio, scelto}]; nel
   modulo il campo di testo (se c'è un'etichetta) può restare vuoto.
   campi: più campi di testo, [{etichetta, valore, segnaposto, mono}], al
   posto di quello solo; i testi tornano in ``campi``, nello stesso ordine
   (``testo`` è il primo). Un campo con ``elenco: 'cartelle'|'estensioni'``
   è un elenco di voci con × e + (elenco.js, da caricare nella pagina):
   ``valore`` sono le voci, e torna il testo nel formato del server.

   tono: 'pericolo' (bottone rosso), 'attenzione', 'info', 'errore'.
   oggetto: la cosa su cui si agisce, in evidenza (stringa o {nome, dettaglio}).
   via / resta: conseguenze in elenco, cosa sparisce e cosa resta com'è.
   Tutto il testo entra come textContent: niente HTML dai chiamanti. */
(() => {
  const SVG = {
    cestino:   '<path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12"/><path d="M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7"/>',
    scollega:  '<path d="M9 17H7A5 5 0 0 1 7 7h2"/><path d="M15 7h2a5 5 0 0 1 4 8"/><path d="M8 12h3"/><path d="M3 3l18 18"/>',
    attenzione:'<path d="M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    errore:    '<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/>',
    info:      '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    posta:     '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    reimposta: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>',
    nuovo:     '<path d="M12 5v14M5 12h14"/>',
    cartella:  '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    via:       '<path d="M6 12h12"/>',
    resta:     '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    nota:      '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  };
  const ICONA_TONO = {pericolo: 'cestino', attenzione: 'attenzione', info: 'info', errore: 'errore'};
  const icona = (nome, px) =>
    `<svg width="${px}" height="${px}" viewBox="0 0 24 24" fill="none" stroke="currentColor" ` +
    `stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${SVG[nome] || ''}</svg>`;

  let progressivo = 0;
  const el = (tag, cls, testo) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (testo != null) n.textContent = testo;
    return n;
  };

  function apri(o, tipo){
    const id = 'dlg' + (++progressivo);
    const tono = o.tono || (tipo === 'chiedi' ? 'info' : 'attenzione');
    const d = el('dialog', `dlg dlg--${tono}`);
    d.setAttribute('aria-labelledby', id + '-t');
    const campi = tipo === 'chiedi' ? [o]
      : tipo === 'modulo' ? (o.campi || (o.etichetta ? [o] : [])) : [];
    if (tipo !== 'chiedi' && tipo !== 'modulo') d.setAttribute('role', 'alertdialog');

    const carta = el('form', 'dlg-carta');
    carta.method = 'dialog';
    const testa = el('div', 'dlg-testa');
    const ic = el('span', 'dlg-icona');
    ic.innerHTML = icona(o.icona || ICONA_TONO[tono] || 'info', 22);
    const corpo = el('div', 'dlg-corpo');
    const h = el('h2', 'dlg-titolo', o.titolo || '');
    h.id = id + '-t';
    corpo.append(h);

    // testo: paragrafi separati da una riga vuota
    if (o.testo){
      const box = el('div', 'dlg-testo');
      box.id = id + '-d';
      String(o.testo).split(/\n\s*\n/).forEach(p => box.append(el('p', null, p.trim())));
      corpo.append(box);
      d.setAttribute('aria-describedby', box.id);
    }
    if (o.oggetto){
      const ogg = typeof o.oggetto === 'string' ? {nome: o.oggetto} : o.oggetto;
      const box = el('div', 'dlg-oggetto');
      box.append(el('span', 'dlg-oggetto-nome', ogg.nome));
      if (ogg.dettaglio) box.append(el('span', 'dlg-oggetto-dett', ogg.dettaglio));
      corpo.append(box);
    }
    if (o.elenco?.length){
      const max = o.massimo || 40;
      const ul = el('ul', 'dlg-elenco');
      o.elenco.slice(0, max).forEach(v => ul.append(el('li', null, v)));
      if (o.elenco.length > max) ul.append(el('li', 'altri', `… e altri ${o.elenco.length - max}`));
      corpo.append(ul);
    }
    const punti = [...(o.via || []).map(t => ['via', t]), ...(o.resta || []).map(t => ['resta', t])];
    if (punti.length){
      const ul = el('ul', 'dlg-punti');
      for (const [k, t] of punti){
        const li = el('li', k);
        const seg = el('span', 'segno');
        seg.innerHTML = icona(k, 12);
        li.append(seg, el('span', null, t));
        ul.append(li);
      }
      corpo.append(ul);
    }
    const caselle = [];
    if (tipo === 'modulo' && o.scelte?.length){
      const box = el('div', 'dlg-scelte');
      for (const sc of o.scelte){
        const lab = el('label', 'dlg-scelta');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.value = sc.valore;
        cb.checked = !!sc.scelto;
        caselle.push(cb);
        const txt = el('span', 'dlg-scelta-txt');
        txt.append(el('span', 'dlg-scelta-nome', sc.nome));
        if (sc.dettaglio) txt.append(el('span', 'dlg-scelta-dett', sc.dettaglio));
        lab.append(cb, txt);
        box.append(lab);
      }
      corpo.append(box);
    }
    const inputs = campi.map(c => {
      if (c.elenco){
        const box = el('div', 'dlg-campo');
        const w = Elenco.crea({tipo: c.elenco, valori: c.valore || [], segnaposto: c.segnaposto,
                               id: `${id}-e${corpo.children.length}`, vuoto: c.vuoto});
        if (c.etichetta){
          const et = el('label', 'dlg-etichetta', c.etichetta);
          et.htmlFor = w.el.querySelector('input').id;
          box.append(et);
        }
        box.append(w.el);
        corpo.append(box);
        return {get value(){ return Elenco.unisci(c.elenco, w.valori()); }};
      }
      const lab = el('label', 'dlg-campo');
      if (c.etichetta) lab.append(el('span', 'dlg-etichetta', c.etichetta));
      const inp = el('input', c.mono ? 'mono' : '');
      inp.type = 'text';
      inp.autocomplete = 'off';
      inp.spellcheck = !c.mono;
      inp.value = c.valore || '';
      inp.placeholder = c.segnaposto || '';
      if (c.lunghezza) inp.maxLength = c.lunghezza;
      lab.append(inp);
      corpo.append(lab);
      return inp;
    });
    const input = inputs[0] || null;
    if (o.nota){
      const n = el('div', 'dlg-nota');
      const s = el('span');
      s.innerHTML = icona('nota', 15);
      n.append(s, el('span', null, o.nota));
      corpo.append(n);
    }
    testa.append(ic, corpo);

    const piede = el('div', 'dlg-piede');
    const bOk = el('button', 'dlg-btn dlg-ok', o.ok || (tipo === 'avviso' ? 'Ho capito' : 'Conferma'));
    bOk.type = 'submit';
    bOk.value = 'ok';
    if (tipo !== 'avviso'){
      const bNo = el('button', 'dlg-btn dlg-annulla', o.annulla || 'Annulla');
      bNo.type = 'button';
      bNo.onclick = () => chiudi(false);
      piede.append(bNo);
    }
    piede.append(bOk);
    carta.append(testa, piede);
    d.append(carta);

    if (input && tipo === 'chiedi'){
      const aggiorna = () => { bOk.disabled = !input.value.trim(); };
      input.addEventListener('input', aggiorna);
      aggiorna();
    }

    let fine;
    const esito = new Promise(res => { fine = res; });
    const prima = document.activeElement;
    let chiusa = false;
    function chiudi(ok){
      if (chiusa) return;
      chiusa = true;
      fine(tipo === 'chiedi' ? (ok ? input.value.trim() : null)
         : tipo === 'modulo' ? (ok ? {scelte: caselle.filter(c => c.checked).map(c => c.value),
                                      testo: input ? input.value.trim() : '',
                                      campi: inputs.map(i => i.value.trim())} : null)
         : ok);
      d.classList.add('esce');
      const via = () => {
        d.close(); d.remove();
        if (prima?.isConnected) prima.focus?.({preventScroll: true});
      };
      if (matchMedia('(prefers-reduced-motion: reduce)').matches) via();
      else setTimeout(via, 140);
    }
    carta.addEventListener('submit', e => {
      e.preventDefault();
      if (tipo === 'chiedi' && !input.value.trim()) return;
      chiudi(true);
    });
    // Esc chiude solo questa finestra: le modali sotto (caselle, calendari,
    // eliminazione file) ascoltano Esc su document e si chiuderebbero anche loro
    d.addEventListener('keydown', e => {
      if (e.key !== 'Escape') return;
      e.preventDefault(); e.stopPropagation();
      chiudi(false);
    });
    d.addEventListener('cancel', e => { e.preventDefault(); chiudi(false); });
    // clic sul velo attorno = annulla (non se il clic è partito dentro, es. selezionando il testo)
    let giuFuori = false;
    d.addEventListener('mousedown', e => { giuFuori = e.target === d; });
    d.addEventListener('click', e => { if (e.target === d && giuFuori) chiudi(false); });

    document.body.append(d);
    d.showModal();
    // per le azioni distruttive il fuoco parte da «Annulla»: un Invio di troppo non cancella
    if (tipo === 'chiedi'){ input.focus(); input.select(); }
    else if (caselle.length) caselle[0].focus();
    else if (tono === 'pericolo' && tipo === 'conferma') piede.querySelector('.dlg-annulla').focus();
    else bOk.focus();
    return esito;
  }

  window.Dialogo = {
    conferma: o => apri(o, 'conferma'),
    avviso: o => apri(typeof o === 'string' ? {testo: o} : o, 'avviso').then(() => {}),
    errore: (testo, titolo) => apri({titolo: titolo || 'Operazione non riuscita',
                                     testo, tono: 'errore'}, 'avviso').then(() => {}),
    chiedi: o => apri(o, 'chiedi'),
    modulo: o => apri(o, 'modulo'),
  };
})();
