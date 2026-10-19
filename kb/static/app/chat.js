(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const params = new URLSearchParams(location.search);
  const storedKb = () => { try { return localStorage.getItem('knowledge-grafo-kb') || ''; } catch (_) { return ''; } };
  let currentKb = params.get('kb') || storedKb();
  let knowledges = [];
  let conversation = null;
  let messages = [];
  let filters = {};
  let busy = false;
  let loading = true;
  let historyRequest = 0;
  let previewRequest = 0;
  let initialized = false;

  for (const key of ['entita', 'docs', 'tema', 'tag', 'persona', 'org']) {
    if (params.get(key)) filters[key] = params.get(key);
  }
  if (params.getAll('docs').length > 1) filters.docs = params.getAll('docs');
  if (params.get('doc')) filters.docs = [params.get('doc')];

  const node = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text != null) el.textContent = text;
    return el;
  };
  const csrf = () => document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/)?.[1] || '';
  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {...(options.headers || {}), 'X-CSRFToken': csrf()},
    });
    if (response.status === 401) {
      location.href = '/accounts/login/?next=' + encodeURIComponent(location.pathname + location.search);
      throw new Error('La sessione è scaduta. Accedi di nuovo.');
    }
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw new Error(typeof result.detail === 'string' ? result.detail : 'Non riesco a completare la richiesta. Riprova.');
    }
    return response.headers.get('content-type')?.includes('json') ? response.json() : response.text();
  }
  const kbUrl = path => path + (path.includes('?') ? '&' : '?') + 'kb=' + encodeURIComponent(currentKb);

  function error(text = '') {
    $('#chat-error').textContent = text;
    $('#chat-error').hidden = !text;
  }

  function updateControls() {
    const disabled = busy || loading || !currentKb;
    $('#chat-kb').disabled = busy || loading || !knowledges.length;
    $('#new-chat').disabled = disabled;
    $('#delete-chat').disabled = disabled;
    $('#clear-scope').disabled = disabled;
    $('#chat-question').disabled = disabled;
    $('#send-question').disabled = disabled || !$('#chat-question').value.trim();
    $('#chat-pending').hidden = !busy;
    $('#chat-form').setAttribute('aria-busy', String(busy));
    $('#conversation-pane').setAttribute('aria-busy', String(busy || loading));
    document.querySelectorAll('.history-item, [data-question]').forEach(button => { button.disabled = disabled; });
  }

  function updateLocation() {
    const query = new URLSearchParams();
    if (currentKb) query.set('kb', currentKb);
    if (conversation) query.set('conversation', conversation.id);
    else {
      for (const [key, value] of Object.entries(filters)) {
        if (!value || (Array.isArray(value) && !value.length)) continue;
        if (key === 'docs' && Array.isArray(value)) {
          if (value.length === 1) query.set('doc', value[0]);
          else for (const path of value) query.append('docs', path);
        } else query.set(key, Array.isArray(value) ? value.join(',') : value);
      }
    }
    history.replaceState(null, '', '/chat' + (query.size ? '?' + query : ''));
    const link = currentKb ? '/?kb=' + encodeURIComponent(currentKb) : '/';
    $('#knowledge-link').href = link;
    $('#sidebar-knowledge-link').href = link;
    try { if (currentKb) localStorage.setItem('knowledge-grafo-kb', currentKb); } catch (_) {}
  }

  function describeScope() {
    if (filters.entita) return String(filters.entita).replace(/^#(?:tema|tag|persona|org):/, '');
    const labels = {docs:'Documento', tema:'Tema', tag:'Tag', persona:'Persona', org:'Organizzazione'};
    return Object.entries(filters).filter(([key, value]) => labels[key] && value && (!Array.isArray(value) || value.length))
      .map(([key, value]) => labels[key] + ': ' + (Array.isArray(value) ? value.join(', ') : value)).join(' · ');
  }

  function updateContext() {
    const title = conversation?.title || 'Nuova conversazione';
    $('#conversation-title').textContent = title;
    $('#conversation-title').title = title;
    document.title = conversation ? title + ' · NIKI' : 'NIKI · Chat';
    $('#delete-chat').hidden = !conversation;
    const scope = describeScope();
    $('#scope-label').textContent = scope;
    $('#scope-chip').hidden = !scope;
    $('#scope-chip').title = scope;
    $('#chat-kb').value = currentKb;
    document.querySelectorAll('.history-item').forEach(button => {
      const current = String(button.dataset.id) === String(conversation?.id);
      button.classList.toggle('current', current);
      if (current) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    });
    updateLocation();
    updateControls();
  }

  function closeSidebar() {
    $('#chat-sidebar').classList.remove('open');
    $('#history-toggle').setAttribute('aria-expanded', 'false');
    $('#history-toggle').setAttribute('aria-label', 'Mostra le conversazioni');
  }

  function scrollBottom() {
    requestAnimationFrame(() => { $('#chat-scroll').scrollTop = $('#chat-scroll').scrollHeight; });
  }

  function renderMessage(message) {
    const assistant = message.role === 'assistant';
    const article = node('article', 'chat-message ' + (assistant ? 'assistant' : 'user'));
    const heading = node('div', 'message-heading');
    const avatar = node('span', 'message-avatar', assistant ? 'N' : 'TU');
    avatar.setAttribute('aria-hidden', 'true');
    heading.append(avatar, node('span', null, assistant ? 'NIKI' : 'Tu'));
    // Il modello e i documenti sono testo: non possono inserire HTML nella pagina.
    article.append(heading, node('div', 'message-content', message.content || ''));
    const sources = Array.isArray(message.sources) ? message.sources.filter(source => typeof source.doc === 'string' && source.doc) : [];
    if (assistant && sources.length) {
      const section = node('section', 'message-sources');
      section.setAttribute('aria-label', 'Fonti della risposta');
      section.append(node('h3', null, 'Fonti · ' + sources.length));
      const list = node('div', 'source-list');
      for (const [index, source] of sources.entries()) {
        const button = node('button', 'source-button');
        button.type = 'button';
        const label = source.title || source.doc;
        button.title = 'Anteprima: ' + label;
        button.append(node('span', 'source-number', '[' + (source.number || index + 1) + ']'), node('span', 'source-name', label));
        button.addEventListener('click', () => openSource(source));
        list.append(button);
      }
      section.append(list);
      article.append(section);
    }
    return article;
  }

  function renderMessages() {
    $('#chat-messages').replaceChildren(...messages.map(renderMessage));
    $('#chat-welcome').hidden = messages.length > 0;
    scrollBottom();
  }

  async function openSource(source) {
    const request = ++previewRequest;
    const preview = $('#source-preview');
    $('#source-title').textContent = source.title || source.doc;
    $('#source-path').textContent = source.doc;
    $('#source-file').href = kbUrl('/api/file?path=' + encodeURIComponent(source.doc));
    $('#source-excerpt-text').textContent = source.text || '';
    $('#source-excerpt').hidden = !source.text;
    $('#source-text').textContent = 'Carico il documento…';
    if (!preview.open) preview.showModal();
    preview.scrollTop = 0;
    try {
      const data = await api(kbUrl('/api/doc?path=' + encodeURIComponent(source.doc)));
      if (request !== previewRequest) return;
      $('#source-text').textContent = typeof data === 'string' ? data : (data.text || data.content || 'Nessun testo disponibile per questo documento.');
    } catch (err) {
      if (request === previewRequest) $('#source-text').textContent = 'Anteprima non disponibile: ' + err.message;
    }
  }

  function formatDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return new Intl.DateTimeFormat('it-IT', {day:'numeric', month:'short', hour:'2-digit', minute:'2-digit'}).format(date);
  }

  async function loadHistory() {
    const request = ++historyRequest;
    const kb = currentKb;
    $('#history-status').textContent = 'Carico le conversazioni…';
    $('#history-status').hidden = false;
    $('#history-retry').hidden = true;
    try {
      const data = await api(kbUrl('/api/chats'));
      if (request !== historyRequest || kb !== currentKb) return;
      const list = Array.isArray(data.conversations) ? data.conversations : [];
      $('#chat-history').replaceChildren();
      for (const item of list) {
        const button = node('button', 'history-item');
        button.type = 'button';
        button.dataset.id = item.id;
        button.title = item.title || 'Conversazione';
        button.append(node('span', 'history-title', item.title || 'Conversazione'), node('span', 'history-date', formatDate(item.updated_at)));
        button.addEventListener('click', () => openConversation(item.id));
        $('#chat-history').append(button);
      }
      $('#history-status').textContent = 'Le domande che fai qui compariranno in questo elenco.';
      $('#history-status').hidden = list.length > 0;
      updateContext();
    } catch (err) {
      if (request !== historyRequest || kb !== currentKb) return;
      $('#history-status').textContent = 'Non riesco a caricare la cronologia: ' + err.message;
      $('#history-retry').hidden = false;
    }
  }

  function resetConversation({clearFilters = false, focus = true} = {}) {
    conversation = null;
    messages = [];
    if (clearFilters) filters = {};
    $('#chat-question').value = '';
    resizeQuestion();
    error();
    renderMessages();
    updateContext();
    closeSidebar();
    if (focus) $('#chat-question').focus();
  }

  async function openConversation(id) {
    if (busy || loading) return;
    loading = true;
    error();
    updateControls();
    try {
      const data = await api('/api/chats/' + encodeURIComponent(id));
      if (!data.conversation || !Array.isArray(data.messages)) throw new Error('Conversazione non disponibile.');
      const changedKb = currentKb !== data.conversation.kb;
      conversation = data.conversation;
      currentKb = conversation.kb;
      filters = conversation.filters || {};
      messages = data.messages;
      $('#chat-question').value = '';
      resizeQuestion();
      renderMessages();
      closeSidebar();
      if (changedKb) await loadHistory();
    } catch (err) {
      error('Non riesco ad aprire la conversazione: ' + err.message);
    } finally {
      loading = false;
      updateContext();
      $('#chat-question').focus();
    }
  }

  async function sendQuestion(event) {
    event.preventDefault();
    const question = $('#chat-question').value.trim();
    if (!question || busy || loading || !currentKb) return;
    error();
    busy = true;
    $('#chat-question').value = '';
    resizeQuestion();
    $('#chat-welcome').hidden = true;
    const pendingUser = renderMessage({role:'user', content:question});
    $('#chat-messages').append(pendingUser);
    updateControls();
    scrollBottom();
    try {
      const data = await api('/api/chat', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({kb:currentKb, conversation_id:conversation?.id || null, question, filters}),
      });
      if (!data.conversation || !Array.isArray(data.messages)) throw new Error('Risposta non disponibile. Riprova.');
      conversation = data.conversation;
      filters = conversation.filters || filters;
      messages.push(...data.messages);
      pendingUser.remove();
      for (const message of data.messages) $('#chat-messages').append(renderMessage(message));
      updateContext();
      await loadHistory();
    } catch (err) {
      pendingUser.remove();
      $('#chat-welcome').hidden = messages.length > 0;
      $('#chat-question').value = question;
      resizeQuestion();
      error(err.message);
    } finally {
      busy = false;
      updateControls();
      scrollBottom();
      $('#chat-question').focus();
    }
  }

  async function deleteConversation() {
    if (!conversation || busy || loading) return;
    const confirmed = await Dialogo.conferma({
      titolo:'Eliminare la conversazione?',
      testo:'La conversazione e i suoi messaggi saranno eliminati dalla tua cronologia.',
      oggetto:conversation.title || 'Conversazione',
      tono:'pericolo', ok:'Elimina',
    });
    if (!confirmed) return;
    loading = true;
    error();
    updateControls();
    try {
      await api('/api/chats/' + encodeURIComponent(conversation.id), {method:'DELETE'});
      resetConversation({focus:false});
      await loadHistory();
    } catch (err) {
      error('Non riesco a eliminare la conversazione: ' + err.message);
    } finally {
      loading = false;
      updateControls();
      $('#chat-question').focus();
    }
  }

  function resizeQuestion() {
    const input = $('#chat-question');
    input.style.height = 'auto';
    input.style.height = Math.min(180, Math.max(46, input.scrollHeight)) + 'px';
  }

  async function initialize() {
    loading = true;
    error();
    updateControls();
    try {
      const data = await api('/api/kbs');
      knowledges = data.knowledges || [];
      if (!knowledges.some(kb => kb.slug === currentKb)) {
        if (currentKb) filters = {};
        currentKb = knowledges[0]?.slug || '';
      }
      $('#chat-kb').replaceChildren();
      for (const kb of knowledges) {
        const option = node('option', null, kb.name || kb.slug);
        option.value = kb.slug;
        $('#chat-kb').append(option);
      }
      if (!currentKb) {
        $('#chat-kb').append(node('option', null, 'Nessuna knowledge'));
        $('#history-status').textContent = 'Nessuna knowledge disponibile.';
        error('Apri Knowledge e grafo per creare una knowledge e aggiungere i documenti.');
      } else {
        initialized = true;
        await loadHistory();
      }
      loading = false;
      updateContext();
      if (params.get('conversation') && currentKb) await openConversation(params.get('conversation'));
    } catch (err) {
      error(err.message);
      $('#history-status').textContent = 'Non riesco a caricare le knowledge.';
      $('#history-retry').hidden = false;
    } finally {
      loading = false;
      updateControls();
    }
  }

  $('#chat-form').addEventListener('submit', sendQuestion);
  $('#chat-question').addEventListener('input', () => { resizeQuestion(); updateControls(); });
  $('#chat-question').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!$('#send-question').disabled) $('#chat-form').requestSubmit();
    }
  });
  $('#new-chat').addEventListener('click', () => resetConversation());
  $('#clear-scope').addEventListener('click', () => resetConversation({clearFilters:true}));
  $('#delete-chat').addEventListener('click', deleteConversation);
  $('#history-retry').addEventListener('click', () => initialized ? loadHistory() : initialize());
  $('#chat-kb').addEventListener('change', async event => {
    if (busy || loading) return;
    currentKb = event.target.value;
    $('#chat-history').replaceChildren();
    resetConversation({clearFilters:true});
    await loadHistory();
  });
  $('#history-toggle').addEventListener('click', () => {
    const open = $('#chat-sidebar').classList.toggle('open');
    $('#history-toggle').setAttribute('aria-expanded', String(open));
    $('#history-toggle').setAttribute('aria-label', open ? 'Nascondi le conversazioni' : 'Mostra le conversazioni');
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('#chat-sidebar, #history-toggle')) closeSidebar();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && $('#chat-sidebar').classList.contains('open')) {
      closeSidebar();
      $('#history-toggle').focus();
    }
  });
  document.querySelectorAll('[data-question]').forEach(button => {
    button.addEventListener('click', () => {
      $('#chat-question').value = button.dataset.question;
      resizeQuestion();
      updateControls();
      $('#chat-question').focus();
    });
  });
  $('#source-close').addEventListener('click', () => $('#source-preview').close());
  $('#source-preview').addEventListener('close', () => { previewRequest++; });
  initialize();
})();
