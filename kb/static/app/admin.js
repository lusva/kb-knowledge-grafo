const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

let msgTimer = 0;
function setMsg(text, tipo){
  const el = $('#msg');
  clearTimeout(msgTimer);
  el.textContent = text;
  el.className = tipo || '';
  // una conferma sparisce da sola; errori e modifiche pendenti restano
  if (tipo === 'ok') msgTimer = setTimeout(() => { if (el.className === 'ok') aggiornaPendenti(); }, 4000);
}

/* fetch autenticata: aggiunge il token CSRF e torna alla login se la sessione è scaduta */
const csrftoken = () => document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/)?.[1] || '';
async function authFetch(p, opt = {}){
  opt.headers = {...(opt.headers || {}), 'X-CSRFToken': csrftoken()};
  const r = await fetch(p, opt);
  if (r.status === 401){
    location.href = '/accounts/login/?next=' + encodeURIComponent(location.pathname);
    throw new Error('sessione scaduta');
  }
  if (r.status === 403) throw new Error('permessi insufficienti: serve un account staff');
  return r;
}

/* capacità che un campo modello richiede (come le dichiara Ollama in /api/show) */
const CAPACITA = {embed_model: 'embedding', vision_model: 'vision', ocr_model: 'vision'};
/* cosa significa lasciare vuoto un campo modello senza default */
const VUOTO = {rewrite_model: 'Nessuno: ricerca senza riscrittura'};
/* interruttori 0/1: si scelgono da un menu, non si scrivono come numeri */
const BOOLEANI = new Set(['chunk_header', 'hybrid', 'query_rewrite']);
const booleano = f => f.type === 'int' && (BOOLEANI.has(f.key) || (/\b0 = /.test(f.help) && /\b1 = /.test(f.help)));
/* elenchi di nomi: voci con × e un campo con + (elenco.js) invece del testo
   separato da virgole */
const ELENCHI = {escludi_cartelle: 'cartelle', escludi_ext: 'estensioni'};

const addOption = (parent, value, text) => {
  const o = document.createElement('option');
  o.value = value; o.textContent = text; parent.appendChild(o);
};

function modelSelect(f, models){
  const sel = document.createElement('select');
  addOption(sel, '', f.default ? `Predefinito · ${f.default}` : (VUOTO[f.key] || 'Nessuno'));
  const serve = CAPACITA[f.key] || 'completion';
  // un modello che non dichiara le capacità (Ollama vecchio) resta tra gli adatti
  const adatto = m => !m.capabilities || m.capabilities.includes(serve);
  const nome = m => m.name + (serve === 'completion' && m.capabilities?.includes('vision')
    ? ' · vede le immagini' : '');
  const gruppi = [['Adatti', models.filter(adatto)],
                  ['Altri modelli installati', models.filter(m => !adatto(m))]];
  for (const [titolo, lista] of gruppi){
    if (!lista.length) continue;
    const g = document.createElement('optgroup'); g.label = titolo;
    lista.forEach(m => addOption(g, m.name, nome(m)));
    sel.appendChild(g);
  }
  // un valore salvato che non è più installato resta visibile, non si perde in silenzio
  const valore = f.overridden ? f.value : '';
  if (valore && !models.some(m => m.name === valore))
    addOption(sel, valore, `${valore} · non installato`);
  sel.value = valore;
  return sel;
}

function booleanSelect(f){
  const sel = document.createElement('select');
  addOption(sel, '', `Predefinito · ${Number(f.default) ? 'attivo' : 'disattivato'}`);
  addOption(sel, '1', 'Attivo');
  addOption(sel, '0', 'Disattivato');
  sel.value = f.overridden ? String(f.value) : '';
  return sel;
}

/* campi di testo con un elenco di valori possibili (cfg.scelte): un menu, con
   il predefinito in cima e un valore salvato che non c'è più ancora visibile */
function sceltaSelect(f, s){
  const sel = document.createElement('select');
  addOption(sel, '', `Predefinita · ${s.predefinito}`);
  s.opzioni.forEach(o => addOption(sel, o.valore, o.nome));
  const valore = f.overridden ? String(f.value) : '';
  if (valore && !s.opzioni.some(o => o.valore === valore))
    addOption(sel, valore, `${valore} · non disponibile`);
  sel.value = valore;
  return sel;
}

