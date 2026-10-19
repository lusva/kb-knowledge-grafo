const $ = s => document.querySelector(s);

/* Elementi cliccabili che non sono <button> — fonti, citazioni, voci dello
   storico, le × — sono span e div con un onclick: con la tastiera non ci si
   arrivava e un lettore di schermo non li presentava come azioni. Qui prendono
   il ruolo e il tabindex di un pulsante; Invio e Spazio li attivano. */
function cliccabile(el, etichetta){
  el.setAttribute('role', 'button');
  el.tabIndex = 0;
  if (etichetta) el.setAttribute('aria-label', etichetta);
  return el;
}
document.addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.matches?.('[role="button"]')){
    e.preventDefault();
    e.target.click();
  }
});
/* annunci per i lettori di schermo */
function annuncia(testo){
  const a = $('#annuncio');
  a.textContent = '';
  setTimeout(() => { a.textContent = testo; }, 50);   // stesso testo due volte: va riletto
}

/* Esiti di importazioni, indicizzazioni ed eliminazioni: un avviso che
   compare in basso e sparisce da solo (resta finché ci si passa sopra). */
function avvisa(testo){
  const box = $('#avvisi');
  const d = document.createElement('div');
  d.className = 'avviso';
  d.textContent = testo;
  const chiudi = document.createElement('button');
  chiudi.type = 'button';
  chiudi.className = 'x';
  chiudi.setAttribute('aria-label', 'Chiudi l\'avviso');
  chiudi.textContent = '×';
  chiudi.onclick = () => d.remove();
  d.appendChild(chiudi);
  box.appendChild(d);
  annuncia(testo);
  let timer = null;
  const programma = () => { timer = setTimeout(() => d.remove(), 9000 + testo.length * 40); };
  d.addEventListener('mouseenter', () => clearTimeout(timer));
  d.addEventListener('mouseleave', programma);
  programma();
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
  return r;
}
const api = (p, opt) => authFetch(p, opt).then(async r => {
  if (!r.ok) throw new Error((await r.json().catch(()=>({}))).detail || r.statusText);
  return r.headers.get('content-type')?.includes('json') ? r.json() : r.text();
});

/* knowledge attiva: viene inviata come parametro ?kb=… a tutte le API */
let currentKb = new URLSearchParams(location.search).get('kb') || localStorage.getItem('knowledge-grafo-kb') || '';
let currentKbName = '';
/* Filtri della ricerca interna e della lista dei documenti. */
/* Restrizione a un sottoinsieme: un documento scelto nell'anteprima, oppure
   un'entità cliccata nel grafo (tema, persona, organizzazione). Delle entità
   viaggia il NOME: quali documenti contengano lo decide il server, perché un
   tema può valerne trecento. */
let scelta = null;         // {chiave: 'docs'|'tema'|'persona'|'org', valore, etichetta}
function filtri(){
  const f = {};
  const tipo = $('#f-tipo').value, giorni = $('#f-periodo').value;
  if (scelta?.entita) f.entita = scelta.entita;
  else if (scelta) f[scelta.chiave] = scelta.valore;
  if (tipo) f.tipi = tipo;
  if (giorni){
    const d = new Date(Date.now() - giorni * 86400000);
    f.da = d.toISOString().slice(0, 10);
  }
  return f;
}
function filtriQuery(){
  const f = filtri();
  return Object.keys(f).map(k => '&' + k + '=' + encodeURIComponent(f[k])).join('');
}
function segnalaFiltri(){
  $('#f-tipo').classList.toggle('attivo', !!$('#f-tipo').value);
  $('#f-periodo').classList.toggle('attivo', !!$('#f-periodo').value);
}
const ICONA = {docs:'📄', tema:'◆', persona:'●', org:'▲', tag:'#'};
function impostaScelta(chiave, valore, etichetta, entita){
  scelta = valore ? {chiave, valore, etichetta, entita} : null;
  $('#f-doc').hidden = !scelta;
  $('#f-doc-nome').textContent = scelta ? (ICONA[chiave] || '') + ' ' + etichetta : '';
  $('#f-doc').title = scelta
    ? 'La ricerca usa solo i documenti di: ' + etichetta + ' — × per tornare a tutti'
    : '';
  aggiornaLinkChat();
}
$('#f-doc').querySelector('.x').onclick = () => { impostaScelta(null, null, ''); aggiornaDocumenti(); };
$('#f-tipo').addEventListener('change', () => { segnalaFiltri(); aggiornaDocumenti(); });
$('#f-periodo').addEventListener('change', () => { segnalaFiltri(); aggiornaDocumenti(); });

function withKb(url){
  if (!currentKb) return url;
  return url + (url.includes('?') ? '&' : '?') + 'kb=' + encodeURIComponent(currentKb);
}

function linkChat(documento){
  const query = new URLSearchParams();
  if (currentKb) query.set('kb', currentKb);
  if (documento) query.set('doc', documento);
  else if (scelta?.entita) query.set('entita', scelta.entita);
  else if (scelta) query.set(scelta.chiave, scelta.valore);
  return '/chat' + (query.size ? '?' + query : '');
}
function aggiornaLinkChat(){
  $('#chat-link').href = linkChat();
  $('#chat-link').title = scelta ? 'Fai una domanda sui documenti di: ' + scelta.etichetta : 'Fai una domanda sulla knowledge attiva';
}

/* Palette brand: temi Ambra, persone Magenta, organizzazioni Turchese Deep.
   Un arco prende il colore di ciò a cui porta: la persona verso il tema o
   verso la sua organizzazione. */
const NODE_COLORS = {documento:'#7C3AED', tema:'#D97706', tag:'#2563EB', persona:'#DB2777', org:'#0D9488'};
const EDGE_COLORS = {tema:'#D97706', tag:'#2563EB', documento:'#7C3AED', wikilink:'#7C3AED', link:'#7C3AED', persona:'#DB2777', org:'#0D9488'};

let Graph = null;
let thinking = false;   // durante una ricerca la camera non insegue il layout
const highlight = new Map();   // id nodo -> timestamp di fine evidenziazione

/* ---------------- menu a tendina ---------------- */
/* Un solo menu aperto per volta; si chiude con un clic fuori, con Esc (e il
   fuoco torna al pulsante) o con Tab. Aperto da tastiera, il fuoco va sulla
   prima voce; le frecce scorrono le voci visibili, Home ed End saltano agli
   estremi. */
let openMenuEl = null, openMenuBtn = null;
const vociMenu = menu => [...menu.querySelectorAll('button')]
  .filter(b => !b.disabled && b.offsetParent !== null);
