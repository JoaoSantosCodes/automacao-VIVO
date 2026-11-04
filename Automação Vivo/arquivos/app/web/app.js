/* Robô de Chamados Vivo — interface web local.
 * Sem dependências externas. Todo texto vindo do robô entra no DOM por
 * textContent, nunca como HTML. */
'use strict';

const ISSUE_LABEL = { link_interrompido: 'Link inoperante', perca_pacotes: 'Perda de pacotes' };
const $ = (id) => document.getElementById(id);

const ui = {
  state: { busy: false, simulation: false, operation: null, prompts: [], log: [] },
  lookup: null,          // resultado da última consulta
  preview: null,         // resultado da última revisão
  previewSeq: 0,
  batchReview: null,     // revisão de lote pendente
  modalPromptId: null,   // confirmação do worker exibida no modal
  modalLocal: false,     // modal aberto por ação local (não pelo worker)
  logIds: new Set(),
  settings: null,
  history: null,
  closed: false,
};

/* ------------------------------------------------------------------ util */

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function normKey(value) {
  return String(value).normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
}

function pick(obj, ...keys) {
  if (!obj || typeof obj !== 'object') return '';
  const index = {};
  for (const [key, value] of Object.entries(obj)) index[normKey(key)] = value;
  for (const key of keys) {
    const value = index[normKey(key)];
    if (value !== undefined && value !== null && String(value).trim() !== '') return String(value);
  }
  return '';
}