/* Un elenco al posto del campo di testo. Il valore salvato resta il testo che
   si scriveva prima, in un input nascosto che il resto della pagina legge
   come gli altri: vuoto = predefinito (le voci predefinite si vedono già
   nell'elenco), e un elenco svuotato apposta diventa "," perché il vuoto
   tornerebbe al predefinito invece di non escludere niente. */
function elencoCampo(f, tipo){
  const predef = Elenco.dividi(tipo, f.default);
  const uguali = (a, b) => a.length === b.length && a.every(v => b.includes(v));
  const codifica = voci => uguali(voci, predef) ? '' : voci.length ? Elenco.unisci(tipo, voci) : ',';
  const input = document.createElement('input');
  input.type = 'hidden';
  const lista = Elenco.crea({
    tipo, id: 'in-' + f.key, vuoto: 'Nessuna',
    valori: f.overridden ? Elenco.dividi(tipo, String(f.value)) : predef,
    segnaposto: tipo === 'cartelle' ? 'Nome della cartella, es. dist' : 'Estensione, es. .zip',
    onchange: voci => {
      input.value = codifica(voci);
      input.dispatchEvent(new Event('input', {bubbles: true}));
    },
  });
  input.value = codifica(lista.valori());
  input.ripristina = () => lista.imposta(predef);   // «personalizzato ×»
  return [input, lista];
}

const slug = s => s.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9]+/g, '-');

/* ---------------- resa ---------------- */
let iniziali = {};        // key -> valore al caricamento, per sapere cosa è cambiato
let campi = [];           // {key, el, testo} per la ricerca

function render(cfg){
  const body = $('#cfg-body');
  body.innerHTML = '';
  iniziali = {}; campi = [];
  const models = cfg.ollama_models || [];

  const note = $('#models-note');
  note.className = models.length ? '' : 'err';
  const servizio = cfg.servizio || 'Ollama';
  note.textContent = models.length
    ? (servizio === 'Ollama'
        ? `${models.length} modelli Ollama installati: i campi modello li propongono in un menu.`
        : `${models.length} modelli dal server LLM: i campi modello li propongono in un menu.`)
    : `${servizio} non raggiungibile: l’elenco dei modelli disponibili non c’è.`;

  const gruppi = [];   // {nome, id, el}
  let gruppo = null;
  for (const f of cfg.fields){
    if (!gruppo || gruppo.nome !== f.group){
      const div = document.createElement('section');
      div.className = 'group'; div.id = 'g-' + slug(f.group);
      div.setAttribute('aria-labelledby', div.id + '-t');
      const h = document.createElement('h2'); h.id = div.id + '-t'; h.textContent = f.group;
      div.appendChild(h);
      body.appendChild(div);
      gruppo = {nome: f.group, id: div.id, el: div};
      gruppi.push(gruppo);
    }

    const field = document.createElement('div');
    field.className = 'field'; field.id = 'c-' + f.key; field.dataset.gruppo = gruppo.id;
    const lbl = document.createElement('label'); lbl.className = 'lbl';
    lbl.htmlFor = 'in-' + f.key; lbl.textContent = f.label;
    if (f.overridden){
      const o = document.createElement('button');
      o.type = 'button'; o.className = 'over';
      o.title = `Torna al predefinito (${f.default === '' ? 'vuoto' : f.default})`;
      o.innerHTML = 'personalizzato <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" '
        + 'stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';
      o.onclick = () => {
        input.ripristina?.();
        input.value = ''; input.dispatchEvent(new Event('input', {bubbles: true})); input.focus();
      };
      lbl.appendChild(o);
    }
    // modelli: menu dei modelli installati; se Ollama non risponde, campo libero
    let input;
    const scelta = cfg.scelte?.[f.key];
    let lista = null;
    if (f.is_model && models.length) input = modelSelect(f, models);
    else if (scelta) input = sceltaSelect(f, scelta);
    else if (ELENCHI[f.key]) [input, lista] = elencoCampo(f, ELENCHI[f.key]);
    else if (booleano(f)) input = booleanSelect(f);
    else {
      input = document.createElement('input');
      input.value = f.overridden ? String(f.value) : '';
      input.placeholder = (f.default === '' ? 'vuoto' : f.default) + ' (predefinito)';
      if (f.type === 'str') input.type = 'text';
      else { input.type = 'number'; input.inputMode = 'decimal'; if (f.type === 'float') input.step = '0.01'; }
    }
    if (!lista) input.id = 'in-' + f.key;
    input.dataset.key = f.key;
    iniziali[f.key] = input.value;
    const ctrl = document.createElement('div'); ctrl.className = 'ctrl'; ctrl.appendChild(input);
    if (lista) ctrl.prepend(lista.el);
    if (scelta?.avviso){
      const av = document.createElement('div'); av.className = 'avviso';
      av.textContent = scelta.avviso;
      ctrl.appendChild(av);
    }
    const hint = document.createElement('div'); hint.className = 'hint';
    hint.textContent = f.help || 'Vuoto = usa il predefinito.';
    field.append(lbl, hint, ctrl);
    gruppo.el.appendChild(field);
    campi.push({key: f.key, el: field, testo: slug(`${f.label} ${f.help} ${f.key} ${f.group}`)});
  }

  const nessuno = document.createElement('p');
  nessuno.id = 'nessuno'; nessuno.hidden = true;
  body.appendChild(nessuno);

  renderIndice(gruppi);
  osservaSezioni(gruppi);
  filtra($('#cerca').value);
  aggiornaPendenti();
  evidenziaDaHash();
}