function closeMenu(rifocalizza){
  if (!openMenuEl) return;
  const btn = openMenuBtn;
  openMenuEl.classList.remove('open');
  btn.setAttribute('aria-expanded', 'false');
  openMenuEl = openMenuBtn = null;
  if (rifocalizza) btn.focus();
}
function toggleMenu(menu, btn, daTastiera){
  const wasOpen = openMenuEl === menu;
  closeMenu();
  if (wasOpen) return;
  menu.classList.add('open');
  btn.setAttribute('aria-expanded', 'true');
  openMenuEl = menu; openMenuBtn = btn;
  if (daTastiera) vociMenu(menu)[0]?.focus();
}
document.addEventListener('click', () => closeMenu());
document.addEventListener('keydown', e => {
  if (!openMenuEl) return;
  if (e.key === 'Escape'){ e.preventDefault(); closeMenu(true); return; }
  if (e.key === 'Tab'){ closeMenu(); return; }
  const voci = vociMenu(openMenuEl);
  if (!voci.length) return;
  const i = voci.indexOf(document.activeElement);
  let j = null;
  if (e.key === 'ArrowDown') j = i < 0 ? 0 : (i + 1) % voci.length;
  else if (e.key === 'ArrowUp') j = i < 0 ? voci.length - 1 : (i - 1 + voci.length) % voci.length;
  else if (e.key === 'Home') j = 0;
  else if (e.key === 'End') j = voci.length - 1;
  if (j !== null){ e.preventDefault(); voci[j].focus(); }
});
document.querySelectorAll('.menu').forEach(m => m.addEventListener('click', e => e.stopPropagation()));

for (const [btn, menu] of [['#kb-btn', '#kb-menu'], ['#app-btn', '#app-menu']]){
  // e.detail vale 0 quando il clic viene da Invio o Spazio: allora il fuoco entra nel menu
  $(btn).onclick = e => { e.stopPropagation(); toggleMenu($(menu), $(btn), e.detail === 0); };
  $(btn).addEventListener('keydown', e => {
    if (e.key === 'ArrowDown' && openMenuEl !== $(menu)){ e.preventDefault(); toggleMenu($(menu), $(btn), true); }
  });
}

/* le voci di menu condividono gli stessi comandi in entrambe le tendine */
const ACTIONS = {
  'new':           () => newKnowledge(),
  'add-file':      () => addFromDisk('file'),
  'add-folder':    () => addFromDisk('folder'),
  'filtro':        () => apriFiltro(),
  'reindex':       () => reindex(),
  'temi':          () => rifaiTemi(),
  'del':           () => deleteKnowledge(),
  'del-files':     () => apriEliminaFile(),
  'settings':      () => { location.href = '/config'; },
  'utenti':        () => { location.href = '/admin/auth/user/'; },
  'logout':        () => logout(),
};
document.querySelectorAll('.menu button[data-act]').forEach(b => {
  b.onclick = () => { closeMenu(); ACTIONS[b.dataset.act](); };
});

/* ---------------- mappa ---------------- */
/* Temi, persone e organizzazioni (vedi kb/graph.py): poche centinaia di nodi,
   ognuno grande quanto i documenti che tocca. Cliccarne uno mostra la lista
   dei documenti collegati. I singoli documenti si aprono dalla lista e dalla ricerca. */
let grafoCorrente = {nodes:[], links:[]};

/* cresce con i documenti, ma senza che un tema enorme copra tutti gli altri */
const raggio = n => 3 + Math.min(Math.sqrt(n.documenti || 1) * 0.9, 16);

/* Tira ogni nodo verso il centro. Senza, i nodi senza archi (una persona con
   nessun tema ricorrente) scappano verso i bordi e
   l'inquadratura si allarga fino a rendere la mappa minuscola. */
function gravita(k){
  let nodi = [];
  const forza = alpha => {
    for (const n of nodi){ n.vx -= n.x * k * alpha; n.vy -= n.y * k * alpha; }
  };
  forza.initialize = ns => { nodi = ns; };
  return forza;
}

/* Tiene i nodi a distanza: i temi con largo margine, perché i loro nomi sono
   sempre scritti e altrimenti si accavallano al centro della mappa. Con un paio
   di centinaia di nodi il confronto fra tutte le coppie costa poco. */
const ingombro = n => raggio(n) + (n.type === 'tema' ? 65 : 5);
function separazione(){
  let nodi = [];
  const forza = () => {
    for (let i = 0; i < nodi.length; i++){
      for (let j = i + 1; j < nodi.length; j++){
        const a = nodi[i], b = nodi[j];
        const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
        const minimo = ingombro(a) + ingombro(b);
        if (d >= minimo) continue;
        const spinta = (minimo - d) / d * 0.25;
        a.vx -= dx * spinta; a.vy -= dy * spinta;
        b.vx += dx * spinta; b.vy += dy * spinta;
      }
    }
  };
  forza.initialize = ns => { nodi = ns; };
  return forza;
}

function initGraph(data){
  if (data){
    /* i nodi già disposti restano dove sono: dopo un'indicizzazione la mappa
       non si rimescola, si sistemano solo i nodi nuovi */
    const prima = new Map(grafoCorrente.nodes.map(n => [n.id, n]));
    for (const n of data.nodes){
      const p = prima.get(n.id);
      if (p && isFinite(p.x)){ n.x = p.x; n.y = p.y; }
    }
    grafoCorrente = data;
  }
  $('#empty-hint').classList.toggle('show', !grafoCorrente.nodes.length);
  if (!Graph){
    const pane = $('#graph-pane');
    Graph = ForceGraph()($('#graph'))
      /* senza misure esplicite il canvas nasce largo quanto la finestra, più del
         riquadro disponibile, e la mappa "centrata" finisce spostata a destra */
      .width(pane.clientWidth).height(pane.clientHeight)
      .backgroundColor('#F5F3EC')
      .cooldownTime(8000)
      // a layout fermo, l'ultima inquadratura (se l'utente non ha già spostato la vista)
      .onEngineStop(() => {
        if (fitUntil && !thinking && !highlight.size) Graph.zoomToFit(400, 90);
      })
      /* Il canvas si ridisegna solo quando qualcosa cambia (layout, zoom,
         passaggio del mouse), non 60 volte al secondo a mappa ferma. */
      .onRenderFramePre(() => {
        const now = performance.now();
        for (const [id, until] of highlight) if (now >= until) highlight.delete(id);
      })
      .onRenderFramePost(() => Graph.autoPauseRedraw(true))
      .nodeId('id')
      .nodeLabel(n => esc(n.type === 'documento' ? `${n.titolo || n.label} · clic per aprire`
        : `${n.titolo || n.label} — ${n.documenti} document${n.documenti === 1 ? 'o' : 'i'} · clic per vedere i documenti`))
      // l'area cliccabile coincide con il cerchio disegnato, anche per i nodi grandi
      .nodePointerAreaPaint((n, colore, ctx) => {
        ctx.fillStyle = colore;
        ctx.beginPath(); ctx.arc(n.x, n.y, raggio(n) + 2, 0, 2*Math.PI); ctx.fill();
      })
      .linkColor(l => (EDGE_COLORS[l.type] || '#8B8778') + (highlight.size ? '1F' : '59'))
      // persona-tema: più spesso quanti più documenti hanno in comune
      .linkWidth(l => l.type === 'tema' ? Math.min(1 + Math.round((l.weight || 0) / 10) / 2, 4) : 1)
      .onNodeClick(openNode)
      .nodeCanvasObject((n, ctx, scale) => {
        const r = raggio(n), acceso = highlight.has(n.id);
        ctx.globalAlpha = highlight.size && !acceso ? 0.2 : 1;
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, 2*Math.PI);
        ctx.fillStyle = NODE_COLORS[n.type] || '#7C3AED';
        ctx.fill();
        // anello attorno a temi e persone delle documenti trovati
        if (acceso){
          ctx.beginPath();
          ctx.arc(n.x, n.y, r + 4/scale, 0, 2*Math.PI);
          ctx.strokeStyle = 'rgba(124,58,237,.9)';   /* Viola Primary */
          ctx.lineWidth = 2/scale;
          ctx.stroke();
        }
        /* nomi: i temi sempre, persone e organizzazioni quando il nodo è
           abbastanza grande sullo schermo; dimensione costante a ogni zoom */
        const tema = n.type === 'tema';
        if (tema || acceso || r * scale > 9){
          ctx.font = `${tema ? 600 : 400} ${(tema ? 12 : 10.5) / scale}px Inter, sans-serif`;
          ctx.textAlign = 'center';
          ctx.textBaseline = 'top';
          ctx.lineWidth = 3/scale;
          ctx.strokeStyle = '#F5F3EC';            // alone del colore di fondo: si legge sopra gli archi
          ctx.strokeText(n.label, n.x, n.y + r + 3/scale);
          ctx.fillStyle = tema ? '#92400E' : '#4B5563';
          ctx.fillText(n.label, n.x, n.y + r + 3/scale);
        }
        ctx.globalAlpha = 1;
      });
    Graph.d3Force('charge').strength(n => -30 - 5 * raggio(n));
    Graph.d3Force('link').distance(l => 25 + raggio(l.source) + raggio(l.target));
    Graph.d3Force('gravita', gravita(0.08));
    Graph.d3Force('separazione', separazione());
  }
  Graph.graphData(grafoCorrente);
  followLayout();   // inquadra i nodi mentre si dispongono
}