function fmtTime(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtDateTime(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function toast(message, kind) {
  const el = h('div', { class: `toast ${kind || ''}`, text: message });
  $('toasts').append(el);
  setTimeout(() => el.remove(), kind === 'error' ? 7000 : 3500);
}

async function copyText(text, okMessage) {
  const value = String(text || '').trim();
  if (!value) { toast('Nada para copiar.'); return; }
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const area = h('textarea', {});
    area.value = value;
    document.body.append(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }
  toast(okMessage || 'Copiado.');
}

class ApiError extends Error {
  constructor(status, data) {
    super((data && data.error) || `Erro ${status}`);
    this.status = status;
    this.data = data || {};
  }
}

async function api(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Robo-Vivo': '1' },
    body: JSON.stringify(body || {}),
    credentials: 'same-origin',
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

async function apiGet(path) {
  const response = await fetch(path, { credentials: 'same-origin' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

function show(el, visible) { el.hidden = !visible; }

function statusChip(status) {
  const value = String(status || '').toUpperCase();
  let kind = '';
  if (['ABERTO', 'RECONHECIDO', 'ENVIADO'].includes(value)) kind = 'ok';
  else if (['PROTOCOLO PENDENTE', 'ENVIO INCERTO', 'PENDENTE', 'VALIDAÇÃO NECESSÁRIA', 'NÃO CONFIGURADO', 'DESATIVADO', 'AGUARDANDO CONFIRMAÇÃO', 'SIMULAÇÃO'].includes(value)) kind = 'warn';
  else if (['ERRO', 'ERRO NA BASE', 'CANCELADO'].includes(value)) kind = 'err';
  else if (['EM ANDAMENTO', 'NA FILA', 'ENVIADO — AGUARDANDO PROTOCOLO', 'AGUARDANDO', 'EXECUTANDO', 'ZABBIX'].includes(value)) kind = 'run';
  else if (['NÃO ENVIADO', 'NÃO SOLICITADO', 'NÃO PROCESSADO'].includes(value)) kind = '';
  return h('span', { class: `chip ${kind}`, text: value || '—' });
}

/* ------------------------------------------------------------------ abas */

function selectTab(name) {
  document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('is-active', tab.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach((panel) => panel.classList.toggle('is-active', panel.id === `tab-${name}`));
  if (name === 'historico') loadHistory();
  if (name === 'config') loadSettings();
}

/* ------------------------------------------------------------------ eventos SSE */

function connect() {
  const conn = $('conn');
  const source = new EventSource('/api/events');
  source.onopen = () => { conn.className = 'conn is-on'; $('conn-text').textContent = 'Conectado'; };
  source.onerror = () => {
    if (ui.closed) { source.close(); return; }
    conn.className = 'conn is-off';
    $('conn-text').textContent = 'Reconectando…';
  };
  source.onmessage = (message) => {
    let event;
    try { event = JSON.parse(message.data); } catch { return; }
    if (event.type === 'state') applyState(event.state);
    else if (event.type === 'log') appendLog(event.entry);
    else if (event.type === 'shutdown') {
      ui.closed = true;
      source.close();
      show($('closed'), true);
    }
  };
}

function applyState(state) {
  ui.state = state;
  show($('sim-badge'), !!state.simulation);
  for (const entry of state.log || []) appendLog(entry);
  renderTicketOperation();
  renderBatchOperation();
  renderBusy();
  renderPrompts();
}

function appendLog(entry) {
  if (!entry) return;
  const key = `${entry.time}|${entry.message}`;
  if (ui.logIds.has(key)) return;
  ui.logIds.add(key);
  const list = $('log');
  const item = h('li', { class: entry.level || '' }, h('time', { text: fmtTime(entry.time) }), h('span', { text: entry.message }));
  list.prepend(item);
  while (list.children.length > 300) list.lastChild.remove();
}

function renderBusy() {
  const { busy, operation } = ui.state;
  const banner = $('busy-banner');
  show(banner, !!busy);
  if (busy && operation) {
    const what = operation.kind === 'batch' ? 'Lote em andamento' : `Chamado ${operation.vd} em andamento`;
    $('busy-banner-text').textContent = `${what}: ${operation.message || ''}`;
  }
  // Como no Tkinter: depois de um chamado, só uma nova consulta libera outro.
  $('btn-open-ticket').disabled = !!busy || !!(operation && operation.kind === 'ticket');
  $('btn-batch-prepare').disabled = !!busy;
  $('btn-batch-start').disabled = !!busy;
  document.querySelectorAll('#vivo-form button, #zbx-form button').forEach((btn) => { btn.disabled = !!busy && btn.id !== 'btn-zbx-test'; });
  $('btn-shutdown').disabled = !!busy;
}

/* ------------------------------------------------------------------ chamado: consulta */

async function onLookup(event) {
  event.preventDefault();
  const entrada = $('entrada').value.trim();
  if (!entrada) return;
  const button = $('btn-lookup');
  button.disabled = true;
  show($('lookup-error'), false);
  resetTicketForm();
  try {
    const data = await api('/api/lookup', { entrada });
    ui.lookup = { ...data, entrada };
    renderLookup();
    await refreshPreview();
  } catch (error) {
    $('lookup-error').textContent = error.message;
    show($('lookup-error'), true);
  } finally {
    button.disabled = false;
  }
}

function resetTicketForm() {
  ui.lookup = null;
  ui.preview = null;
  show($('store-card'), false);
  show($('circuit-card'), false);
  show($('issue-card'), false);
  show($('preview-card'), false);
}

function renderLookup() {
  const data = ui.lookup;
  const store = data.store;
  $('store-name').textContent = store.nome || 'Nome não informado';
  $('store-vd').textContent = data.vd;
  show($('store-24h'), !!store.vinte_quatro_horas);
  $('store-address').textContent = [store.endereco, store.bairro, `${store.cidade}/${store.uf}`, store.cep ? `CEP ${store.cep}` : ''].filter(Boolean).join(' — ');
  $('store-hours').textContent = store.vinte_quatro_horas
    ? '24 horas (00:00 às 23:59)'
    : `${store.horario || ''}${store.hora_inicio ? ` (${store.hora_inicio} às ${store.hora_fim})` : ''}`;
  $('store-host').textContent = data.zabbix_host || 'Será localizado pelo VD (exige um único incidente ativo)';
  show($('store-card'), true);

  const list = $('links');
  list.replaceChildren();
  data.links.forEach((link, index) => {
    const input = h('input', { type: 'radio', name: 'link', value: link.designacao, checked: index === 0 });
    input.addEventListener('change', refreshPreview);
    list.append(h('label', { class: 'choice' },
      input,
      h('span', { class: 'choice-main', text: link.designacao || 'Sem designação' }),
      h('span', { class: 'choice-meta', text: [link.operadora, link.tipo_acesso, link.velocidade, link.usuario].filter(Boolean).join(' · ') }),
    ));
  });
  if (data.links.length > 1) {
    list.prepend(h('div', { class: 'alert alert-warn', text: `Esta loja tem ${data.links.length} links Vivo MPLS ativos. Escolha o circuito correto.` }));
  }
  show($('circuit-card'), true);
  show($('issue-card'), true);
}

function selectedLink() {
  const checked = document.querySelector('input[name="link"]:checked');
  return checked ? checked.value : '';
}

function selectedIssue(name) {
  const checked = document.querySelector(`input[name="${name || 'issue'}"]:checked`);
  return checked ? checked.value : 'link_interrompido';
}

async function refreshPreview() {
  if (!ui.lookup) return;
  const seq = ++ui.previewSeq;
  try {
    const data = await api('/api/ticket/preview', {
      entrada: ui.lookup.entrada,
      problema: selectedIssue(),
      designacao: selectedLink(),
    });
    if (seq !== ui.previewSeq) return;
    ui.preview = data;
    renderPreview();
  } catch (error) {
    if (seq !== ui.previewSeq) return;
    ui.preview = null;
    show($('preview-card'), false);
    $('lookup-error').textContent = error.message;
    show($('lookup-error'), true);
  }
}

function confirmationRows(confirmacao) {
  return [
    ['Loja', confirmacao.loja],
    ['Host Zabbix', confirmacao.host],
    ['Endereço', confirmacao.endereco],
    ['Designação', confirmacao.designacao],
    ['Horário no portal', confirmacao.horario],
    ['Problema', confirmacao.problema],
  ];
}

function fillKv(dl, rows) {
  dl.replaceChildren();
  for (const [label, value] of rows) dl.append(h('dt', { text: label }), h('dd', { text: value || '—' }));
}

function renderPreview() {
  const data = ui.preview;
  fillKv($('preview-kv'), confirmationRows(data.confirmacao));
  $('preview-text').textContent = data.descricao;
  const dup = $('preview-dup');
  dup.textContent = `Já existe um chamado registrado nas últimas ${data.janela_duplicidade_horas}h para este VD e circuito. Confira o histórico antes de abrir outro.`;
  show(dup, !!data.duplicado);
  show($('preview-card'), true);
}

function onOpenTicket() {
  const data = ui.preview;
  if (!data || ui.state.busy) return;
  const body = h('div', {},
    h('p', { text: 'Confira os dados da base antes de iniciar a automação:' }),
    (() => { const dl = h('dl', { class: 'kv' }); fillKv(dl, confirmationRows(data.confirmacao)); return dl; })(),
  );
  let dupCheck = null;
  if (data.duplicado) {
    dupCheck = h('input', { type: 'checkbox' });
    body.append(
      h('div', { class: 'alert alert-warn', text: `Há um chamado recente (últimas ${data.janela_duplicidade_horas}h) para este VD e circuito.` }),
      h('label', { class: 'check' }, dupCheck, 'Conferi o histórico e quero abrir um novo chamado mesmo assim.'),
    );
  }
  if (ui.state.simulation) body.append(h('div', { class: 'alert alert-info', text: 'Modo simulação: nada será enviado à Vivo nem ao Zabbix.' }));
  openLocalModal('Os dados acima estão corretos?', body, [
    { label: 'Voltar', value: 'no' },
    {
      label: 'Sim, iniciar automação', value: 'yes', primary: true,
      onClick: async (close) => {
        if (dupCheck && !dupCheck.checked) { toast('Marque a confirmação de duplicidade para continuar.', 'error'); return; }
        try {
          await api('/api/ticket/start', {
            entrada: ui.lookup.entrada,
            problema: selectedIssue(),
            designacao: selectedLink(),
            aceitar_duplicidade: !!(dupCheck && dupCheck.checked),
          });
          close();
        } catch (error) {
          close();
          if (error.data.action === 'configurar_vivo') { toast(error.message, 'error'); selectTab('config'); return; }
          toast(error.message, 'error');
        }
      },
    },
  ]);
}

/* ------------------------------------------------------------------ chamado: andamento */

function renderTicketOperation() {
  const op = ui.state.operation;
  const isTicket = op && op.kind === 'ticket';
  show($('op-empty'), !isTicket);
  show($('op-ticket'), !!isTicket);
  if (!isTicket) return;
  const done = op.phase === 'concluido';
  $('op-title').textContent = `${op.vd} — ${op.loja || ''}`;
  $('op-sub').textContent = `${op.issue_label} · ${op.designacao}${op.simulation ? ' · SIMULAÇÃO' : ''}`;
  const result = op.result || null;
  const chipText = done ? (result ? result.status : 'ENCERRADO') : (op.phase === 'zabbix' ? 'ZABBIX' : 'EXECUTANDO');
  $('op-chip').replaceWith(Object.assign(statusChip(chipText), { id: 'op-chip' }));
  $('op-status').classList.toggle('is-done', done);
  $('op-status-text').textContent = op.message || '';
  show($('btn-cancel-ticket'), !done && op.phase !== 'zabbix');
  show($('btn-new-ticket'), done);

  const hasResult = !!(result && (result.protocol || result.case_number || result.support_phrase || result.status));
  show($('op-result'), hasResult);
  if (!hasResult) return;
  $('res-protocol').textContent = result.protocol || '—';
  $('res-case').textContent = result.case_number || '—';
  document.querySelectorAll('#op-result [data-copy-from]').forEach((btn) => {
    const source = $(btn.dataset.copyFrom);
    if (source && source.tagName !== 'TEXTAREA') btn.disabled = !source.textContent || source.textContent === '—';
  });
  const msg = $('res-message');
  msg.className = `result-message alert ${result.status === 'ABERTO' ? 'alert-ok' : (result.status === 'ERRO' ? 'alert-error' : 'alert-warn')}`;
  msg.textContent = result.message || '';
  const zbx = $('res-zabbix');
  zbx.replaceChildren();
  if (result.zabbix_status) zbx.append(h('b', { text: 'Zabbix: ' }), statusChip(result.zabbix_status), ' ', result.zabbix_detail || '');
  else if (op.phase === 'zabbix') zbx.append(h('b', { text: 'Zabbix: ' }), 'reconhecimento em andamento…');
  show($('res-support'), !!result.support_phrase);
  $('res-support-text').value = result.support_phrase || '';
}

async function onCancel() {
  const op = ui.state.operation;
  const submitted = op && (op.submission_seen || (op.items || []).some((item) => String(item.status).startsWith('ENVIADO')));
  const body = h('div', {},
    h('p', { text: 'Interromper fecha o navegador da automação imediatamente.' }),
    submitted ? h('div', { class: 'alert alert-warn', text: 'Algum chamado já pode ter sido enviado. Confira o portal antes de tentar de novo.' }) : null,
  );
  openLocalModal('Interromper a automação?', body, [
    { label: 'Continuar executando', value: 'no' },
    { label: 'Interromper', value: 'yes', danger: true, onClick: async (close) => { close(); try { await api('/api/cancel'); } catch (error) { toast(error.message, 'error'); } } },
  ]);
}

async function onClearOperation() {
  try { await api('/api/operation/clear'); } catch (error) { toast(error.message, 'error'); }
}

/* ------------------------------------------------------------------ confirmações do worker */

function renderPrompts() {
  const prompts = ui.state.prompts || [];
  const modal = $('modal');
  if (ui.modalLocal) return; // não sobrepõe uma confirmação local aberta
  if (!prompts.length) {
    if (ui.modalPromptId && modal.open) modal.close();
    ui.modalPromptId = null;
    return;
  }
  const current = prompts.find((prompt) => prompt.id === ui.modalPromptId) || prompts[0];
  if (current.id === ui.modalPromptId && modal.open) {
    updateCounter(prompts.length);
    return;
  }
  ui.modalPromptId = current.id;
  if (current.stage === 'portal_candidate') showCandidatePrompt(current);
  else showPreviewPrompt(current);
  updateCounter(prompts.length);
}

function updateCounter(count) {
  const counter = $('modal-counter');
  counter.textContent = `${count} aguardando`;
  show(counter, count > 1);
}

function compareRow(label, portal, base) {
  const same = portal && base && normKey(portal) === normKey(base);
  return h('tr', {}, h('th', { text: label }), h('td', { class: same ? 'match' : '', text: portal || '—' }), h('td', { text: base || '—' }));
}

function showCandidatePrompt(prompt) {
  const c = prompt.candidate || {};
  const e = prompt.expected || {};
  const title = prompt.position ? `Confirmar circuito — ${prompt.vd} (${prompt.position}/${prompt.total})` : `Confirmar circuito — ${prompt.vd}`;
  const table = h('table', { class: 'compare' },
    h('thead', {}, h('tr', {}, h('th', {}), h('th', { text: 'Encontrado no portal' }), h('th', { text: 'Esperado pela base' }))),
    h('tbody', {},
      compareRow('Empresa', pick(c, 'empresa', 'company'), ''),
      compareRow('Produto', pick(c, 'produto', 'produto_servico', 'product'), ''),
      compareRow('Loja', '', `${pick(e, 'vd') || prompt.vd} — ${pick(e, 'store_name', 'nome_loja') || prompt.loja}`),
      compareRow('Endereço', pick(c, 'endereco', 'address'), pick(e, 'address', 'endereco')),
      compareRow('Cidade', pick(c, 'cidade', 'city'), pick(e, 'city', 'cidade')),
      compareRow('Identificação', pick(c, 'identificacao', 'identification'), pick(e, 'identification', 'identificacao')),
      compareRow('ID do serviço', pick(c, 'id_servico', 'service_id', 'id'), pick(e, 'service_id', 'id_servico')),
      compareRow('Velocidade', pick(c, 'velocidade', 'speed'), ''),
      compareRow('Nº do contrato', pick(c, 'contrato', 'contract'), ''),
    ),
  );
  const body = h('div', {},
    prompt.simulated ? h('div', { class: 'alert alert-info', text: 'Candidato simulado: nenhum acesso real ao portal.' }) : null,
    h('p', { text: 'Compare o circuito encontrado no portal com os dados da base. Valores iguais aparecem em verde.' }),
    table,
  );
  openPromptModal(title, body, [
    { label: 'Não, procurar outro', confirmed: false },
    { label: 'Sim, é este o circuito', confirmed: true, primary: true },
  ], prompt);
}

function showPreviewPrompt(prompt) {
  const p = prompt.preview || {};
  const dl = h('dl', { class: 'kv' });
  fillKv(dl, [
    ['Loja', `${pick(p, 'vd') || prompt.vd} — ${pick(p, 'store_name', 'nome_loja') || prompt.loja}`],
    ['Endereço', pick(p, 'endereco', 'address')],
    ['Identificação', pick(p, 'identificacao', 'identification')],
    ['Horário', `${pick(p, 'hora_inicio', 'start_time')} às ${pick(p, 'hora_fim', 'hora_termino', 'end_time')}`],
    ['Contato', [pick(p, 'contact_name'), pick(p, 'contact_phone'), pick(p, 'contact_email')].filter(Boolean).join(' · ')],
  ]);
  const body = h('div', {},
    prompt.simulated ? h('div', { class: 'alert alert-info', text: 'Simulação: o clique em Enviar não será feito de verdade.' }) : null,
    dl,
    h('div', { class: 'label', text: 'Descrição' }),
    h('pre', { class: 'preview-text', text: pick(p, 'descricao', 'description') }),
    h('div', { class: 'alert alert-warn', text: 'Ao autorizar, o robô clica em Enviar uma única vez. Esta confirmação não será repetida.' }),
  );
  openPromptModal(`Revisão final antes do envio — ${prompt.vd}`, body, [
    { label: 'Cancelar envio', confirmed: false },
    { label: 'Autorizar envio', confirmed: true, primary: true },
  ], prompt);
}

function openPromptModal(title, body, buttons, prompt) {
  const modal = $('modal');
  $('modal-title').textContent = title;
  $('modal-body').replaceChildren(body);
  const actions = $('modal-actions');
  actions.replaceChildren();
  for (const button of buttons) {
    actions.append(h('button', {
      type: 'button',
      class: `btn ${button.primary ? 'btn-primary' : ''}`,
      text: button.label,
      onclick: async (event) => {
        actions.querySelectorAll('button').forEach((b) => { b.disabled = true; });
        try {
          await api('/api/respond', { prompt_id: prompt.id, confirmado: button.confirmed });
        } catch (error) {
          toast(error.message, 'error');
        }
        if (ui.modalPromptId === prompt.id) {
          ui.modalPromptId = null;
          modal.close();
        }
        event.preventDefault();
        renderPrompts();
      },
    }));
  }
  if (!modal.open) modal.showModal();
}

function openLocalModal(title, body, buttons) {
  const modal = $('modal');
  ui.modalLocal = true;
  $('modal-title').textContent = title;
  show($('modal-counter'), false);
  $('modal-body').replaceChildren(body);
  const actions = $('modal-actions');
  actions.replaceChildren();
  const close = () => {
    ui.modalLocal = false;
    if (modal.open) modal.close();
    renderPrompts();
  };
  for (const button of buttons) {
    actions.append(h('button', {
      type: 'button',
      class: `btn ${button.primary ? 'btn-primary' : ''} ${button.danger ? 'btn-danger-ghost' : ''}`,
      text: button.label,
      onclick: async () => {
        if (!button.onClick) { close(); return; }
        actions.querySelectorAll('button').forEach((b) => { b.disabled = true; });
        await button.onClick(close);
        actions.querySelectorAll('button').forEach((b) => { b.disabled = false; });
      },
    }));
  }
  if (modal.open) modal.close();
  modal.showModal();
}

/* ------------------------------------------------------------------ lote */

async function onBatchPrepare() {
  const entrada = $('batch-input').value;
  show($('batch-error'), false);
  const button = $('btn-batch-prepare');
  button.disabled = true;
  try {
    ui.batchReview = await api('/api/batch/prepare', { entrada, problema: selectedIssue('batch-issue') });
    renderBatchReview();
  } catch (error) {
    $('batch-error').textContent = error.message;
    show($('batch-error'), true);
  } finally {
    button.disabled = !!ui.state.busy;
  }
}

function renderBatchReview() {
  const data = ui.batchReview;
  const card = $('batch-review');
  if (!data) { show(card, false); return; }
  $('batch-review-issue').textContent = data.issue_label;
  const ignored = $('batch-ignored');
  ignored.textContent = data.ignorados && data.ignorados.length
    ? `Itens ignorados por não parecerem VD nem host: ${data.ignorados.join(', ')}`
    : '';
  show(ignored, !!(data.ignorados && data.ignorados.length));
  const rows = [];
  for (const item of data.items) {
    const warnings = [];
    if (item.duplicado) warnings.push(h('span', { class: 'chip warn', text: 'POSSÍVEL DUPLICIDADE' }));
    if (item.circuitos > 1) warnings.push(h('span', { class: 'chip warn', text: `${item.circuitos} CIRCUITOS: USA O 1º` }));
    rows.push({ position: item.position, el: h('tr', {},
      h('td', { text: item.position }),
      h('td', { class: 'mono', text: item.vd }),
      h('td', { class: 'mono', text: item.zabbix_host || 'localizar pelo VD' }),
      h('td', { text: item.loja }),
      h('td', { class: 'mono', text: item.designacao }),
      h('td', {}, ...warnings),
    ) });
  }
  for (const failure of data.failures) {
    rows.push({ position: failure.position, el: h('tr', { class: 'is-fail' },
      h('td', { text: failure.position }),
      h('td', { class: 'mono', text: failure.vd }),
      h('td', { class: 'mono', text: failure.zabbix_host || '' }),
      h('td', { colspan: 3, text: `Não será processado: ${failure.message}` }),
    ) });
  }
  rows.sort((a, b) => a.position - b.position);
  $('batch-review-body').replaceChildren(...rows.map((row) => row.el));
  $('batch-tabs').textContent = data.concorrencia;
  const start = $('btn-batch-start');
  start.textContent = `Autorizar lote (${data.items.length} chamado${data.items.length === 1 ? '' : 's'})`;
  start.disabled = !data.items.length || !!ui.state.busy;
  show(card, true);
}

function onBatchStart() {
  const data = ui.batchReview;
  if (!data || !data.items.length) return;
  const duplicates = data.items.filter((item) => item.duplicado).length;
  const body = h('div', {},
    h('p', {}, 'Problema aplicado a todo o lote: ', h('b', { text: data.issue_label })),
    h('p', { text: `${data.items.length} chamado(s) serão abertos; ${data.failures.length} VD(s) com erro na base não serão processados.` }),
    duplicates ? h('div', { class: 'alert alert-warn', text: `${duplicates} VD(s) têm chamado recente registrado (possível duplicidade).` }) : null,
    h('p', { class: 'muted', text: 'No lote, a autorização do envio vale para todos os VDs. O circuito de cada loja ainda pede sua confirmação.' }),
    ui.state.simulation ? h('div', { class: 'alert alert-info', text: 'Modo simulação: nada será enviado à Vivo nem ao Zabbix.' }) : null,
  );
  openLocalModal('Autoriza iniciar o lote?', body, [
    { label: 'Voltar', value: 'no' },
    {
      label: 'Autorizar lote', primary: true,
      onClick: async (close) => {
        try {
          await api('/api/batch/start', { batch_id: data.batch_id });
          ui.batchReview = null;
          renderBatchReview();
          close();
        } catch (error) {
          close();
          if (error.data.action === 'configurar_vivo') { toast(error.message, 'error'); selectTab('config'); return; }
          if (error.status === 410) { ui.batchReview = null; renderBatchReview(); }
          toast(error.message, 'error');
        }
      },
    },
  ]);
}

const batchSelection = new Set();

function renderBatchOperation() {
  const op = ui.state.operation;
  const isBatch = op && op.kind === 'batch';
  show($('batch-progress'), !!isBatch);
  show($('batch-form-card'), !isBatch || op.phase === 'concluido');
  if (!isBatch) return;
  const done = op.phase === 'concluido';
  $('batch-chip').replaceWith(Object.assign(statusChip(done ? 'CONCLUÍDO' : (op.phase === 'zabbix' ? 'ZABBIX' : 'EXECUTANDO')), { id: 'batch-chip' }));
  $('batch-status').classList.toggle('is-done', done);
  $('batch-status-text').textContent = op.message || '';
  show($('btn-cancel-batch'), !done && op.phase !== 'zabbix');
  show($('btn-batch-new'), done);

  const counts = { total: op.items.length, abertos: 0, pendentes: 0, falhas: 0, andamento: 0 };
  for (const item of op.items) {
    if (['ABERTO', 'ENVIADO'].includes(item.status)) counts.abertos += 1;
    else if (['PROTOCOLO PENDENTE', 'ENVIO INCERTO'].includes(item.status)) counts.pendentes += 1;
    else if (['NA FILA', 'EM ANDAMENTO', 'AGUARDANDO CONFIRMAÇÃO', 'ENVIADO — AGUARDANDO PROTOCOLO'].includes(item.status)) counts.andamento += 1;
    else counts.falhas += 1;
  }
  $('batch-counters').replaceChildren(
    h('div', { class: 'counter' }, h('b', { text: counts.total }), 'total'),
    h('div', { class: 'counter' }, h('b', { text: counts.andamento }), 'em andamento'),
    h('div', { class: 'counter' }, h('b', { text: counts.abertos }), 'abertos'),
    h('div', { class: 'counter' }, h('b', { text: counts.pendentes }), 'pendentes'),
    h('div', { class: 'counter' }, h('b', { text: counts.falhas }), 'falhas / não enviados'),
  );

  const rows = op.items.map((item) => {
    const key = String(item.position);
    const box = h('input', { type: 'checkbox', checked: batchSelection.has(key) });
    box.addEventListener('change', () => { if (box.checked) batchSelection.add(key); else batchSelection.delete(key); });
    const detail = [item.message, item.zabbix_detail].filter(Boolean).join(' — ');
    return h('tr', {},
      h('td', {}, box),
      h('td', { text: item.position }),
      h('td', { class: 'mono', text: item.vd }),
      h('td', { class: 'mono', text: item.zabbix_host || '—' }),
      h('td', {}, statusChip(item.status)),
      h('td', { class: 'mono', text: item.protocol || '' }),
      h('td', { class: 'mono', text: item.case_number || '' }),
      h('td', {}, item.zabbix_status ? statusChip(item.zabbix_status) : ''),
      h('td', { class: 'detail', text: detail }),
    );
  });
  $('batch-body').replaceChildren(...rows);
}

function chosenBatchItems() {
  const op = ui.state.operation;
  if (!op || op.kind !== 'batch') return [];
  const chosen = op.items.filter((item) => batchSelection.has(String(item.position)));
  return chosen.length ? chosen : op.items;
}

function clean(value) { return String(value || '').replace(/[\t\r\n]+/g, ' ').trim(); }

function onBatchCopy(kind) {
  const items = chosenBatchItems();
  if (kind === 'cases') {
    copyText(items.map((item) => clean(item.case_number)).filter(Boolean).join('\n'), 'Casos copiados.');
  } else if (kind === 'details') {
    copyText(items.map((item) => [clean(item.message), clean(item.zabbix_detail)].filter(Boolean).join(' — ')).filter(Boolean).join('\n'), 'Detalhes copiados.');
  } else {
    const lines = items.map((item) => {
      const parts = [item.vd || 'VD não informado', `Status: ${item.status || 'não informado'}`];
      if (item.protocol) parts.push(`Protocolo Vivo: ${item.protocol}`);
      if (item.case_number) parts.push(`Número do caso: ${item.case_number}`);
      if (item.zabbix_host) parts.push(`Host: ${item.zabbix_host}`);
      if (item.zabbix_status) parts.push(`Zabbix: ${item.zabbix_status}`);
      if (item.message) parts.push(`Detalhe: ${clean(item.message)}`);
      if (item.zabbix_detail) parts.push(`Detalhe Zabbix: ${clean(item.zabbix_detail)}`);
      return parts.join(' — ');
    });
    copyText(lines.join('\n'), 'Frases copiadas.');
  }
}

/* ------------------------------------------------------------------ histórico */

async function loadHistory() {
  try {
    ui.history = await apiGet('/api/history');
    renderHistory();
  } catch (error) {
    toast(error.message, 'error');
  }
}

function renderHistory() {
  const data = ui.history;
  if (!data) return;
  const filter = normKey($('hist-filter').value || '');
  const match = (row) => !filter || normKey(Object.values(row).join(' ')).includes(filter);
  const windowMs = (data.janela_duplicidade_horas || 24) * 3600 * 1000;
  const now = Date.now();
  const rows = data.chamados.filter(match).map((row) => {
    const recent = now - new Date(row.data_hora).getTime() < windowMs;
    return h('tr', {},
      h('td', { class: 'mono' }, fmtDateTime(row.data_hora), recent ? h('span', { class: 'chip warn', title: 'Dentro da janela de duplicidade', text: ' RECENTE' }) : null),
      h('td', { class: 'mono', text: row.vd }),
      h('td', { text: row.loja }),
      h('td', { text: ISSUE_LABEL[row.problema] || row.problema }),
      h('td', { class: 'mono', text: row.designacao }),
      h('td', { class: 'mono', text: row.protocolo_vivo }),
      h('td', { class: 'mono', text: row.numero_caso }),
      h('td', {}, statusChip(row.status)),
      h('td', { title: row.detalhe_zabbix || '' }, row.status_zabbix ? statusChip(row.status_zabbix) : h('span', { class: 'muted', text: '—' })),
    );
  });
  $('hist-body').replaceChildren(...(rows.length ? rows : [h('tr', {}, h('td', { colspan: 9, class: 'muted', text: 'Nenhum chamado encontrado.' }))]));
  $('hist-summary').textContent = `${data.total_chamados} chamado(s) no histórico; mostrando ${rows.length}. A trava de duplicidade usa as últimas ${data.janela_duplicidade_horas}h.`;
  const zrows = data.zabbix.filter(match).map((row) => h('tr', {},
    h('td', { class: 'mono', text: fmtDateTime(row.data_hora) }),
    h('td', { class: 'mono', text: row.vd }),
    h('td', { class: 'mono', text: row.host }),
    h('td', { class: 'mono', text: row.protocolo_vivo }),
    h('td', {}, statusChip(row.status_zabbix)),
    h('td', { class: 'detail', text: row.detalhe }),
  ));
  $('zbx-body').replaceChildren(...(zrows.length ? zrows : [h('tr', {}, h('td', { colspan: 6, class: 'muted', text: 'Nenhum reconhecimento registrado.' }))]));
}

/* ------------------------------------------------------------------ configurações */

async function loadSettings() {
  try {
    ui.settings = await apiGet('/api/settings');
    renderSettings();
  } catch (error) {
    toast(error.message, 'error');
  }
}

function renderSettings() {
  const s = ui.settings;
  if (!s) return;
  const vivo = s.credenciais_vivo;
  $('vivo-status').textContent = !vivo.disponivel
    ? `Indisponível neste computador: ${vivo.erro || ''}`
    : (vivo.configurado ? `Acesso cadastrado para o CPF ${vivo.cpf}. Preencha para trocar.` : 'Nenhum acesso cadastrado. Cadastre para o robô entrar sozinho no portal.');
  show($('btn-vivo-remove'), !!vivo.configurado);
  const zbx = s.credenciais_zabbix;
  $('zbx-status').textContent = !zbx.disponivel
    ? `Indisponível neste computador: ${zbx.erro || ''}`
    : (zbx.configurado ? `Configurado${zbx.automatico ? ' · reconhecimento automático ativo' : ' · reconhecimento automático desativado'}.` : 'Não configurado. Sem isso, os incidentes não são reconhecidos automaticamente.');
  if (!$('zbx-url').value) $('zbx-url').value = zbx.api_url || s.zabbix_url_padrao;
  $('zbx-auto').checked = zbx.configurado ? !!zbx.automatico : true;
  show($('zbx-token-hint'), !!zbx.configurado);
  show($('btn-zbx-remove'), !!zbx.configurado);
  $('batch-limit').textContent = s.limite_lote;

  $('bases-body').replaceChildren(...s.bases.map((base) => h('tr', {},
    h('td', { text: base.nome }),
    h('td', { class: 'mono', text: base.arquivo || '—' }),
    h('td', {}, base.existe ? h('span', { class: 'chip ok', text: `OK · ${base.tamanho_kb} KB` }) : h('span', { class: 'chip err', text: 'NÃO ENCONTRADO' })),
    h('td', { class: 'mono', text: fmtDateTime(base.atualizado_em) }),
  )));
  fillKv($('params'), [
    ['Portal', s.portal.url],
    ['Produto esperado', s.portal.produto_esperado],
    ['Ocorrência', s.portal.ocorrencia],
    ['Navegador oculto', s.portal.navegador_oculto],
    ['Espera do protocolo', `${s.portal.tempo_protocolo_segundos} s`],
    ['Janela de duplicidade', `${s.janela_duplicidade_horas} h`],
    ['Contato', [s.contato.nome, s.contato.telefone, s.contato.email].filter(Boolean).join(' · ')],
    ['Lote', `até ${s.limite_lote} VDs · ${s.abas_paralelas} abas em paralelo`],
  ]);
}

async function onSaveVivo(event) {
  event.preventDefault();
  try {
    await api('/api/credentials/vivo', { cpf: $('vivo-cpf').value, senha: $('vivo-senha').value });
    $('vivo-senha').value = '';
    $('vivo-cpf').value = '';
    toast('Acesso salvo com segurança.');
    loadSettings();
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    $('vivo-senha').value = '';
  }
}

function onRemoveVivo() {
  openLocalModal('Remover o acesso ao Portal Vivo?', h('p', { text: 'O CPF e a senha serão apagados do Gerenciador de Credenciais deste computador, junto com a sessão do navegador do robô.' }), [
    { label: 'Cancelar' },
    { label: 'Remover', danger: true, onClick: async (close) => { close(); try { await api('/api/credentials/vivo/remove'); toast('Acesso removido.'); loadSettings(); } catch (error) { toast(error.message, 'error'); } } },
  ]);
}

async function onTestZabbix() {
  const button = $('btn-zbx-test');
  button.disabled = true;
  try {
    const data = await api('/api/credentials/zabbix/test', { api_url: $('zbx-url').value, token: $('zbx-token').value });
    toast(`Conexão e token validados. Versão do Zabbix: ${data.versao}`);
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    button.disabled = false;
  }
}

async function onSaveZabbix(event) {
  event.preventDefault();
  try {
    await api('/api/credentials/zabbix', { api_url: $('zbx-url').value, token: $('zbx-token').value, automatico: $('zbx-auto').checked });
    $('zbx-token').value = '';
    toast('Configuração do Zabbix salva.');
    loadSettings();
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    $('zbx-token').value = '';
  }
}

function onRemoveZabbix() {
  openLocalModal('Remover a integração com o Zabbix?', h('p', { text: 'O token será apagado do Gerenciador de Credenciais deste computador.' }), [
    { label: 'Cancelar' },
    { label: 'Remover', danger: true, onClick: async (close) => { close(); try { await api('/api/credentials/zabbix/remove'); toast('Integração removida.'); $('zbx-url').value = ''; loadSettings(); } catch (error) { toast(error.message, 'error'); } } },
  ]);
}

function onShutdown() {
  openLocalModal('Encerrar o robô?', h('p', { text: 'O servidor local será encerrado. Para usar de novo, abra pelo atalho.' }), [
    { label: 'Cancelar' },
    { label: 'Encerrar', danger: true, onClick: async (close) => { close(); try { await api('/api/shutdown'); } catch (error) { toast(error.message, 'error'); } } },
  ]);
}

/* ------------------------------------------------------------------ início */

function init() {
  document.querySelectorAll('.tab').forEach((tab) => tab.addEventListener('click', () => selectTab(tab.dataset.tab)));
  $('busy-banner-go').addEventListener('click', () => selectTab(ui.state.operation && ui.state.operation.kind === 'batch' ? 'lote' : 'chamado'));
  $('lookup-form').addEventListener('submit', onLookup);
  document.querySelectorAll('input[name="issue"]').forEach((input) => input.addEventListener('change', refreshPreview));
  $('btn-open-ticket').addEventListener('click', onOpenTicket);
  $('btn-cancel-ticket').addEventListener('click', onCancel);
  $('btn-new-ticket').addEventListener('click', async () => { await onClearOperation(); resetTicketForm(); $('entrada').value = ''; $('entrada').focus(); });
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-copy-from]');
    if (!button) return;
    const source = $(button.dataset.copyFrom);
    copyText(source.value !== undefined && source.tagName === 'TEXTAREA' ? source.value : source.textContent, 'Copiado.');
  });
  $('btn-batch-prepare').addEventListener('click', onBatchPrepare);
  $('btn-batch-start').addEventListener('click', onBatchStart);
  $('btn-batch-discard').addEventListener('click', () => { ui.batchReview = null; renderBatchReview(); });
  $('btn-cancel-batch').addEventListener('click', onCancel);
  $('btn-batch-new').addEventListener('click', async () => { batchSelection.clear(); await onClearOperation(); });
  $('batch-select-all').addEventListener('change', (event) => {
    const op = ui.state.operation;
    batchSelection.clear();
    if (event.target.checked && op && op.kind === 'batch') op.items.forEach((item) => batchSelection.add(String(item.position)));
    renderBatchOperation();
  });
  document.querySelectorAll('[data-batch-copy]').forEach((button) => button.addEventListener('click', () => onBatchCopy(button.dataset.batchCopy)));
  $('hist-filter').addEventListener('input', renderHistory);
  $('btn-hist-refresh').addEventListener('click', loadHistory);
  $('vivo-form').addEventListener('submit', onSaveVivo);
  $('btn-vivo-remove').addEventListener('click', onRemoveVivo);
  $('zbx-form').addEventListener('submit', onSaveZabbix);
  $('btn-zbx-test').addEventListener('click', onTestZabbix);
  $('btn-zbx-remove').addEventListener('click', onRemoveZabbix);
  $('btn-shutdown').addEventListener('click', onShutdown);
  // Esc não deve fechar uma confirmação do worker sem resposta.
  $('modal').addEventListener('cancel', (event) => {
    if (ui.modalPromptId) event.preventDefault();
    else { ui.modalLocal = false; setTimeout(renderPrompts, 0); }
  });
  connect();
  loadSettings();
  $('entrada').focus();
}

document.addEventListener('DOMContentLoaded', init);