function renderIndice(gruppi){
  const nav = $('#indice-voci');
  nav.innerHTML = '';
  for (const g of gruppi){
    const a = document.createElement('a');
    a.href = '#' + g.id; a.dataset.gruppo = g.id;
    a.textContent = g.nome;
    a.onclick = e => {
      e.preventDefault();
      g.el.scrollIntoView({behavior: 'smooth', block: 'start'});
      history.replaceState(null, '', '#' + g.id);
      segnaAttiva(g.id);
    };
    nav.appendChild(a);
  }
}

function segnaAttiva(id){
  $$('#indice-voci a').forEach(a => a.classList.toggle('attiva', a.dataset.gruppo === id));
}

/* l'indice segue lo scorrimento: è attiva la sezione più in alto tra quelle visibili */
let osservatore = null;
function osservaSezioni(gruppi){
  osservatore?.disconnect();
  const visibili = new Set();
  osservatore = new IntersectionObserver(entries => {
    for (const e of entries) e.isIntersecting ? visibili.add(e.target.id) : visibili.delete(e.target.id);
    const prima = gruppi.find(g => visibili.has(g.id) && !g.el.hidden);
    if (prima) segnaAttiva(prima.id);
  }, {rootMargin: '-64px 0px -55% 0px', threshold: 0});
  gruppi.forEach(g => osservatore.observe(g.el));
}

/* ---------------- ricerca ---------------- */
function filtra(q){
  const parole = slug(q.trim()).split('-').filter(Boolean);
  let trovati = 0;
  for (const c of campi){
    const ok = parole.every(p => c.testo.includes(p));
    c.el.hidden = !ok;
    if (ok) trovati++;
  }
  for (const g of $$('.group')){
    g.hidden = !g.querySelector('.field:not([hidden])');
    $(`#indice-voci a[data-gruppo="${g.id}"]`).hidden = g.hidden;
  }
  const nessuno = $('#nessuno');
  nessuno.hidden = trovati > 0;
  if (!trovati) nessuno.textContent = `Nessuna impostazione corrisponde a «${q.trim()}».`;
}
$('#cerca').addEventListener('input', e => filtra(e.target.value));
$('#cerca').addEventListener('keydown', e => {
  if (e.key === 'Escape' && e.target.value){ e.target.value = ''; filtra(''); }
});
// «/» porta il cursore nella ricerca, come nei siti di documentazione
document.addEventListener('keydown', e => {
  if (e.key === '/' && !/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)){
    e.preventDefault(); $('#cerca').focus();
  }
});