/* Dopo il caricamento la camera insegue il layout finché i nodi si muovono, così
   all'apertura (e a ogni cambio di knowledge) il grafo è già inquadrato invece di
   restare un grumo di punti al centro. Si ferma quando il layout è fermo, dopo il
   tempo massimo, o appena l'utente sposta la vista. */
let fitUntil = 0, followId = 0;

function followLayout(){
  const now = performance.now();
  fitUntil = now + 1500;          // prolungato finché l'ingombro cambia
  const hardStop = now + 8000;    // come cooldownTime: dopo, il layout è fermo
  if (followId) return;
  let last = null;
  const step = () => {
    const t = performance.now();
    if (!Graph || thinking || t > fitUntil || t > hardStop){ followId = 0; return; }
    Graph.zoomToFit(0, 90);   // margine largo: i nomi sporgono dai nodi
    const b = Graph.getGraphBbox();
    if (b){
      if (last && (Math.abs(b.x[0]-last.x[0]) + Math.abs(b.x[1]-last.x[1]) +
                   Math.abs(b.y[0]-last.y[0]) + Math.abs(b.y[1]-last.y[1])) > 1)
        fitUntil = t + 1500;      // si muove ancora: continua a seguirlo
      last = b;
    }
    followId = requestAnimationFrame(step);
  };
  followId = requestAnimationFrame(step);
}

/* Per qualche secondo illumina i temi, le persone e le organizzazioni dei
   documenti trovati (il server li manda con ogni risultato, in "entita") e
   inquadra la camera su di loro. */
const DURATA_EVIDENZA = 6000;
function highlightSources(sources){
  if (!Graph || !sources?.length) return;
  const presenti = new Set(Graph.graphData().nodes.map(n => n.id));
  const ids = new Set(sources.flatMap(s => s.entita || []).filter(id => presenti.has(id)));
  if (!ids.size) return;
  const until = performance.now() + DURATA_EVIDENZA;
  ids.forEach(id => highlight.set(id, until));
  ridisegna();
  Graph.zoomToFit(800, 100, n => ids.has(n.id));
  setTimeout(ridisegna, DURATA_EVIDENZA + 50);   // toglie l'evidenza allo scadere
}

/* un solo fotogramma: onRenderFramePost rimette subito in pausa il ridisegno */
function ridisegna(){ Graph?.autoPauseRedraw(false); }

async function refreshGraph(){
  const kb = currentKb;
  const data = await api(withKb('/api/graph'));
  if (kb !== currentKb) return;
  initGraph(data);
  if ($('#search-panel').classList.contains('open')) await aggiornaDocumenti();
}

/* appena l'utente sposta o ingrandisce la vista, smettiamo di inquadrare da soli */
['wheel', 'mousedown'].forEach(ev =>
  $('#graph').addEventListener(ev, () => { fitUntil = 0; }, {passive: true}));

/* il canvas del grafo segue la dimensione del contenitore */
new ResizeObserver(() => {
  if (Graph) Graph.width($('#graph-pane').clientWidth).height($('#graph-pane').clientHeight);
}).observe($('#graph-pane'));

/* ---------------- anteprima documento ---------------- */
let currentDoc = null;
let cartellaKb = '';   // cartella su disco della knowledge attiva (dalla riga di stato)

async function openNode(node){
  /* persone, organizzazioni e temi non hanno un file da mostrare: cliccarli
     restringe la ricerca a quel gruppo, che è la cosa che serve davvero */
  const ENTITA = {persona:'persona', org:'org', tema:'tema', tag:'tag'};
  if (ENTITA[node.type]){
    const valore = node.titolo || node.label;     // per le persone, l'indirizzo intero
    impostaScelta(ENTITA[node.type], valore, node.label, node.id);
    $('#search').value = '';
    $('#preview').classList.remove('open');
    await mostraDocumenti();
    return;
  }
  const path = node.rel_path || node.path || node.id;
  node = {...node, id: path};
  currentDoc = path;
  $('#pv-title').textContent = node.titolo || node.label || path;
  $('#pv-percorso').textContent = cartellaKb ? `${cartellaKb}/${node.id}` : '';
  $('#pv-body').textContent = 'Carico…';
  $('#preview').classList.add('open');
  try {
    const testo = await api(withKb('/api/doc?path=' + encodeURIComponent(node.id)));
    if (currentDoc !== node.id) return;
    $('#pv-body').textContent = typeof testo === 'string' ? testo : (testo.text || testo.content || '');
  }
  catch(e){ $('#pv-body').textContent = 'Errore: ' + e.message; }
}
$('#pv-close').onclick = () => $('#preview').classList.remove('open');

$('#pv-open').onclick = () => {
  if (currentDoc) window.open(withKb('/api/file?path=' + encodeURIComponent(currentDoc)), '_blank', 'noopener');
};
$('#pv-chat').onclick = () => {
  if (currentDoc) location.href = linkChat(currentDoc);
};

/* fonti, citazioni e risultati di ricerca aprono l'anteprima del documento */
function focusDoc(id){ openNode({id, type: 'doc'}); }

async function logout(){
  try{ await authFetch('/accounts/logout/', {method:'POST'}); }catch(e){}
  location.href = '/accounts/login/';
}

/* intestazione del menu account e iniziale nell'avatar della barra */
function renderAccount(u){
  const nome = u?.username || '';
  $('#acc-nome').textContent = nome || '—';
  $('#acc-ruolo').textContent = nome ? (u.is_staff ? 'Amministratore' : 'Utente') : '';
  $('#app-btn').title = nome ? `${nome} · account e impostazioni` : 'Account e impostazioni';
  if (nome){
    for (const id of ['#acc-avatar', '#acc-avatar-big']){
      $(id).textContent = nome[0].toUpperCase();
      $(id).classList.add('iniziale');
    }
  }
}

/* ---------------- knowledge ---------------- */
function renderKbMenu(knowledges){
  const box = $('#kb-options');
  box.innerHTML = '';
  currentKbName = '';
  // se la knowledge salvata non esiste più, ricadi sulla prima
  if (!knowledges.some(k => k.slug === currentKb))
    currentKb = knowledges[0]?.slug || '';

  for (const k of knowledges){
    const b = document.createElement('button');
    b.className = k.slug === currentKb ? 'sel' : '';
    b.setAttribute('role', 'menuitemradio');
    b.setAttribute('aria-checked', k.slug === currentKb ? 'true' : 'false');
    b.innerHTML = `<svg class="ico tick${k.slug === currentKb ? '' : ' hide'}" width="16" height="16"
        viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 5 5L20 7"/></svg>
      <span class="lbl"></span><span class="note"></span>`;
    b.querySelector('.lbl').textContent = k.name;
    b.querySelector('.note').textContent = (k.riservata ? 'riservata · ' : '') + k.documents + ' doc';
    if (k.riservata) b.title = 'Knowledge riservata: la vedono solo lo staff e gli utenti scelti';
    b.onclick = () => { closeMenu(); switchKb(k.slug); };
    box.appendChild(b);
    if (k.slug === currentKb) currentKbName = k.name;
  }
  $('#kb-name').textContent = currentKbName || '—';
  $('#mi-kb-nome').textContent = currentKbName || '—';
  $('#mi-kb-del').disabled = knowledges.length <= 1;
  aggiornaLinkChat();
}

/* cambiare knowledge ricarica tutto il contesto: grafo, ricerca, anteprima */
async function switchKb(slug){
  if (slug === currentKb) return;
  currentKb = slug;
  localStorage.setItem('knowledge-grafo-kb', slug);
  resetWorkspace();
  await refreshStatus();
  await refreshGraph();
}

/* riporta l'area di lavoro allo stato iniziale della knowledge appena scelta */
function resetWorkspace(){
  ricercaId++; thinking = false;
  impostaScelta(null, null, '');   // un nodo o un documento dell'altra knowledge qui non esiste
  highlight.clear();
  currentDoc = null;
  $('#preview').classList.remove('open');
  $('#search').value = '';
  $('#search-results').innerHTML = '';
  $('#search-panel').classList.add('open');
}

async function newKnowledge(){
  const name = await Dialogo.chiedi({titolo: 'Nuova knowledge', icona: 'nuovo', etichetta: 'Nome',
                                     segnaposto: 'es. Commesse 2026', ok: 'Crea'});
  if (!name) return;
  try{
    const k = await api('/api/kbs', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name: name.trim()})
    });
    currentKb = k.slug;
    localStorage.setItem('knowledge-grafo-kb', k.slug);
    resetWorkspace();
    await refreshStatus();
    await refreshGraph();
    avvisa(`Knowledge "${k.name}" creata. Per aggiungere file o cartelle usa il menu delle impostazioni (l'avatar in alto a destra).`);
  }catch(e){ avvisa('Errore: ' + e.message); }
}

async function deleteKnowledge(){
  if (!await Dialogo.conferma({
    titolo: 'Eliminare la knowledge?', oggetto: currentKbName, tono: 'pericolo', ok: 'Elimina',
    via: ['I documenti e il loro indice vengono rimossi',
          'La cartella su disco viene cancellata: non passa dal Cestino']})) return;
  try{
    await api('/api/kbs/' + encodeURIComponent(currentKb), {method:'DELETE'});
    currentKb = '';
    localStorage.removeItem('knowledge-grafo-kb');
    resetWorkspace();
    await refreshStatus();
    await refreshGraph();
  }catch(e){ avvisa('Errore: ' + e.message); }
}

/* Percorsi da importare: il server apre il Finder sullo schermo dell'utente.
   Ritorna null se la selezione è stata annullata. */
async function pickPaths(kind){
  const r = await authFetch('/api/pick', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({kind, kb: currentKb})
  });
  if (r.status === 501){   // server non locale o non macOS: si torna a scrivere il percorso
    const p = await Dialogo.chiedi({titolo: 'Aggiungi file o cartelle', icona: 'cartella',
      etichetta: 'Percorso del file o della cartella', mono: true,
      segnaposto: '/Users/tu/Documenti/archivio', nota: 'Vale anche ~ per la cartella utente, es. ~/file.pdf',
      ok: 'Aggiungi'});
    return p ? [p] : null;
  }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || r.statusText);
  return d.canceled ? null : d.paths;
}

function importSummary(imp){
  const n = (imp?.copiati ?? 0) + (imp?.sostituiti ?? 0);
  const extra = [];
  if (imp?.sostituiti) extra.push(`${imp.sostituiti} ${imp.sostituiti === 1
    ? 'sostituito: la versione precedente è' : 'sostituiti: le versioni precedenti sono'} nel Cestino`);
  if (imp?.saltati) extra.push(`${imp.saltati} già presenti`);
  if (imp?.ignorati) extra.push(`${imp.ignorati} non importati (formato non supportato o non leggibili)`);
  if (imp?.esclusi) extra.push(`${imp.esclusi} ${imp.esclusi === 1
    ? 'escluso' : 'esclusi'} dal filtro della knowledge`);
  if (imp?.cartelle_escluse) extra.push(`${imp.cartelle_escluse} ${imp.cartelle_escluse === 1
    ? 'cartella saltata' : 'cartelle saltate'} dal filtro`);
  if (imp?.decorativi) extra.push(`${imp.decorativi} ${imp.decorativi === 1
    ? 'immagine scartata: logo o firma' : 'immagini scartate: loghi o firme'}`);
  return `Importati ${n} file` + (extra.length ? ` (${extra.join(', ')})` : '') + '.';
}

let busy = false;   // un'importazione o una reindicizzazione per volta

async function addFromDisk(kind){
  if (busy) return;
  busy = true;
  progressShow(true);
  $('#rp-text').textContent = 'Scegli nel Finder…';
  let paths = null;
  try{
    paths = await pickPaths(kind);
  }catch(e){ avvisa('Errore: ' + e.message); }
  if (!paths || !paths.length){ progressShow(false); busy = false; return; }

  $('#rp-text').textContent = 'Cerco i file da copiare…';
  try{
    const r = await authFetch('/api/kbs/' + encodeURIComponent(currentKb) + '/import', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({paths})
    });
    if (!r.ok) throw new Error((await r.json().catch(()=>({}))).detail || r.statusText);
    const ct = r.headers.get('content-type') || '';
    if (ct.includes('application/json')){
      // Ollama spento: i file sono stati copiati ma non indicizzati
      const d = await r.json();
      avvisa(importSummary(d.import) + (d.detail ? ' ' + d.detail : ''));
    } else {
      const {stats, imp, avviso, fermato} = await driveIndexStream(r);
      avvisa(importSummary(imp) + (stats ? ' ' + reindexSummary(stats) : '')
             + (fermato ? ' ' + fermatoSummary(fermato) : '') + (avviso ? ' ' + avviso : ''));
    }
    await refreshStatus();
    await refreshGraph();
  }catch(e){ avvisa('Errore: ' + e.message); }
  progressShow(false);
  busy = false;
}