/* #c-<chiave> o #<chiave> nell'URL: scorre fino al campo e lo evidenzia */
function evidenziaDaHash(){
  const h = location.hash.slice(1);
  if (!h) return;
  const el = $('#' + CSS.escape(h)) || $('#c-' + CSS.escape(h));
  if (!el) return;
  el.scrollIntoView({block: el.classList.contains('field') ? 'center' : 'start'});
  if (el.classList.contains('field')){
    el.classList.add('evidenza');
    el.querySelector('input, select')?.focus({preventScroll: true});
  }
}

/* ---------------- modifiche pendenti ---------------- */
function modificati(){
  return $$('#cfg-body [data-key]').filter(inp => inp.value.trim() !== iniziali[inp.dataset.key]);
}
function aggiornaPendenti(){
  const n = modificati().length;
  $$('.field').forEach(f => f.classList.remove('cambiato'));
  modificati().forEach(inp => inp.closest('.field').classList.add('cambiato'));
  $('#btn-save').disabled = n === 0;
  const el = $('#msg');
  if (el.className === 'err') return;   // l'errore resta finché non si risalva
  if (n === 0){ el.textContent = ''; el.className = ''; return; }
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  el.className = 'pendenti';
  el.innerHTML = `${n} ${n === 1 ? 'modifica non salvata' : 'modifiche non salvate'} · <kbd>${mac ? '⌘' : 'Ctrl'}</kbd>+<kbd>S</kbd> salva`;
}
$('#cfg-body').addEventListener('input', () => { if ($('#msg').className === 'err') setMsg(''); aggiornaPendenti(); });
window.addEventListener('beforeunload', e => { if (modificati().length) e.preventDefault(); });

/* ---------------- caricamento e salvataggio ---------------- */
async function load(){
  try{
    const r = await authFetch('/api/config');
    if (!r.ok) throw new Error(r.statusText);
    render(await r.json());
  }catch(e){ $('#cfg-body').textContent = 'Errore: ' + e.message; }
}

async function save(values){
  setMsg('Salvo…');
  $('#btn-save').disabled = $('#btn-reset').disabled = true;
  try{
    const r = await authFetch('/api/config', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({values})
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok && !data.errors) throw new Error(`errore ${r.status} ${r.statusText}`);
    if (!r.ok){
      const errati = Object.keys(data.errors || {});
      for (const inp of $$('#cfg-body [data-key]'))
        inp.classList.toggle('invalid', errati.includes(inp.dataset.key));
      const nomi = errati.map(k => $(`#c-${CSS.escape(k)} .lbl`)?.firstChild?.textContent || k);
      setMsg('Valori non validi: ' + nomi.join(', '), 'err');
      $(`#c-${CSS.escape(errati[0])}`)?.scrollIntoView({block: 'center', behavior: 'smooth'});
    } else {
      render(data);
      setMsg('Salvato ✓', 'ok');
    }
  }catch(e){ setMsg('Errore: ' + e.message, 'err'); }
  $('#btn-reset').disabled = false;
  if ($('#msg').className !== 'pendenti') $('#btn-save').disabled = modificati().length === 0;
}

function collect(reset){
  const values = {};
  for (const inp of $$('#cfg-body [data-key]'))
    values[inp.dataset.key] = reset ? '' : inp.value.trim();
  return values;
}

$('#btn-save').onclick = () => save(collect(false));
$('#btn-reset').onclick = () => {
  const n = Object.values(iniziali).filter(v => v !== '').length + modificati().length;
  if (!n){ setMsg('Nessun valore personalizzato: sono già tutti predefiniti.', 'ok'); return; }
  Dialogo.conferma({
    titolo: 'Ripristinare i valori predefiniti?', tono: 'pericolo', icona: 'reimposta',
    testo: 'Tutte le impostazioni tornano ai valori predefiniti.',
    via: ['Le personalizzazioni vengono tolte subito'], ok: 'Ripristina'})
    .then(ok => { if (ok) save(collect(true)); });
};
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's'){
    e.preventDefault();
    // il nome scritto in un elenco e non ancora aggiunto entra quando il campo perde il fuoco
    if (document.activeElement?.classList.contains('elenco-nuovo')) document.activeElement.blur();
    if (!$('#btn-save').disabled) save(collect(false));
  }
});

load();