/* ---------------- filtro dei file ---------------- */
/* Quali tipi di file la knowledge importa e indicizza, e in quali cartelle
   non entra (node_modules e simili). Togliere un tipo o una cartella non
   cancella niente: i file già importati escono dall'indice e restano nella
   cartella, pronti a rientrare se il filtro torna com'era. */
async function apriFiltro(){
  let f;
  try{ f = await api('/api/kbs/' + encodeURIComponent(currentKb) + '/filtro'); }
  catch(e){ avvisa('Errore: ' + e.message); return; }
  const globali = [
    f.globali.length ? 'estensioni ' + f.globali.join(' ') : '',
    f.cartelle_globali.length ? 'cartelle ' + f.cartelle_globali.join(', ') : '',
  ].filter(Boolean).join('; ');
  const scelta = await Dialogo.modulo({
    titolo: `Filtro dei file di «${currentKbName}»`, icona: 'cartella',
    testo: 'Spunta i tipi di file da importare e indicizzare. Quelli tolti, e quelli dentro ' +
           'le cartelle da saltare, non si copiano più e, se già presenti, escono ' +
           'dall\'indice; i file restano nella cartella.',
    scelte: f.tipi.map(t => ({valore: t.chiave, nome: t.nome,
                              dettaglio: t.estensioni.join(' '), scelto: !t.escluso})),
    campi: [
      {etichetta: 'Altre estensioni da escludere', elenco: 'estensioni',
       valore: Elenco.dividi('estensioni', f.ext), segnaposto: 'es. .csv', vuoto: 'Nessuna'},
      {etichetta: 'Cartelle da saltare, per nome', elenco: 'cartelle',
       valore: Elenco.dividi('cartelle', f.cartelle), segnaposto: 'es. dist', vuoto: 'Nessuna'},
    ],
    nota: globali
      ? `Valgono anche le esclusioni di Impostazioni, per tutte le knowledge: ${globali}`
      : null,
    ok: 'Salva'});
  if (!scelta) return;
  const ammessi = new Set(scelta.scelte);
  let r;
  try{
    r = await api('/api/kbs/' + encodeURIComponent(currentKb) + '/filtro', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({tipi: f.tipi.map(t => t.chiave).filter(c => !ammessi.has(c)),
                            ext: scelta.campi[0], cartelle: scelta.campi[1]})
    });
  }catch(e){ avvisa('Errore: ' + e.message); return; }
  if (!r.cambiato) return;
  if (await Dialogo.conferma({titolo: 'Aggiornare l\'indice adesso?', tono: 'info', icona: 'reimposta',
        testo: 'Il filtro vale da subito per le nuove importazioni. Per togliere dall\'indice i ' +
               'file esclusi, o aggiungere quelli tornati ammessi, serve una reindicizzazione.',
        ok: 'Reindicizza', annulla: 'Più tardi'}))
    reindex();
}

/* ---------------- eliminazione ---------------- */
/* Seleziona file e cartelle da togliere dalla knowledge. */
const Elim = {modo:'', dir:'', voci:[], scelti:new Map()};

const ICO_DIR = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const ICO_FILE = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>';

const fmtByte = n => n < 1024 ? n + ' B'
  : n < 1048576 ? Math.round(n / 1024) + ' KB'
  : (n / 1048576).toFixed(1).replace('.', ',') + ' MB';

let focusPrimaDelDialogo = null;
function apriDialogo(titolo, sotto, ok){
  $('#md-title').textContent = titolo;
  $('#md-sub').textContent = sotto;
  $('#md-ok').textContent = ok;
  $('#md-ok').disabled = true;
  msgDialogo('');
  focusPrimaDelDialogo = document.activeElement;
  $('#overlay').hidden = false;
  // il focus entra nel dialogo: da tastiera si resterebbe sulla pagina dietro
  $('#md-annulla').focus();
}
function chiudiDialogo(){
  $('#overlay').hidden = true; Elim.modo = '';
  focusPrimaDelDialogo?.focus?.();
  focusPrimaDelDialogo = null;
}
function msgDialogo(testo, errore){
  $('#md-msg').textContent = testo;
  $('#md-msg').classList.toggle('err', !!errore);
}
$('#md-annulla').onclick = chiudiDialogo;
$('#overlay').onclick = e => { if (e.target.id === 'overlay') chiudiDialogo(); };
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !$('#overlay').hidden) chiudiDialogo();
});
$('#md-ok').onclick = eliminaFile;
$('#fb-filtro').oninput = () => disegnaVoci();

/* --- file e cartelle --- */
async function apriEliminaFile(){
  Elim.modo = 'file';
  Elim.scelti = new Map();
  apriDialogo(`Elimina file o cartelle da «${currentKbName}»`,
    'Spariscono dalla ricerca e dalla mappa e finiscono nel Cestino del Mac, da cui si possono recuperare.',
    'Elimina');
  $('#fb-bar').hidden = false;
  aggiornaScelti();
  await vaiCartella('');
}

async function vaiCartella(dir){
  Elim.dir = dir;
  Elim.voci = [];
  $('#fb-filtro').value = '';
  disegnaBriciole();
  $('#md-body').innerHTML = '<div class="fb-vuoto">Carico…</div>';
  try{
    const d = await api(`/api/kbs/${encodeURIComponent(currentKb)}/files?dir=` + encodeURIComponent(dir));
    if (Elim.modo !== 'file' || Elim.dir !== dir) return;   // nel frattempo si è andati altrove
    Elim.voci = d.voci;
    disegnaVoci();
  }catch(e){ $('#md-body').innerHTML = ''; msgDialogo('Errore: ' + e.message, true); }
}

function disegnaBriciole(){
  const box = $('#fb-crumbs');
  box.innerHTML = '';
  const parti = Elim.dir ? Elim.dir.split('/') : [];
  const tappe = [[currentKbName, '']].concat(parti.map((p, i) => [p, parti.slice(0, i + 1).join('/')]));
  tappe.forEach(([nome, rel], i) => {
    if (i) box.append('/');
    const s = document.createElement('span');
    s.textContent = nome;
    if (i === tappe.length - 1) s.className = 'qui';
    else { s.onclick = () => vaiCartella(rel); cliccabile(s, 'Torna a ' + nome); }
    box.append(s);
  });
}

/* ciò che sta dentro una cartella già scelta se ne va con lei */
const coperta = rel => [...Elim.scelti.keys()].some(s => rel.startsWith(s + '/'));

function scegli(v){
  for (const k of [...Elim.scelti.keys()]) if (k.startsWith(v.rel + '/')) Elim.scelti.delete(k);
  Elim.scelti.set(v.rel, v);
}

function fileScelti(){
  return [...Elim.scelti.values()].reduce((n, v) => n + (v.cartella ? v.file : 1), 0);
}

function aggiornaScelti(){
  const n = Elim.scelti.size;
  $('#md-ok').disabled = !n;
  msgDialogo(n ? `${n} ${n === 1 ? 'elemento scelto' : 'elementi scelti'} · ${fileScelti()} file`
               : 'Spunta cosa eliminare; clic su una cartella per aprirla.');
}

function rigaFile(v){
  const dentro = coperta(v.rel), presa = dentro || Elim.scelti.has(v.rel);
  const row = document.createElement('div');
  row.className = 'fb-row' + (v.cartella ? ' dir' : '') + (presa ? ' scelta' : '');
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = presa;
  cb.disabled = dentro;
  if (dentro) row.title = 'Viene eliminato insieme alla cartella che lo contiene';
  const ico = document.createElement('span');
  ico.className = 'ico';
  ico.innerHTML = v.cartella ? ICO_DIR : ICO_FILE;
  const nm = document.createElement('span');
  nm.className = 'nm';
  nm.textContent = v.nome;
  // la casella scelta è già un controllo; per entrare in una cartella serve il nome
  if (v.cartella) cliccabile(nm, 'Apri la cartella ' + v.nome);
  cb.setAttribute('aria-label', 'Scegli ' + v.nome);
  row.append(cb, ico, nm);
  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.textContent = v.cartella ? `${v.file} file`
    : fmtByte(v.byte) + (v.indicizzato ? '' : ' · non indicizzato');
  row.append(meta);
  const cambia = () => {
    if (cb.disabled) return;
    if (Elim.scelti.has(v.rel)) Elim.scelti.delete(v.rel); else scegli(v);
    aggiornaScelti();
    disegnaVoci();
  };
  cb.onclick = e => { e.stopPropagation(); cambia(); };
  row.onclick = () => v.cartella ? vaiCartella(v.rel) : cambia();
  return row;
}

function disegnaVoci(){
  const box = $('#md-body'), alto = box.scrollTop;
  const filtro = $('#fb-filtro').value.trim().toLowerCase();
  const voci = Elim.voci.filter(v => !filtro || v.nome.toLowerCase().includes(filtro));
  box.innerHTML = '';
  if (!voci.length){
    const vuoto = document.createElement('div');
    vuoto.className = 'fb-vuoto';
    vuoto.textContent = filtro ? `Nessun nome contiene «${filtro}».` : 'Cartella vuota.';
    box.append(vuoto);
    return;
  }
  const frag = document.createDocumentFragment();
  /* «tutti» vale per ciò che si vede: filtrando per nome si sceglie un gruppo */
  const libere = voci.filter(v => !coperta(v.rel));
  if (libere.length > 1){
    const tutte = libere.every(v => Elim.scelti.has(v.rel));
    const row = document.createElement('label');
    row.className = 'fb-row tutti';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = tutte;
    cb.onchange = () => {
      libere.forEach(v => tutte ? Elim.scelti.delete(v.rel) : scegli(v));
      aggiornaScelti();
      disegnaVoci();
    };
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = filtro ? `Tutti i ${libere.length} risultati` : `Tutti i ${libere.length} elementi di questa cartella`;
    row.append(cb, nm);
    frag.append(row);
  }
  voci.forEach(v => frag.append(rigaFile(v)));
  box.append(frag);
  box.scrollTop = alto;
}

async function eliminaFile(){
  const voci = [...Elim.scelti.values()];
  if (!voci.length) return;
  const n = fileScelti();
  if (!await Dialogo.conferma({
    titolo: `Eliminare ${n === 1 ? '1 file' : n + ' file'}?`, testo: `Dalla knowledge «${currentKbName}»:`,
    elenco: voci.map(v => v.rel + (v.cartella ? '/' : '')), tono: 'pericolo', ok: 'Elimina',
    via: ['Spariscono dalla ricerca e dalla mappa'], resta: ['Finiscono nel Cestino del Mac, da dove si possono recuperare']})) return;
  $('#md-ok').disabled = true;
  msgDialogo('Elimino…');
  try{
    const d = await api(`/api/kbs/${encodeURIComponent(currentKb)}/elimina`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({paths: voci.map(v => v.rel)})
    });
    chiudiDialogo();
    await dopoEliminazione(voci.map(v => v.rel), eliminaSummary(d));
  }catch(e){ msgDialogo('Errore: ' + e.message, true); $('#md-ok').disabled = false; }
}

function eliminaSummary(d){
  const file = d.file === 1 ? 'Eliminato 1 file' : `Eliminati ${d.file} file`;
  const doc = d.documenti === 1 ? '1 documento tolto' : `${d.documenti} documenti tolti`;
  return (d.file ? `${file} (${doc} dall'indice): sono nel Cestino.` : `${doc} dall'indice.`) +
    (d.errori?.length ? `\nNon spostati nel Cestino (restano nella knowledge): ${d.errori.join('; ')}` : '');
}

async function dopoEliminazione(radici, testo){
  // anteprima e filtro su un documento appena tolto non hanno più niente da mostrare
  const tolto = rel => !!rel && radici.some(r => rel === r || rel.startsWith(r + '/'));
  if (tolto(currentDoc)){ currentDoc = null; $('#preview').classList.remove('open'); }
  if (scelta?.chiave === 'docs' && tolto(scelta.valore)) impostaScelta(null, null, '');
  avvisa(testo);
  await refreshStatus();
  await refreshGraph();
}

async function refreshStatus(){
  const kb = currentKb;
  try{
    const s = await api(withKb('/api/status'));
    if (kb !== currentKb) return;
    $('#ollama-dot').classList.toggle('ok', s.ollama.ok);
    if (s.kb) { currentKb = s.kb; localStorage.setItem('knowledge-grafo-kb', s.kb); }
    if (s.knowledges) renderKbMenu(s.knowledges);
    let line = s.documenti + (s.documenti === 1 ? ' documento' : ' documenti');
    if (!s.ollama.ok) line += ' · ' + (s.ollama.servizio || 'Ollama') + ' spento';
    else if (s.modelli_mancanti?.length) line += ' · manca: ' + s.modelli_mancanti.join(', ');
    /* materiale dentro documents/ che non appartiene a nessuna knowledge: non
       viene indicizzato e senza avviso resterebbe invisibile per sempre */
    /* indicizzazione avviata altrove (altra scheda, o prima di un reload) */
    if (s.lavoro && s.lavoro.stato === 'in corso') seguiLavoro(s.lavoro);
    const fuori = s.fuori_knowledge || [];
    const avvisi = [];
    if (s.sospesa){
      line += ' · indicizzazione automatica sospesa';
      avvisi.push('Hai fermato l\'indicizzazione di questa knowledge: premi Reindicizza per riprendere.');
    }
    if (fuori.length){
      const file = fuori.reduce((n, f) => n + f.file, 0);
      line += ` · ⚠ ${file} file fuori dalle knowledge`;
      avvisi.push('Non indicizzati, perché non stanno in nessuna knowledge:\n'
        + fuori.map(f => `  ${f.nome} (${f.file})`).join('\n')
        + '\n\nSpostali in documents/<knowledge>/ oppure crea una knowledge con quel nome.');
    }
    /* file di formato che nessuna pipeline sa leggere: restano fuori
       dall'indice, e va detto */
    const ns = s.non_supportati || {};
    if (ns.file){
      line += ` · ${ns.file} file di formato non supportato`;
      avvisi.push('Formati non supportati, quindi non indicizzati:\n'
        + Object.entries(ns.estensioni || {}).map(([e, n]) => `  ${e} (${n})`).join('\n'));
    }
    $('#status-line').title = avvisi.join('\n\n');
    $('#status-line').textContent = line;
    renderAccount(s.utente);
    cartellaKb = s.kb ? s.cartella_documenti : '';
    // Impostazioni e utenti sono disponibili allo staff.
    document.querySelectorAll('.mi-staff').forEach(b => b.style.display = s.utente?.is_staff ? '' : 'none');
  }catch(e){ $('#status-line').textContent = 'server non raggiungibile'; }
}

/* ---------------- avanzamento indicizzazione ---------------- */
function progressShow(on){
  const p = $('#progress');
  if (on){
    p.hidden = false;
    $('#rp-fill').style.width = '32%';
    $('#rp-fill').classList.add('indeterminate');
    $('#rp-text').textContent = 'Preparo…';
    $('#rp-count').textContent = '';
    $('#rp-file').textContent = '';
  } else {
    p.hidden = true;
    $('#rp-fill').classList.remove('indeterminate');
  }
  lavoroInCorso(null);
}

/* ---------------- stop dell'indicizzazione ---------------- */
/* Il pulsante «Ferma» compare quando si conosce il lavoro sul server. Il
   lavoro si ferma dopo il documento che sta leggendo: quel che è già
   indicizzato resta, e l'indicizzazione automatica della knowledge (file
   cambiati) si sospende finché non si preme Reindicizza. */
let lavoroId = null;
function lavoroInCorso(id, fermando){
  lavoroId = id;
  const b = $('#rp-ferma');
  b.hidden = !id;
  b.disabled = !!fermando;
  if (fermando) $('#rp-text').textContent = 'Mi fermo dopo il documento in corso…';
}
$('#rp-ferma').onclick = async () => {
  const id = lavoroId;
  if (!id) return;
  const ok = await Dialogo.conferma({
    titolo: 'Fermare l\'indicizzazione?', tono: 'attenzione', icona: 'attenzione',
    testo: 'Si ferma dopo il documento che sta leggendo.',
    resta: ['I documenti già indicizzati restano cercabili',
            'I file restano nella cartella: Reindicizza riprende da dove si è fermata'],
    via: ['L\'indicizzazione automatica resta sospesa finché non premi Reindicizza'],
    ok: 'Ferma', annulla: 'Continua'});
  if (!ok || lavoroId !== id) return;
  try{
    await api('/api/lavori/' + encodeURIComponent(id) + '/ferma', {method: 'POST'});
    lavoroInCorso(id, true);
  }catch(e){ avvisa('Errore: ' + e.message); }
};

function fermatoSummary(ev){
  return 'Indicizzazione fermata' +
    (ev.total ? ` dopo ${ev.done} documenti su ${ev.total}.` : ' prima di partire.') +
    ' Quel che era già indicizzato resta; l\'indicizzazione automatica di questa knowledge ' +
    'è sospesa finché non premi Reindicizza.';
}

function progressEvent(ev){
  if (ev.type === 'cerco'){
    // importazione: si scorrono ancora le cartelle, il totale non si sa
    $('#rp-text').textContent = 'Cerco i file da copiare…';
    $('#rp-count').textContent = ev.trovati.toLocaleString('it-IT') + ' trovati';
  } else if (ev.type === 'start'){
    $('#rp-fill').classList.remove('indeterminate');
    $('#rp-text').textContent = ev.label || (ev.total ? 'Indicizzo i documenti…' : 'Nessun file da indicizzare');
    $('#rp-count').textContent = ev.total ? '0 / ' + ev.total : '';
    $('#rp-fill').style.width = ev.total ? '0%' : '100%';
  } else if (ev.type === 'progress'){
    const pct = ev.total ? Math.round(ev.done / ev.total * 100) : 100;
    $('#rp-fill').style.width = pct + '%';
    $('#rp-count').textContent = ev.done + ' / ' + ev.total;
    $('#rp-file').textContent = (ev.action === 'errore' || ev.action === 'ignorati' ? '⚠ ' : '') + (ev.file || '');
  }
}

/* legge lo stream NDJSON dell'indicizzazione aggiornando la barra.
   Ritorna {stats, imp, avviso, fermato}: imp e avviso solo per l'import, che prima
   dell'indicizzazione manda l'avanzamento della copia; fermato è l'evento
   «stopped» se l'utente ha fermato il lavoro. */
async function driveIndexStream(resp){
  const reader = resp.body.getReader(), dec = new TextDecoder();
  let buf = '', stats = null, imp = null, avviso = '', fermato = null;
  for (;;){
    const {done, value} = await reader.read();
    if (done) break;
    buf += dec.decode(value, {stream:true});
    const lines = buf.split('\n'); buf = lines.pop();
    for (const line of lines){
      if (!line.trim()) continue;
      const ev = JSON.parse(line);
      if (ev.error) throw new Error(ev.error);
      if (ev.type === 'import') imp = ev.import;
      else if (ev.type === 'avviso') avviso = ev.detail;
      else if (ev.type === 'done') stats = ev.stats;
      else if (ev.type === 'job') lavoroInCorso(ev.id);
      else if (ev.type === 'stopping') lavoroInCorso(lavoroId, true);
      else if (ev.type === 'stopped') fermato = ev;
      else progressEvent(ev);
    }
  }
  return {stats, imp, avviso, fermato};
}

function reindexSummary(stats){
  if (stats.gruppi) return temiSummary(stats);   // era un lavoro «Rifai i temi»
  return `Indice aggiornato: ${stats.aggiunti} nuovi, ${stats.aggiornati} aggiornati, ` +
    `${stats.rimossi} rimossi, ${stats.invariati} invariati.` +
    // dopo tanti documenti cambiati i temi si rifanno da soli
    (stats.temi?.raggruppati ? `\nTemi rifatti: ${stats.temi.temi.join(', ')}.` : '') +
    (stats.errori.length ? `\nErrori: ${stats.errori.join('; ')}` : '');
}

function temiSummary(stats){
  if (!stats.temi.length) return 'Troppo pochi documenti per fare temi.';
  return `${stats.temi.length} temi: ${stats.temi.join(', ')}.` +
    (stats.senza_tema ? ` ${stats.senza_tema} documenti restano senza tema: troppo lontani da tutti i gruppi.` : '');
}

/* ---------------- reindex ---------------- */
/* L'indicizzazione gira sul server e non dipende più da questa finestra:
   ricaricando la pagina (o aprendone un'altra) ci si riattacca a quella in
   corso invece di vedere una barra che non c'è più. */
let seguoLavoro = false;
async function seguiLavoro(l){
  if (seguoLavoro || busy) return;
  seguoLavoro = true;
  progressShow(true);
  $('#rp-fill').classList.remove('indeterminate');
  $('#rp-text').textContent = l.descrizione || 'Indicizzo i documenti…';
  try{
    for(;;){
      progressEvent({type:'progress', done:l.done, total:l.total, file:l.file});
      // un lavoro su una knowledge che non si può aprire arriva senza kb: niente stop
      lavoroInCorso(l.kb ? l.id : null, l.fermando);
      await new Promise(r => setTimeout(r, 2000));
      l = await api('/api/reindex/stato');
      if (!l || l.stato !== 'in corso') break;
    }
    if (l && l.stato === 'fermato') avvisa(fermatoSummary(l));
    else if (l && l.stats) avvisa(reindexSummary(l.stats));
    else if (l && l.errore) avvisa('Errore: ' + l.errore);
    await refreshGraph(); await refreshStatus();
  }catch(e){ /* il lavoro continua sul server: qui basta smettere di guardarlo */ }
  progressShow(false);
  seguoLavoro = false;
}

function reindex(){ return lanciaLavoro('/api/reindex'); }

/* i gruppi nascono dai vettori già salvati: niente da reindicizzare, solo un
   nome da chiedere al modello per ogni tema (mezzo minuto circa) */
function rifaiTemi(){ return lanciaLavoro('/api/temi'); }

/* avvia un lavoro sul server e ne segue l'avanzamento nella barra */
async function lanciaLavoro(url){
  if (busy) return;
  busy = true;
  progressShow(true);
  try{
    const r = await authFetch(withKb(url), {method:'POST'});
    if (!r.ok) throw new Error((await r.json().catch(()=>({}))).detail || r.statusText);
    const {stats, fermato} = await driveIndexStream(r);
    if (stats || fermato){
      avvisa(stats ? reindexSummary(stats) : fermatoSummary(fermato));
      await refreshGraph(); await refreshStatus();
    }
  }catch(e){ avvisa('Errore: ' + e.message); }
  progressShow(false);
  busy = false;
}
$('#btn-refresh').onclick = () => refreshGraph().catch(e => avvisa('Errore: ' + e.message));

/* ---------------- documenti e ricerca interna ---------------- */
let ricercaId = 0;
function statoElenco(testo){
  const box = $('#search-results');
  box.innerHTML = '';
  const p = document.createElement('div');
  p.className = 'hit stato'; p.textContent = testo;
  box.appendChild(p);
}

function rigaDocumento({path, title, meta, text}){
  const div = document.createElement('div');
  div.className = 'hit';
  div.innerHTML = '<div class="doc"><span class="nm"></span><span class="sc"></span></div><div class="percorso"></div><div class="txt"></div>';
  div.querySelector('.nm').textContent = title || path;
  div.querySelector('.sc').textContent = meta || '';
  div.querySelector('.percorso').textContent = path;
  div.querySelector('.txt').textContent = text || '';
  div.onclick = () => focusDoc(path);
  cliccabile(div, 'Apri ' + (title || path));
  return div;
}

async function mostraDocumenti(){
  const id = ++ricercaId, kb = currentKb;
  // Le pagine successive usano esattamente la knowledge e i filtri iniziali.
  // Un nuovo elenco o una ricerca invalidano anche le risposte ancora in volo.
  const url = withKb('/api/documents?') + filtriQuery();
  const etichetta = scelta?.etichetta || '';
  const filtrato = !!(scelta || $('#f-tipo').value || $('#f-periodo').value);
  const attuale = () => id === ricercaId && kb === currentKb;
  thinking = false;
  const box = $('#search-results');
  $('#search-panel').classList.add('open');
  $('#sp-title').textContent = etichetta || 'Documenti';
  statoElenco('Carico i documenti…');

  const paginazione = document.createElement('div');
  paginazione.className = 'documenti-paginazione';
  const conteggio = document.createElement('span');
  const altri = document.createElement('button');
  altri.type = 'button'; altri.textContent = 'Carica altri';
  paginazione.append(conteggio, altri);
  let mostrati = 0, prossimo = 0, caricando = false;

  async function caricaPagina(){
    if (!attuale() || caricando || prossimo === null) return;
    caricando = true;
    altri.disabled = true; altri.textContent = 'Carico…';
    try{
      const data = await api(url + '&offset=' + prossimo);
      if (!attuale()) return;
      const docs = data.documents || [];
      const n = data.total ?? (mostrati + docs.length);
      $('#sp-title').textContent = `${n} document${n === 1 ? 'o' : 'i'}` + (etichetta ? ' · ' + etichetta : '');
      if (!mostrati && !docs.length){
        statoElenco(filtrato
          ? 'Nessun documento corrisponde ai filtri selezionati.'
          : 'Non ci sono ancora documenti indicizzati. Aggiungi file o cartelle dal menu in alto a destra.');
        prossimo = null;
        return;
      }
      if (!mostrati) box.innerHTML = '';
      paginazione.remove();
      const frag = document.createDocumentFragment();
      for (const d of docs) frag.appendChild(rigaDocumento({
        path:d.rel_path, title:d.title, meta:d.kind === 'immagine' ? 'Immagine' : '',
        text:d.preview || (d.topic ? 'Tema: ' + d.topic : '')}));
      box.appendChild(frag);
      mostrati += docs.length;
      prossimo = data.next_offset ?? null;
      conteggio.textContent = `${mostrati} di ${n} documenti mostrati`;
      altri.hidden = prossimo === null;
      box.appendChild(paginazione);
      annuncia(conteggio.textContent);
    }catch(e){
      if (!attuale()) return;
      if (mostrati) avvisa('Errore nel caricamento dei documenti: ' + e.message);
      else statoElenco('Errore: ' + e.message);
    }finally{
      caricando = false;
      altri.disabled = false; altri.textContent = 'Carica altri';
    }
  }
  altri.onclick = caricaPagina;
  await caricaPagina();
}

async function cercaDocumenti(){
  const q = $('#search').value.trim();
  if (!q) return mostraDocumenti();
  const id = ++ricercaId, kb = currentKb;
  const box = $('#search-results');
  $('#search-panel').classList.add('open');
  $('#sp-title').textContent = 'Risultati della ricerca';
  statoElenco('Cerco nei documenti…');
  thinking = true;
  try{
    const {results = []} = await api(withKb('/api/search?q=' + encodeURIComponent(q)) + filtriQuery());
    if (id !== ricercaId || kb !== currentKb) return;
    $('#sp-title').textContent = results.length
      ? results.length + (results.length === 1 ? ' risultato' : ' risultati') : 'Nessun risultato';
    box.innerHTML = '';
    if (!results.length) statoElenco('Nessun passaggio corrisponde alla ricerca.');
    for (const h of results) box.appendChild(rigaDocumento({
      path:h.doc, title:h.title || h.doc, meta:h.origine || '', text:h.text}));
    highlightSources(results);
    annuncia($('#sp-title').textContent);
  }catch(e){
    if (id === ricercaId && kb === currentKb) statoElenco('Errore: ' + e.message);
  }finally{
    if (id === ricercaId) thinking = false;
  }
}

function aggiornaDocumenti(){
  return $('#search').value.trim() ? cercaDocumenti() : mostraDocumenti();
}
$('#sp-close').onclick = () => $('#search-panel').classList.remove('open');
$('#btn-documents').onclick = () => {
  $('#search').value = '';
  $('#f-tipo').value = $('#f-periodo').value = '';
  impostaScelta(null, null, ''); segnalaFiltri();
  mostraDocumenti();
};
$('#search').addEventListener('keydown', e => {
  if (e.key === 'Escape'){ $('#search-panel').classList.remove('open'); return; }
  if (e.key === 'Enter') cercaDocumenti();
});

/* testo sicuro dentro l'HTML */
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
/* ---------------- avvio ---------------- */
(async () => {
  try { await refreshStatus(); await refreshGraph(); await mostraDocumenti(); }
  catch(e){ avvisa('Errore: ' + e.message); }
})();
setInterval(refreshStatus, 15000);
