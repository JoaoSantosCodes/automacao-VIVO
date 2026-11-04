#!/usr/bin/env node

/**
 * Worker de automacao do Portal Vivo Empresas.
 *
 * Protocolo: JSON Lines (um objeto JSON por linha) em stdin/stdout.
 * A senha chega somente pela memoria do processo, e e removida do ambiente
 * antes de o Chrome ser iniciado. Ela nunca e gravada nem impressa.
 */

import { createRequire } from 'node:module';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import readline from 'node:readline';

const require = createRequire(import.meta.url);

// A raiz publica redireciona para /oauth?logout=true. O worker normal sempre
// inicia no dashboard para preservar a sessao; apenas --auth usa a raiz.
const DEFAULT_LOGIN_URL = 'https://mve.vivo.com.br/oauth?logout=true';
const DEFAULT_PORTAL_URL = 'https://mve.vivo.com.br/sec/dashboard';
const DEFAULT_SERVICE_NOW_URL = 'https://vivob2b.service-now.com/portalvivoempresas';
const ACTIVE_SERVICES_QUERY = 'id=meus_servicos_ativos&table=sn_install_base_item&view=portal&filter=state%3Din_use';
// Identificadores do fluxo "Abrir Chamado Tecnico" para VPN IP MPLS. O
// deployed_entity_sys_id continua sendo lido do circuito escolhido em tempo
// real; estes dois valores apenas identificam o formulario e o produto Vivo.
const TECHNICAL_HELP_SYS_ID = 'a656630e1b2ec154b423dd70f54bcb04';
const VPN_MPLS_SOLD_PRODUCT_SYS_ID = '0329cce21baac59407fb415e54bcb91';
const RESPONSE_TIMEOUT_MS = 60 * 60 * 1000;
const DEFAULT_PROTOCOL_TIMEOUT_MS = 300 * 1000;
const NAVIGATION_TIMEOUT_MS = 90 * 1000;
const DIAGNOSTIC_RETENTION_DAYS = 14;
const ERROR_ARTIFACT_RETENTION_DAYS = 7;
const PUBLIC_PORTAL_ROUTE_IDS = new Set([
  'nova_solicitacao',
  'meus_servicos_ativos',
  'csm_get_help',
  'pv_detail',
  'csm_ticket',
]);

let activePage = null;
const registrationValidationWaitedPages = new WeakSet();
let navigationLogPath = null;
let nextPageId = 1;
const pageIds = new WeakMap();
const observedPages = new WeakSet();

function cleanString(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/\u00a0/g, ' ').trim();
}

function normalize(value) {
  return cleanString(value)
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^A-Za-z0-9]+/g, ' ')
    .trim()
    .toUpperCase();
}

function digits(value) {
  return cleanString(value).replace(/\D/g, '');
}

function takeCredentialsFromEnvironment() {
  const cpf = digits(process.env.ROBO_VIVO_CPF ?? '');
  const password = String(process.env.ROBO_VIVO_PASSWORD ?? '');
  delete process.env.ROBO_VIVO_CPF;
  delete process.env.ROBO_VIVO_PASSWORD;
  if (!cpf && !password) return null;
  if (cpf.length !== 11 || !password) {
    throw new Error('O CPF ou a senha do acesso automatico estao incompletos. Atualize o acesso no robo.');
  }
  return { cpf, password };
}

function boolValue(value, fallback = false) {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  const text = normalize(value);
  if (['SIM', 'YES', 'TRUE', '1', 'CONFIRMAR', 'CONFIRMADO'].includes(text)) return true;
  if (['NAO', 'NO', 'FALSE', '0', 'CANCELAR', 'CANCELADO'].includes(text)) return false;
  return fallback;
}

function withoutSecrets(value) {
  if (Array.isArray(value)) return value.map(withoutSecrets);
  if (!value || typeof value !== 'object') return value;
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    if (/(senha|password|passwd|secret|token|cookie|authorization|cpf|credential|session|otp|pin|code|assertion|relaystate)/i.test(key)) continue;
    result[key] = withoutSecrets(item);
  }
  return result;
}

function redactDiagnosticText(value) {
  return cleanString(value)
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, '<email-oculto>')
    .replace(/\b\d{11}\b/g, '<numero-oculto>')
    .replace(/\b(?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?9?\d{4}[-\s]?\d{4}\b/g, '<telefone-oculto>')
    .replace(/([?&](?:token|code|state|session|auth|cpf|samlresponse|relaystate)\s*=)[^&\s]+/gi, '$1<oculto>')
    .replace(/\b(authorization|cookie|senha|password|token|samlresponse|relaystate)\s*[:=]\s*[^\s,;]+/gi, '$1=<oculto>')
    .slice(0, 1_000);
}

function hasForbiddenSecretKey(value) {
  if (!value || typeof value !== 'object') return false;
  if (Array.isArray(value)) return value.some(hasForbiddenSecretKey);
  return Object.entries(value).some(([key, item]) =>
    /(senha|password|passwd)/i.test(key) || hasForbiddenSecretKey(item),
  );
}

function emit(type, payload = {}) {
  const message = withoutSecrets({ type, event: type, ...payload });
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function emitStatus(message, extra = {}) {
  emit('status', { message, ...extra });
}

function debug(message) {
  if (process.env.ROBO_VIVO_DEBUG === '1') process.stderr.write(`[vivo-worker] ${message}\n`);
}

class JsonLineBus {
  constructor(input = process.stdin) {
    this.queue = [];
    this.waiters = [];
    this.closed = false;
    this.rl = readline.createInterface({ input, crlfDelay: Infinity });
    this.rl.on('line', (line) => this.#receive(line));
    this.rl.on('close', () => {
      this.closed = true;
      for (const waiter of this.waiters.splice(0)) {
        clearTimeout(waiter.timer);
        waiter.reject(new Error('A entrada do worker foi encerrada.'));
      }
    });
  }

  #receive(line) {
    const text = cleanString(line);
    if (!text) return;
    let message;
    try {
      message = JSON.parse(text);
    } catch {
      debug('Linha ignorada: JSON invalido recebido em stdin.');
      return;
    }
    for (let index = 0; index < this.waiters.length; index += 1) {
      const waiter = this.waiters[index];
      if (!waiter.predicate(message)) continue;
      this.waiters.splice(index, 1);
      clearTimeout(waiter.timer);
      waiter.resolve(message);
      return;
    }
    this.queue.push(message);
  }

  waitFor(predicate, timeoutMs = RESPONSE_TIMEOUT_MS) {
    const queuedIndex = this.queue.findIndex(predicate);
    if (queuedIndex >= 0) return Promise.resolve(this.queue.splice(queuedIndex, 1)[0]);
    if (this.closed) return Promise.reject(new Error('A entrada do worker esta fechada.'));
    return new Promise((resolve, reject) => {
      const waiter = { predicate, resolve, reject, timer: null };
      waiter.timer = setTimeout(() => {
        const index = this.waiters.indexOf(waiter);
        if (index >= 0) this.waiters.splice(index, 1);
        reject(new Error('Tempo esgotado aguardando confirmacao do usuario.'));
      }, timeoutMs);
      this.waiters.push(waiter);
    });
  }

  close() {
    this.rl.close();
  }
}

function argValue(args, name) {
  const index = args.indexOf(name);
  if (index < 0) return null;
  return args[index + 1] ?? null;
}

async function parseJsonRequest(text) {
  if (!text) throw new Error('Informe o JSON do chamado apos --open-ticket.');
  if (text.startsWith('@')) {
    return JSON.parse(await fsp.readFile(path.resolve(text.slice(1)), 'utf8'));
  }
  if (fs.existsSync(text) && fs.statSync(text).isFile()) {
    return JSON.parse(await fsp.readFile(path.resolve(text), 'utf8'));
  }
  return JSON.parse(text);
}

function firstNonEmpty(...values) {
  return values.map(cleanString).find(Boolean) ?? '';
}

function listStrings(...values) {
  const result = [];
  for (const value of values) {
    const items = Array.isArray(value) ? value : [value];
    for (const item of items) {
      const text = cleanString(item);
      if (text) result.push(text);
    }
  }
  return result;
}

function boundedSeconds(value, fallbackSeconds = DEFAULT_PROTOCOL_TIMEOUT_MS / 1000) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return fallbackSeconds;
  return Math.min(Math.max(Math.round(parsed), 30), 900);
}

function normalizeIssue(value) {
  const issue = normalize(value);
  if (/PACOTE/.test(issue)) return 'perca_pacotes';
  if (/INOPER|INTERROMP|INDISPON|LINK/.test(issue)) return 'link_interrompido';
  return '';
}

function normalizeTime(value, fallback) {
  const text = cleanString(value);
  const match = text.match(/\b([01]?\d|2[0-3])[:hH]?([0-5]\d)?\b/);
  if (!match) return fallback;
  return `${match[1].padStart(2, '0')}:${(match[2] ?? '00').padStart(2, '0')}`;
}

function timeRange(value) {
  const matches = [...cleanString(value).matchAll(/\b([01]?\d|2[0-3])(?:[:hH]([0-5]\d))?\b/g)];
  return matches.slice(0, 2).map((match) =>
    `${match[1].padStart(2, '0')}:${(match[2] ?? '00').padStart(2, '0')}`,
  );
}

function looksLike24Hours(...values) {
  const text = normalize(values.filter(Boolean).join(' '));
  return /(^| )24( H| HORAS|H|$)/.test(text) || /00 00.*23 59/.test(text);
}

function addressKeyword(address) {
  const ignored = new Set([
    'RUA', 'AV', 'AVENIDA', 'ALAMEDA', 'ESTRADA', 'RODOVIA', 'TRAVESSA', 'PRACA',
    'LARGO', 'VIA', 'DAS', 'DOS', 'DA', 'DO', 'DE', 'E', 'N', 'NUMERO', 'KM',
    'FUNDOS', 'LOJA', 'BLOCO', 'SALA', 'CEP', 'CENTRO',
  ]);
  const tokens = normalize(address)
    .split(' ')
    .filter((token) => token.length >= 3 && !/^\d+$/.test(token) && !ignored.has(token));
  if (!tokens.length) return '';
  // A primeira palavra distintiva costuma ser a forma mais previsivel de
  // consultar o filtro do ServiceNow (ex.: RUA PARANA, 121 FUNDOS -> PARANA).
  return tokens[0];
}

function addressSearchTerms(address, providedKeywords = [], cep = '') {
  const tokens = normalize(address).split(' ').filter(Boolean);
  const typeExpansion = new Map([
    ['AV', 'AVENIDA'], ['R', 'RUA'], ['AL', 'ALAMEDA'], ['EST', 'ESTRADA'],
    ['ROD', 'RODOVIA'], ['TRAV', 'TRAVESSA'], ['PC', 'PRACA'],
  ]);
  const suffixMarkers = new Set([
    'LOJA', 'BLOCO', 'SALA', 'FUNDOS', 'CEP', 'KM', 'APTO', 'APARTAMENTO',
  ]);
  const weakTokens = new Set([
    'AV', 'AVENIDA', 'R', 'RUA', 'AL', 'ALAMEDA', 'EST', 'ESTRADA', 'ROD',
    'RODOVIA', 'TRAV', 'TRAVESSA', 'PC', 'PRACA', 'DAS', 'DOS', 'DA', 'DO',
    'DE', 'E', 'LOJA', 'BLOCO', 'SALA', 'FUNDOS', 'CEP', 'KM', 'CENTRO',
  ]);
  let streetEnd = tokens.findIndex((token, index) => index > 0 && (/^\d+$/.test(token) || suffixMarkers.has(token)));
  if (streetEnd < 0) streetEnd = Math.min(tokens.length, 6);
  const street = tokens.slice(0, streetEnd);
  const expandedStreet = street.map((token, index) => index === 0 && typeExpansion.has(token)
    ? typeExpansion.get(token)
    : token);
  const withoutType = street.filter((token, index) => !(index === 0 && (typeExpansion.has(token)
    || [...typeExpansion.values()].includes(token))));
  const distinctive = tokens.filter((token) => token.length >= 3 && !/^\d+$/.test(token) && !weakTokens.has(token));
  const number = tokens.find((token) => /^\d+$/.test(token)) || '';
  const supplied = listStrings(providedKeywords)
    .map(normalize)
    .filter((token) => token && !weakTokens.has(token));
  const terms = [
    expandedStreet.join(' '),
    street.join(' '),
    withoutType.join(' '),
    distinctive[0],
    number,
    distinctive.slice(0, 3).join(' '),
    ...distinctive,
    ...supplied,
    digits(cep),
  ];
  return [...new Set(terms.map(cleanString).filter(Boolean))].slice(0, 12);
}

function composeAddress(parts) {
  return parts.map(cleanString).filter(Boolean).join(', ');
}

function buildDescription(ticket) {
  const issueSentence = ticket.problem === 'perca_pacotes'
    ? 'Se encontra com perca de pacotes.'
    : 'Se encontra com o link interrompido.';
  return [
    'Prezados,',
    '',
    `Favor verificar a filial: ${ticket.storeName}`,
    '',
    issueSentence,
    '',
    `Filial: ${ticket.vd} - ${ticket.storeName}`,
    '',
    'Reset: Sim',
    '',
    `Designação: ${ticket.designation}`,
    '',
    `Horário Funcionamento: ${ticket.businessHours}`,
    '',
    'OBS: Para o envio de um técnico à loja, é necessário encaminhar um e-mail para central.comando@dpsp.com.br',
    ', contendo os dados do técnico e o VD da loja, conforme informado na descrição do chamado.',
    '',
    'Contato para validações:',
    '',
    'WhatsApp: (11) 99111-8142 (apenas mensagens)',
    'Telefone: (11) 3274-7527',
    '',
    'Grato!',
  ].join('\n');
}

function normalizeRequest(rawRequest) {
  if (!rawRequest || typeof rawRequest !== 'object' || Array.isArray(rawRequest)) {
    throw new Error('O chamado precisa ser um objeto JSON.');
  }
  if (hasForbiddenSecretKey(rawRequest)) {
    throw new Error('Nao envie senha ao worker. Use o modo --auth e o perfil seguro do Chrome.');
  }

  const source = rawRequest.data && typeof rawRequest.data === 'object'
    ? { ...rawRequest, ...rawRequest.data }
    : rawRequest;
  const store = source.loja && typeof source.loja === 'object' ? source.loja : {};
  const circuit = source.circuito && typeof source.circuito === 'object'
    ? source.circuito
    : (source.link && typeof source.link === 'object' ? source.link : {});
  const contact = source.contact && typeof source.contact === 'object'
    ? source.contact
    : (source.contato && typeof source.contato === 'object' ? source.contato : {});

  const vd = firstNonEmpty(source.vd, store.vd, store.codigo);
  const storeName = firstNonEmpty(source.nome_loja, store.nome, source.nome);
  const designation = firstNonEmpty(
    source.designacao,
    circuit.designacao,
    circuit.identificacao,
    circuit.usuario,
  );
  const identification = firstNonEmpty(
    source.identificacao,
    source.designacao,
    circuit.identificacao,
    circuit.designacao,
    circuit.usuario,
  );
  const serviceId = firstNonEmpty(source.id_servico, source.service_id, circuit.id_servico, circuit.id);
  const address = firstNonEmpty(
    source.endereco_completo,
    source.endereco,
    circuit.endereco,
    store.endereco_completo,
    store.endereco,
  ) || composeAddress([store.endereco, source.bairro ?? store.bairro]);
  const city = firstNonEmpty(source.cidade, circuit.cidade, store.cidade);
  const state = firstNonEmpty(source.uf, store.uf);
  const cep = firstNonEmpty(source.cep, store.cep);
  const hoursScript = firstNonEmpty(
    source.horario_script,
    store.horario_script,
    source.horario_funcionamento,
    store.horario_funcionamento,
  );
  const is24Hours = boolValue(
    source.vinte_quatro_horas ?? store.vinte_quatro_horas,
    looksLike24Hours(hoursScript, source.hora_inicio, source.hora_fim, store.horario_inicio, store.horario_termino),
  );
  const hoursFromScript = timeRange(hoursScript);
  const startTime = is24Hours
    ? '00:00'
    : normalizeTime(
        firstNonEmpty(source.hora_inicio, store.hora_inicio, store.horario_inicio, hoursFromScript[0]),
        '07:00',
      );
  const endTime = is24Hours
    ? '23:59'
    : normalizeTime(
        firstNonEmpty(source.hora_fim, store.hora_fim, store.horario_termino, hoursFromScript[1]),
        '22:00',
      );
  const businessHours = is24Hours
    ? '24 HORAS'
    : firstNonEmpty(hoursScript, `${startTime} ÀS ${endTime}`);
  const problem = normalizeIssue(firstNonEmpty(source.problema, source.problem, source.issue));

  const ticket = {
    requestId: firstNonEmpty(source.request_id, rawRequest.request_id) || `vivo-${Date.now()}`,
    vd,
    storeName,
    address,
    city,
    state,
    cep,
    designation,
    identification,
    serviceId,
    product: firstNonEmpty(source.produto, circuit.produto, circuit.tipo_acesso, 'VPN IP MPLS'),
    productFilter: firstNonEmpty(source.produto_filtro, 'mpls'),
    expectedProduct: firstNonEmpty(source.produto_esperado, 'VPN IP MPLS'),
    occurrence: firstNonEmpty(source.ocorrencia, 'Indisponibilidade'),
    problem,
    startTime,
    endTime,
    businessHours,
    is24Hours,
    contactName: firstNonEmpty(contact.nome, contact.name, source.nome_contato, 'central de comando'),
    contactPhone: digits(firstNonEmpty(contact.telefone, contact.phone, source.telefone_contato, '1132747527')),
    contactEmail: firstNonEmpty(contact.email, source.email_contato, 'central.comando@dpsp.com.br'),
    sharePhone: firstNonEmpty(contact.compartilhar_celular, source.compartilhar_celular, 'Sim'),
    trainingRequired: firstNonEmpty(
      contact.capacitacao_documentacao,
      source.capacitacao_documentacao,
      'Sim',
    ),
    allowSubmit: boolValue(source.allow_submit ?? rawRequest.allow_submit, true),
    headless: boolValue(source.navegador_oculto, true),
    protocolTimeoutMs: boundedSeconds(source.tempo_protocolo_segundos) * 1000,
    portalUrl: firstNonEmpty(source.portal_url, source.portalUrl, DEFAULT_PORTAL_URL),
    raw: withoutSecrets(source),
  };
  ticket.description = firstNonEmpty(source.descricao, source.description) || buildDescription(ticket);
  const streetAddress = firstNonEmpty(source.endereco, store.endereco, address);
  const addressNumbers = normalize(streetAddress)
    .split(' ')
    .filter((item) => /^\d+$/.test(item))
    .map((item) => String(Number.parseInt(item, 10)));
  ticket.searchKeywords = addressSearchTerms(
    streetAddress,
    listStrings(source.palavras_chave_endereco, store.palavras_chave_endereco, addressNumbers),
    cep,
  );
  ticket.searchKeyword = addressKeyword(streetAddress) || ticket.searchKeywords[0] || '';

  const missing = [];
  if (!ticket.vd) missing.push('VD');
  if (!ticket.storeName) missing.push('nome da loja');
  if (!ticket.designation) missing.push('designacao');
  if (!ticket.problem) missing.push('problema (link interrompido ou perca de pacotes)');
  if (!ticket.identification && !ticket.serviceId && !ticket.searchKeyword) {
    missing.push('identificacao, ID do servico ou endereco pesquisavel');
  }
  if (!ticket.contactPhone) missing.push('telefone de contato');
  if (!ticket.description) missing.push('descricao');
  if (missing.length) throw new Error(`Dados obrigatorios ausentes: ${missing.join(', ')}.`);
  if (ticket.description.length > 1000) {
    throw new Error(`A descricao possui ${ticket.description.length} caracteres; o portal aceita no maximo 1000.`);
  }
  return ticket;
}

function loadPlaywright() {
  try {
    return require('playwright');
  } catch (error) {
    throw new Error(`Playwright nao esta instalado ou acessivel: ${error.message}`);
  }
}

function localPaths() {
  const localAppData = cleanString(process.env.LOCALAPPDATA);
  if (!localAppData) throw new Error('A variavel LOCALAPPDATA nao esta disponivel neste Windows.');
  const root = path.join(localAppData, 'RoboVivo');
  return {
    root,
    profile: path.join(root, 'ChromeProfile'),
    errors: path.join(root, 'erros'),
    logs: path.join(root, 'logs'),
    temp: path.join(root, 'temp'),
  };
}

function isServiceNowPortalUrl(value) {
  try {
    const parsed = new URL(cleanString(value));
    return parsed.hostname.toLowerCase() === 'vivob2b.service-now.com'
      && parsed.pathname.toLowerCase().startsWith('/portalvivoempresas');
  } catch {
    return false;
  }
}

function safeUrl(value) {
  try {
    const parsed = new URL(cleanString(value));
    const query = [];
    const keys = [...new Set([...parsed.searchParams.keys()])].sort();
    for (const key of keys) {
      const valueForKey = parsed.searchParams.get(key) || '';
      const publicValue = key.toLowerCase() === 'id' && PUBLIC_PORTAL_ROUTE_IDS.has(valueForKey)
        ? valueForKey
        : '<oculto>';
      query.push(`${encodeURIComponent(key)}=${publicValue}`);
    }
    return `${parsed.protocol}//${parsed.host}${parsed.pathname}${query.length ? `?${query.join('&')}` : ''}`;
  } catch {
    return '<url-invalida>';
  }
}

async function pruneDiagnosticFiles(directory, prefix, maxAgeDays = DIAGNOSTIC_RETENTION_DAYS) {
  const cutoff = Date.now() - (maxAgeDays * 24 * 60 * 60 * 1000);
  const entries = await fsp.readdir(directory, { withFileTypes: true }).catch(() => []);
  await Promise.all(entries
    .filter((entry) => entry.isFile() && entry.name.startsWith(prefix))
    .map(async (entry) => {
      const target = path.join(directory, entry.name);
      const info = await fsp.stat(target).catch(() => null);
      if (info && info.mtimeMs < cutoff) await fsp.unlink(target).catch(() => {});
    }));
}

async function initializeNavigationLog(requestId) {
  const locations = localPaths();
  await fsp.mkdir(locations.logs, { recursive: true });
  await pruneDiagnosticFiles(locations.logs, 'navegacao-');
  await fsp.mkdir(locations.errors, { recursive: true });
  await pruneDiagnosticFiles(locations.errors, 'erro-', ERROR_ARTIFACT_RETENTION_DAYS);
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const runId = randomUUID().slice(0, 8);
  navigationLogPath = path.join(locations.logs, `navegacao-${stamp}-${runId}.txt`);
  await fsp.writeFile(navigationLogPath, '', 'utf8');
  await navigationLog('inicio', { run_id: runId, request_present: Boolean(cleanString(requestId)) });
  return navigationLogPath;
}

async function navigationLog(step, data = {}) {
  if (!navigationLogPath) return;
  const safeData = withoutSecrets(data);
  for (const key of ['url', 'from', 'to', 'destination']) {
    if (safeData[key]) safeData[key] = safeUrl(safeData[key]);
  }
  if (Array.isArray(safeData.pages)) {
    safeData.pages = safeData.pages.map((item) => ({
      index: item.index,
      page_id: item.page_id,
      opener_page_id: item.opener_page_id,
      url: safeUrl(item.url),
      closed: Boolean(item.closed),
    }));
  }
  const line = `${new Date().toISOString()} | ${step} | ${JSON.stringify(safeData)}\n`;
  await fsp.appendFile(navigationLogPath, line, 'utf8').catch(() => {});
}

async function pageSummary(page, index = 0) {
  const opener = await page.opener().catch(() => null);
  return {
    index,
    page_id: pageId(page),
    opener_page_id: opener ? pageId(opener) : null,
    url: safeUrl(page.url()),
    closed: page.isClosed(),
  };
}

function pageId(page) {
  if (!pageIds.has(page)) pageIds.set(page, nextPageId++);
  return pageIds.get(page);
}

function observePage(page) {
  if (!page || observedPages.has(page)) return;
  observedPages.add(page);
  const id = pageId(page);
  void page.opener().then((opener) => navigationLog('aba_aberta', {
    page_id: id,
    opener_page_id: opener ? pageId(opener) : null,
    url: page.url(),
  })).catch(() => {});
  page.on('framenavigated', (frame) => {
    if (frame !== page.mainFrame()) return;
    void navigationLog('navegacao_aba', { page_id: id, url: frame.url() }).catch(() => {});
  });
  page.on('close', () => {
    void navigationLog('aba_fechada', { page_id: id }).catch(() => {});
  });
}

function observeContext(context) {
  context.pages().forEach(observePage);
  context.on('page', observePage);
}

async function logOpenPages(context, step, selectedPage = null) {
  const pages = context.pages();
  const summaries = await Promise.all(pages.map((item, index) => pageSummary(item, index)));
  await navigationLog(step, {
    selected_index: selectedPage ? pages.indexOf(selectedPage) : -1,
    pages: summaries,
  });
}

async function launchChrome({ headless }) {
  const locations = localPaths();
  // Em sessoes RDP, o Windows pode manter TEMP apontando para uma subpasta
  // numerada que ja foi removida (por exemplo, Temp\2). O Playwright tenta
  // criar seus artefatos antes de abrir o Chrome e falha com ENOENT. Usamos
  // uma pasta propria e permanente do robo para eliminar essa dependencia.
  await fsp.mkdir(locations.temp, { recursive: true });
  process.env.TEMP = locations.temp;
  process.env.TMP = locations.temp;
  process.env.TMPDIR = locations.temp;
  const { chromium } = loadPlaywright();
  await fsp.mkdir(locations.profile, { recursive: true });
  await navigationLog('ambiente_temporario_preparado', { temp_ready: true });
  const chromeExecutable = cleanString(process.env.ROBO_CHROME_EXE);
  // A pagina inicial da Vivo nao renderiza todos os componentes do seletor de
  // servicos de forma confiavel no modo headless. Usamos o Chrome normal fora
  // da area visivel da tela: o usuario continua sem ver o navegador, mas a
  // aplicacao recebe exatamente o mesmo DOM de uma janela comum.
  const offscreenWindow = headless && process.env.ROBO_VIVO_TRUE_HEADLESS !== '1';
  const browserOptions = {
    headless: offscreenWindow ? false : headless,
    viewport: { width: 1440, height: 900 },
    locale: 'pt-BR',
    timezoneId: 'America/Sao_Paulo',
    acceptDownloads: false,
    args: [
      '--disable-notifications',
      ...(offscreenWindow ? ['--window-position=-32000,-32000', '--window-size=1440,900'] : []),
    ],
  };
  if (chromeExecutable) browserOptions.executablePath = chromeExecutable;
  else browserOptions.channel = 'chrome';
  const context = await chromium.launchPersistentContext(locations.profile, browserOptions);
  observeContext(context);
  context.setDefaultTimeout(20_000);
  context.setDefaultNavigationTimeout(NAVIGATION_TIMEOUT_MS);
  const page = context.pages()[0] ?? await context.newPage();
  activePage = page;
  return { context, page };
}

async function isVisible(locator) {
  try {
    return await locator.first().isVisible();
  } catch {
    return false;
  }
}

async function firstVisible(locators) {
  for (const locator of locators) {
    const count = Math.min(await locator.count().catch(() => 0), 20);
    for (let index = 0; index < count; index += 1) {
      const candidate = locator.nth(index);
      if (await isVisible(candidate)) return candidate;
    }
  }
  return null;
}

function textRegex(text) {
  return text instanceof RegExp ? text : new RegExp(cleanString(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i');
}

async function locatorForText(page, names, roles = ['button', 'link']) {
  const scopes = [page, ...page.frames().filter((frame) => frame !== page.mainFrame())];
  for (const scope of scopes) {
    for (const name of names) {
      const regex = textRegex(name);
      const locators = roles.map((role) => scope.getByRole(role, { name: regex }));
      locators.push(scope.getByText(regex, { exact: false }));
      const found = await firstVisible(locators);
      if (found) return found;
    }
  }
  return null;
}

async function clickText(page, names, options = {}) {
  const optional = options.optional ?? false;
  const timeout = options.timeout ?? (optional ? 1_500 : 12_000);
  const deadline = Date.now() + timeout;
  do {
    const locator = await locatorForText(page, names);
    if (locator) {
      await locator.click({ timeout: Math.max(500, deadline - Date.now()) });
      return true;
    }
    if (Date.now() < deadline) await page.waitForTimeout(250);
  } while (Date.now() < deadline);
  if (optional) return false;
  throw new Error(`Nao encontrei o controle: ${names.join(' / ')}.`);
}

async function clickAndFollow(page, context, names, options = {}) {
  const before = new Set(context.pages());
  const followTimeout = options.followTimeout ?? 45_000;
  const clickOptions = { ...options };
  delete clickOptions.followTimeout;
  await logOpenPages(context, 'antes_click_redirecionamento', page);
  await clickText(page, names, clickOptions);
  const popup = await waitForPortalPage(context, before, page, followTimeout);
  if (popup && popup !== page) return popup;
  await page.waitForLoadState('domcontentloaded', { timeout: 10_000 }).catch(() => {});
  await logOpenPages(context, 'apos_click_redirecionamento_sem_popup', page);
  return page;
}

async function pageWithTextControl(context, names) {
  const pages = context.pages().filter((item) => !item.isClosed()).reverse();
  for (const candidate of pages) {
    const control = await locatorForText(candidate, names).catch(() => null);
    if (control) {
      await navigationLog('pagina_com_controle_encontrada', {
        url: candidate.url(),
        control: names.map(String).join(' / '),
      });
      return candidate;
    }
  }
  return null;
}

async function waitForPortalPage(context, pagesBefore, currentPage, timeout = 20_000, preferredPage = null) {
  const deadline = Date.now() + timeout;
  let bestNewPage = null;
  let lastFingerprint = '';
  while (Date.now() < deadline) {
    const pages = context.pages().filter((item) => !item.isClosed());
    pages.forEach(observePage);
    const fingerprint = pages.map((item) => `${pageId(item)}:${safeUrl(item.url())}`).join('|');
    if (fingerprint !== lastFingerprint) {
      lastFingerprint = fingerprint;
      await logOpenPages(context, 'mudanca_nas_abas', preferredPage || currentPage);
    }
    let best = null;
    let bestScore = -1;
    for (let index = 0; index < pages.length; index += 1) {
      const candidate = pages[index];
      const url = candidate.url();
      const isNew = !pagesBefore.has(candidate);
      if (!isNew && candidate !== currentPage && candidate !== preferredPage) continue;
      const title = cleanString(await candidate.title().catch(() => ''));
      let score = 0;
      if (isServiceNowPortalUrl(url)) score += 1_000;
      if (candidate === preferredPage) score += 200;
      if (isNew) score += 50;
      if (/portal de servi[cç]os|central de servi[cç]os/i.test(title)) score += 30;
      if (candidate !== currentPage) score += 10;
      if (!/^about:blank$/i.test(url)) score += 5;
      if ((candidate === preferredPage || isNew) && !/^about:blank$/i.test(url)) bestNewPage = candidate;
      if (score > bestScore) {
        best = candidate;
        bestScore = score;
      }
    }
    if (best && isServiceNowPortalUrl(best.url())) {
      await best.waitForLoadState('domcontentloaded', { timeout: NAVIGATION_TIMEOUT_MS }).catch(() => {});
      await best.bringToFront().catch(() => {});
      activePage = best;
      await logOpenPages(context, 'aba_servicenow_selecionada', best);
      return best;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  if (bestNewPage) {
    await bestNewPage.waitForLoadState('domcontentloaded', { timeout: 10_000 }).catch(() => {});
    await bestNewPage.bringToFront().catch(() => {});
    activePage = bestNewPage;
    await logOpenPages(context, 'nova_aba_selecionada_sem_dominio_confirmado', bestNewPage);
    return bestNewPage;
  }
  await logOpenPages(context, 'nenhuma_nova_aba_detectada', currentPage);
  return currentPage;
}

async function clickExactTextInDom(page, wantedText) {
  const scopes = [page.mainFrame(), ...page.frames().filter((frame) => frame !== page.mainFrame())];
  for (const scope of scopes) {
    const clicked = await scope.evaluate((wanted) => {
      const normalized = (value) => String(value ?? '')
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .replace(/\s+/g, ' ')
        .trim()
        .toUpperCase();
      const expected = normalized(wanted);
      const elements = [];
      const visit = (root) => {
        for (const element of root.querySelectorAll('*')) {
          elements.push(element);
          if (element.shadowRoot) visit(element.shadowRoot);
        }
      };
      visit(document);
      const candidates = elements
        .filter((element) => {
          const rect = element.getBoundingClientRect();
          if (rect.width < 2 || rect.height < 2) return false;
          const style = getComputedStyle(element);
          if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
          return normalized(element.innerText || element.textContent) === expected;
        })
        .sort((left, right) => {
          const a = left.getBoundingClientRect();
          const b = right.getBoundingClientRect();
          return (a.width * a.height) - (b.width * b.height);
        });
      if (!candidates.length) return false;
      const element = candidates[0];
      let target = element;
      let current = element;
      for (let level = 0; current && level < 8; level += 1) {
        const interactive = current.matches?.(
          'button, a, [role="button"], [role="option"], [role="menuitem"], [tabindex], [onclick]',
        ) || getComputedStyle(current).cursor === 'pointer';
        if (interactive) {
          target = current;
          break;
        }
        const root = current.getRootNode?.();
        current = current.parentElement || root?.host || null;
      }
      target.scrollIntoView({ block: 'center', inline: 'center' });
      const options = { bubbles: true, cancelable: true, view: window };
      target.dispatchEvent(new MouseEvent('mousedown', options));
      target.dispatchEvent(new MouseEvent('mouseup', options));
      target.click();
      return true;
    }, wantedText).catch(() => false);
    if (clicked) return true;
  }
  return false;
}

async function clickRenderedText(page, wantedText) {
  // Localiza o texto que o Chrome efetivamente desenhou, obtendo o retangulo
  // da selecao, e produz um clique de mouse confiavel nesse ponto. Funciona
  // mesmo quando o card e apenas uma sequencia de divs sem role/href.
  const scopes = [page.mainFrame(), ...page.frames().filter((frame) => frame !== page.mainFrame())];
  for (const scope of scopes) {
    const point = await scope.evaluate((wanted) => {
      const visibleRect = (rect) => rect && rect.width > 1 && rect.height > 1
        && rect.bottom >= 0 && rect.right >= 0
        && rect.top <= innerHeight && rect.left <= innerWidth;
      const selection = getSelection();
      selection?.removeAllRanges();
      if (typeof window.find === 'function' && window.find(wanted, false, false, true, false, false, false)) {
        const range = selection?.rangeCount ? selection.getRangeAt(0) : null;
        const rects = range ? [...range.getClientRects()].filter(visibleRect) : [];
        if (rects.length) {
          const rect = rects[0];
          selection.removeAllRanges();
          return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
        }
      }
      selection?.removeAllRanges();
      const normalized = (value) => String(value ?? '')
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .replace(/\s+/g, ' ')
        .trim()
        .toUpperCase();
      const expected = normalized(wanted);
      const elements = [...document.querySelectorAll('body *')]
        .filter((element) => normalized(element.innerText || element.textContent) === expected)
        .map((element) => ({ element, rect: element.getBoundingClientRect() }))
        .filter(({ element, rect }) => {
          const style = getComputedStyle(element);
          return visibleRect(rect) && style.display !== 'none' && style.visibility !== 'hidden';
        })
        .sort((left, right) => (left.rect.width * left.rect.height) - (right.rect.width * right.rect.height));
      if (!elements.length) return null;
      const rect = elements[0].rect;
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    }, wantedText).catch(() => null);
    if (!point) continue;

    let x = point.x;
    let y = point.y;
    if (scope !== page.mainFrame()) {
      const frameElement = await scope.frameElement().catch(() => null);
      const frameBox = frameElement ? await frameElement.boundingBox().catch(() => null) : null;
      if (!frameBox) continue;
      x += frameBox.x;
      y += frameBox.y;
    }
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.up();
    await page.waitForTimeout(600);
    return true;
  }
  return false;
}

async function clickRenderedTextWhenReady(page, wantedText, timeout = 12_000) {
  const deadline = Date.now() + timeout;
  do {
    if (await clickRenderedText(page, wantedText)) return true;
    if (Date.now() < deadline) await page.waitForTimeout(300);
  } while (Date.now() < deadline);
  return false;
}

async function clickTextRobust(page, names, exactFallback, { optional = false } = {}) {
  if (await clickText(page, names, { optional: true, timeout: 4_000 })) return true;
  if (exactFallback && await clickExactTextInDom(page, exactFallback)) {
    await page.waitForTimeout(500);
    return true;
  }
  if (optional) return false;
  throw new Error(`Nao encontrei o controle: ${names.join(' / ')}.`);
}

async function servicePortalDestination(page) {
  const scopes = [page.mainFrame(), ...page.frames().filter((frame) => frame !== page.mainFrame())];
  for (const scope of scopes) {
    const destination = await scope.evaluate(() => {
      const normalized = (value) => String(value ?? '')
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .replace(/\s+/g, ' ')
        .trim()
        .toUpperCase();
      const elements = [];
      const visit = (root) => {
        for (const element of root.querySelectorAll('*')) {
          elements.push(element);
          if (element.shadowRoot) visit(element.shadowRoot);
        }
      };
      visit(document);
      const attributes = ['href', 'data-href', 'data-url', 'routerlink', 'ng-reflect-router-link'];
      const candidates = elements.filter((element) => {
        const text = normalized(element.innerText || element.textContent);
        const values = attributes.map((name) => element.getAttribute?.(name) || '').join(' ');
        return text === 'PORTAL DE SERVICOS'
          || /portalvivoempresas|service-now\.com/i.test(values);
      });
      for (const element of candidates) {
        for (const name of attributes) {
          const value = element.getAttribute?.(name);
          if (!value || /^javascript:/i.test(value)) continue;
          try {
            return new URL(value, location.href).href;
          } catch {
            // Continua procurando um destino valido.
          }
        }
        const anchor = element.closest?.('a[href]');
        if (anchor?.href && !/^javascript:/i.test(anchor.href)) return anchor.href;
      }
      return '';
    }).catch(() => '');
    if (destination) return destination;
  }
  return '';
}

async function activatePortalWithKeyboard(page) {
  // O painel lateral da Vivo possui navegacao por foco mesmo quando seus itens
  // nao recebem papeis acessiveis de botao/link. Isso evita coordenadas fixas.
  for (let index = 0; index < 24; index += 1) {
    await page.keyboard.press('Tab');
    const activeText = await page.evaluate(() => {
      const element = document.activeElement;
      if (!element) return '';
      return [
        element.getAttribute?.('aria-label'),
        element.getAttribute?.('title'),
        element.innerText,
        element.textContent,
      ].filter(Boolean).join(' ');
    }).catch(() => '');
    if (/portal de servi[cç]os/i.test(activeText)) {
      await page.keyboard.press('Enter');
      return true;
    }
  }
  return false;
}

async function authIsRequired(page) {
  const url = page.url().toLowerCase();
  const loginInput = await firstVisible([
    page.getByRole('textbox', { name: /cpf/i }),
    page.getByLabel(/cpf|login|usu[aá]rio|senha|password/i),
    page.locator('input[type="password"]'),
    page.locator('input[autocomplete="username"]'),
  ]);
  if (loginInput) return true;
  if (/\/login|signin/.test(url)) return true;
  if (/oauth/.test(url) && await isVisible(page.getByText(/boas-vindas|inserir suas informa/i))) return true;
  return false;
}

async function waitForRegistrationValidation(page) {
  if (registrationValidationWaitedPages.has(page)) return false;
  const validationScreen = await firstVisible([
    page.getByRole('button', { name: /validar cadastro/i }),
    page.getByText(/mantenha seu cadastro atualizado/i),
    page.getByText(/^validar cadastro$/i),
  ]);
  if (!validationScreen) return false;

  registrationValidationWaitedPages.add(page);
  emitStatus('Validacao de cadastro detectada. Aguardando 30 segundos...');
  await navigationLog('validacao_cadastro_detectada', { url: page.url(), wait_seconds: 30 });
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline && !page.isClosed()) {
    await page.waitForTimeout(Math.min(500, Math.max(1, deadline - Date.now())));
  }
  const stillVisible = Boolean(await firstVisible([
    page.getByRole('button', { name: /validar cadastro/i }),
    page.getByText(/^validar cadastro$/i),
  ]));
  await navigationLog('espera_validacao_cadastro_concluida', {
    url: page.url(),
    still_visible: stillVisible,
  });
  emitStatus('Espera de validacao concluida. Continuando o acesso...');
  return true;
}

async function handleIntermediaryScreens(page) {
  if (await clickText(page, [/continuar sem biometria/i], { optional: true })) {
    emitStatus('Continuando sem biometria...');
    await page.waitForTimeout(800);
  }
  await waitForRegistrationValidation(page);
  if (await clickText(page, [/atualizar depois/i], { optional: true })) {
    emitStatus('Atualizacao de celular adiada...');
    await page.waitForTimeout(800);
  }
}

async function automaticLogin(page, credentials) {
  if (!credentials) {
    throw new Error('O acesso automatico ainda nao foi configurado. Cadastre CPF e senha no robo.');
  }
  emitStatus('Sessao expirada. Refazendo o login automaticamente...');
  const deadline = Date.now() + 120_000;
  let cpfSent = false;
  let passwordSent = false;
  while (Date.now() < deadline) {
    await handleIntermediaryScreens(page);
    const body = await page.locator('body').innerText({ timeout: 5_000 }).catch(() => '');
    if (/senha.*incorret|dados.*inv[aá]lid|cpf.*inv[aá]lid|acesso.*negado/i.test(body)) {
      throw new Error('A Vivo recusou o CPF ou a senha. Use "Alterar login do portal da Vivo" e tente novamente.');
    }
    if (!(await authIsRequired(page))
      && /vivo m[oó]vel|portal de servi[cç]os|central de ajuda|gest[aã]o|suporte t[eé]cnico/i.test(body)) {
      emitStatus('Login automatico concluido.');
      return;
    }

    const cpfInput = await firstVisible([
      page.getByRole('textbox', { name: /cpf/i }),
      page.getByLabel(/cpf/i),
      page.locator('#text-field'),
      page.locator('input[autocomplete="username"]'),
      page.locator('input[name*="cpf" i]'),
    ]);
    const passwordInput = await firstVisible([
      page.locator('input[type="password"]'),
      page.getByLabel(/senha|password/i),
      page.locator('input[autocomplete="current-password"]'),
    ]);

    let filledSomething = false;
    if (cpfInput && !cpfSent) {
      await cpfInput.fill(credentials.cpf);
      cpfSent = true;
      filledSomething = true;
    }
    if (passwordInput && !passwordSent) {
      await passwordInput.fill(credentials.password);
      passwordSent = true;
      filledSomething = true;
    }
    if (filledSomething) {
      const actionNames = passwordInput
        ? [/^entrar$/i, /^acessar$/i, /^continuar$/i, /^prosseguir$/i]
        : [/^continuar$/i, /^avan[cç]ar$/i, /^prosseguir$/i];
      if (await clickText(page, actionNames, { optional: true, timeout: 4_000 })) {
        await page.waitForLoadState('domcontentloaded', { timeout: 15_000 }).catch(() => {});
        await page.waitForTimeout(800);
        continue;
      }
    }

    if (/captcha|recaptcha|hcaptcha|c[oó]digo.*sms|token.*seguran[cç]a|confirme.*aplicativo/i.test(body)
      && !/continuar sem biometria|atualizar depois/i.test(body)) {
      const error = new Error('A Vivo solicitou uma validacao de seguranca que nao pode ser automatizada.');
      error.authRequired = true;
      throw error;
    }
    await page.waitForTimeout(500);
  }
  throw new Error('O login automatico nao foi concluido dentro de 2 minutos. Confira o acesso salvo e tente novamente.');
}

async function runAuthentication({ dryRun = false, credentials = null } = {}) {
  if (dryRun) {
    emitStatus('Modo de teste: autenticacao validada sem abrir o navegador.');
    emit('success', { mode: 'auth', login_ready: true, message: 'Simulacao de autenticacao concluida.' });
    return;
  }
  emitStatus('Validando o acesso automatico...');
  const { context, page } = await launchChrome({ headless: true });
  try {
    await page.goto(DEFAULT_LOGIN_URL, { waitUntil: 'domcontentloaded' });
    await automaticLogin(page, credentials);
    emit('success', { mode: 'auth', login_ready: true, message: 'Acesso automatico validado com sucesso.' });
  } finally {
    await context.close().catch(() => {});
    activePage = null;
  }
}

async function clickServicePortalOption(page) {
  const portalNames = [/^portal de servi[cç]os$/i, /portal de servi[cç]os/i];
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    await navigationLog('tentativa_portal_servicos', { attempt, url: page.url() });
    if (await clickRenderedText(page, 'Portal de Serviços')) {
      await navigationLog('portal_servicos_clicado_texto_renderizado', { attempt, url: page.url() });
      return;
    }
    const alreadyVisible = await locatorForText(page, portalNames, ['button', 'link', 'menuitem', 'option']);
    if (alreadyVisible) {
      const clicked = await alreadyVisible.click({ timeout: 8_000 }).then(() => true).catch(() => false);
      if (clicked) {
        await navigationLog('portal_servicos_clicado_locator', { attempt, url: page.url() });
        return;
      }
    }
    if (await clickExactTextInDom(page, 'Portal de Serviços')) {
      await page.waitForTimeout(500);
      return;
    }

    const mobileMenu = await firstVisible([
      page.getByRole('button', { name: /vivo m[oó]vel/i }),
      page.getByRole('combobox', { name: /vivo m[oó]vel/i }),
      page.locator('[aria-haspopup="true"]').filter({ hasText: /vivo m[oó]vel/i }),
      page.locator('[aria-expanded]').filter({ hasText: /vivo m[oó]vel/i }),
      page.locator('button').filter({ hasText: /^\s*vivo m[oó]vel/i }),
      page.getByText(/^vivo m[oó]vel$/i, { exact: true }),
    ]);
    if (mobileMenu) {
      const opened = await mobileMenu.click({ timeout: 8_000 }).then(() => true).catch(() => false);
      if (!opened) await clickExactTextInDom(page, 'Vivo Móvel');
      await page.waitForTimeout(1_200);
    } else {
      await clickExactTextInDom(page, 'Vivo Móvel');
      await page.waitForTimeout(1_200);
    }

    const deadline = Date.now() + 5_000;
    while (Date.now() < deadline) {
      if (await clickRenderedText(page, 'Portal de Serviços')) {
        await navigationLog('portal_servicos_clicado_texto_renderizado', { attempt, url: page.url() });
        return;
      }
      const portalOption = await locatorForText(page, portalNames, ['button', 'link', 'menuitem', 'option']);
      if (portalOption) {
        await portalOption.scrollIntoViewIfNeeded().catch(() => {});
        const clicked = await portalOption.click({ timeout: 8_000 }).then(() => true).catch(() => false);
        if (clicked) return;
      }
      if (await clickExactTextInDom(page, 'Portal de Serviços')) {
        await page.waitForTimeout(500);
        return;
      }
      await page.waitForTimeout(300);
    }
    if (await activatePortalWithKeyboard(page)) {
      await page.waitForTimeout(800);
      return;
    }
  }

  const technicalDestination = await servicePortalDestination(page);
  if (technicalDestination) {
    emitStatus('Destino tecnico do Portal de Servicos identificado. Redirecionando...');
    await navigationLog('redirecionamento_tecnico_portal', {
      from: safeUrl(page.url()),
      url: technicalDestination,
    });
    await page.goto(technicalDestination, { waitUntil: 'domcontentloaded' });
    return;
  }

  // Ultimo caminho, independente do menu: o ServiceNow pode iniciar o SSO a
  // partir do endereco estavel do portal e reaproveitar a sessao atual da Vivo.
  emitStatus('Tentando acesso direto e autenticado ao Portal de Servicos...');
  const returnUrl = page.url();
  await page.goto(DEFAULT_SERVICE_NOW_URL, { waitUntil: 'domcontentloaded' }).catch(() => {});
  await page.waitForTimeout(1_500);
  const body = await page.locator('body').innerText({ timeout: 5_000 }).catch(() => '');
  if (/central de servi[cç]os|meu time vivo|portal de servi[cç]os vivo empresas/i.test(body)
    && isServiceNowPortalUrl(page.url())) {
    return;
  }
  await page.goto(returnUrl, { waitUntil: 'domcontentloaded' }).catch(() => {});
  throw new Error('Nao foi possivel acessar o Portal de Servicos pelo destino tecnico nem pelo seletor da Vivo.');
}

async function openActiveServicesDirectly(page) {
  if (!isServiceNowPortalUrl(page.url())) return false;
  emitStatus('Abrindo diretamente a lista de Servicos ativos...');
  const current = new URL(page.url());
  const destination = `${current.origin}/portalvivoempresas?${ACTIVE_SERVICES_QUERY}`;
  await page.goto(destination, { waitUntil: 'domcontentloaded' }).catch(() => {});
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    const body = await page.locator('body').innerText({ timeout: 5_000 }).catch(() => '');
    if (/servi[cç]os ativos/i.test(body)
      && /produto|identifica[cç][aã]o|endere[cç]o|id servi[cç]o/i.test(body)) {
      emitStatus('Lista de Servicos ativos carregada.');
      return true;
    }
    if (await authIsRequired(page)) return false;
    await page.waitForTimeout(500);
  }
  return false;
}

async function discoverTicketFormDestination(page) {
  const scopes = [page.mainFrame(), ...page.frames().filter((frame) => frame !== page.mainFrame())];
  for (const scope of scopes) {
    const result = await scope.evaluate(() => {
      const all = [];
      const visit = (root) => {
        for (const element of root.querySelectorAll('*')) {
          all.push(element);
          if (element.shadowRoot) visit(element.shadowRoot);
        }
      };
      visit(document);
      const attributes = [
        'href', 'data-href', 'data-url', 'action', 'formaction',
        'ng-href', 'ng-reflect-router-link', 'routerlink',
      ];
      for (const element of all) {
        for (const name of attributes) {
          const value = element.getAttribute?.(name) || '';
          if (!/csm_get_help/i.test(value)) continue;
          try {
            return { destination: new URL(value.replace(/&amp;/g, '&'), location.href).href };
          } catch {
            // Continua para a leitura do HTML serializado.
          }
        }
      }
      const html = document.documentElement.outerHTML
        .replace(/&amp;/g, '&')
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'");
      const destinationMatch = html.match(/(?:https?:[^"'<>\s]+)?\/portalvivoempresas\?[^"'<>\s]*id=csm_get_help[^"'<>\s]*/i);
      const soldProductMatch = html.match(/sold_product_sys_id["']?\s*[:=]\s*["']([a-f0-9]{32})/i)
        || html.match(/sold_product_sys_id(?:%3D|=)([a-f0-9]{32})/i);
      return {
        destination: destinationMatch?.[0] || '',
        soldProduct: soldProductMatch?.[1] || '',
      };
    }).catch(() => ({ destination: '', soldProduct: '' }));
    if (result.destination) {
      return new URL(result.destination, page.url()).href;
    }
    if (result.soldProduct) return { soldProduct: result.soldProduct };
  }
  return '';
}

const TICKET_FIELDS = Object.freeze({
  occurrence: {
    labels: [/o\s+que[\s\u00a0]+est[aá][\s\u00a0]+acontecendo/i],
    selectors: 'select, input, button, [role="combobox"]',
  },
  nickname: {
    labels: [/apelido/i, /protocolo[\s\u00a0]+interno/i],
    selectors: 'input, textarea',
  },
  contactName: {
    labels: [
      /nome[\s\u00a0]+(?:adicional[\s\u00a0]+)?(?:do[\s\u00a0]+)?contato/i,
      /contato[\s\u00a0]+adicional[\s\u00a0]+nome/i,
    ],
    selectors: 'input, textarea',
  },
  contactPhone: {
    labels: [
      /telefone[\s\u00a0]+(?:adicional[\s\u00a0]+)?(?:do[\s\u00a0]+)?contato/i,
      /celular[\s\u00a0]+(?:adicional[\s\u00a0]+)?(?:do[\s\u00a0]+)?contato/i,
    ],
    selectors: 'input',
  },
  contactEmail: {
    labels: [
      /e[\s-]*mail[\s\u00a0]+(?:adicional[\s\u00a0]+)?(?:do[\s\u00a0]+)?contato/i,
      /e[\s-]*mail[\s\u00a0]+(?:do[\s\u00a0]+)?contato/i,
    ],
    selectors: 'input',
  },
  startTime: {
    labels: [/hora[\s\u00a0]+de[\s\u00a0]+in[ií]cio/i, /hor[aá]rio[\s\u00a0]+inicial/i],
    selectors: 'input',
  },
  endTime: {
    labels: [
      /hora[\s\u00a0]+de[\s\u00a0]+t[eé]rmino/i,
      /hora[\s\u00a0]+de[\s\u00a0]+fim/i,
      /hor[aá]rio[\s\u00a0]+final/i,
    ],
    selectors: 'input',
  },
  sharePhone: {
    labels: [/autorizo[\s\S]{0,120}compartilhamento[\s\S]{0,120}(?:celular|telefone)/i],
    selectors: 'select, input, button, [role="combobox"]',
  },
  description: {
    labels: [/^\s*\*?\s*descri[cç][aã]o\s*$/i],
    selectors: 'textarea, input',
  },
});

async function ticketFormIsReady(page) {
  const occurrence = await findTicketField(page, 'occurrence');
  const nickname = await findTicketField(page, 'nickname');
  const occurrenceState = await ticketControlDescriptor(occurrence);
  const nicknameState = await ticketControlDescriptor(nickname);
  return Boolean(
    occurrenceState
      && nicknameState
      && occurrenceState.enabled
      && nicknameState.enabled
      && !nicknameState.readonly
      && occurrenceState.frameId === nicknameState.frameId,
  );
}

async function waitForTicketFormReady(page, timeout = 45_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await ticketFormIsReady(page)) {
      await navigationLog('formulario_tecnico_pronto', { url: page.url() });
      return true;
    }
    await page.waitForTimeout(400);
  }
  await navigationLog('formulario_tecnico_nao_ficou_pronto', { url: page.url() });
  return false;
}

async function openTicketFormDirectly(page) {
  if (!isServiceNowPortalUrl(page.url())) return false;
  const originalUrl = page.url();
  const discovered = await discoverTicketFormDestination(page);
  let destination = typeof discovered === 'string' ? discovered : '';
  if (!destination) {
    const current = new URL(originalUrl);
    const deployedEntity = cleanString(
      current.searchParams.get('deployed_entity_sys_id') || current.searchParams.get('sys_id'),
    );
    if (!/^[a-f0-9]{32}$/i.test(deployedEntity)) return false;
    const soldProduct = typeof discovered === 'object' && discovered.soldProduct
      ? discovered.soldProduct
      : VPN_MPLS_SOLD_PRODUCT_SYS_ID;
    destination = `${current.origin}/portalvivoempresas?id=csm_get_help`
      + `&sys_id=${encodeURIComponent(TECHNICAL_HELP_SYS_ID)}`
      + `&deployed_entity_sys_id=${encodeURIComponent(deployedEntity)}`
      + `&sold_product_sys_id=${encodeURIComponent(soldProduct)}`;
  }

  emitStatus('Abrindo diretamente o formulario tecnico do circuito confirmado...');
  await page.goto(destination, { waitUntil: 'domcontentloaded' }).catch(() => {});
  if (await waitForTicketFormReady(page, 45_000)) return true;
  await page.goto(originalUrl, { waitUntil: 'domcontentloaded' }).catch(() => {});
  return false;
}

async function openTicketFormForCandidate(page) {
  if (await ticketFormIsReady(page)) return;
  const clicked = await clickRenderedTextWhenReady(page, 'Abrir Chamado Técnico', 8_000);
  if (clicked && await waitForTicketFormReady(page, 45_000)) return;

  if (await openTicketFormDirectly(page)) return;

  const clickedByControl = await clickTextRobust(
    page,
    [/abrir chamado t[eé]cnico/i],
    'Abrir Chamado Técnico',
    { optional: true },
  );
  if (clickedByControl && await waitForTicketFormReady(page, 45_000)) return;
  throw new Error('O formulario tecnico nao terminou de carregar depois da selecao do circuito.');
}

function portalRouteId(page) {
  try {
    const route = new URL(page.url()).searchParams.get('id') || '';
    return PUBLIC_PORTAL_ROUTE_IDS.has(route) ? route : (route ? '<outra-rota>' : '');
  } catch {
    return '';
  }
}

async function portalStateMarkers(page) {
  const body = normalize(await page.locator('body').innerText({ timeout: 5_000 }).catch(() => ''));
  const route = portalRouteId(page);
  const productFilterReady = Boolean(await findHeaderInput(page, ['Produto']).catch(() => null));
  return {
    route,
    home: route === 'nova_solicitacao'
      || (body.includes('CENTRAL DE SERVICOS') && body.includes('MEU TIME VIVO')),
    central: body.includes('CENTRAL DE SERVICOS')
      && (
        body.includes('TECNICOS')
        || (body.includes('ABRIR CHAMADO TECNICO') && body.includes('SERVICOS ATIVOS'))
      ),
    central_technical: body.includes('CENTRAL DE SERVICOS')
      && body.includes('ABRIR CHAMADO TECNICO')
      && body.includes('SERVICOS ATIVOS'),
    active_services: productFilterReady
      && (route === 'meus_servicos_ativos' || body.includes('SERVICOS ATIVOS'))
      && body.includes('PRODUTO')
      && body.includes('IDENTIFICACAO')
      && body.includes('ENDERECO'),
  };
}

async function waitForPortalState(page, state, timeout = 25_000) {
  const deadline = Date.now() + timeout;
  let lastMarkers = null;
  while (Date.now() < deadline) {
    if (page.isClosed()) return false;
    const markers = await portalStateMarkers(page);
    lastMarkers = markers;
    if (markers[state]) {
      await navigationLog('estado_portal_confirmado', {
        page_id: pageId(page),
        state,
        route: markers.route,
        url: page.url(),
      });
      return true;
    }
    await page.waitForTimeout(400);
  }
  await navigationLog('estado_portal_nao_confirmado', {
    page_id: pageId(page),
    state,
    route: lastMarkers?.route || '',
    markers: lastMarkers || {},
    url: page.url(),
  });
  return false;
}

async function clickPortalCardAndWait(page, label, names, expectedState) {
  if (await waitForPortalState(page, expectedState, 800)) return;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    await navigationLog('tentativa_card_portal', {
      page_id: pageId(page),
      label,
      expected_state: expectedState,
      attempt,
      url: page.url(),
    });
    let clicked = await clickRenderedTextWhenReady(page, label, attempt === 1 ? 12_000 : 4_000);
    if (!clicked) clicked = await clickTextRobust(page, names, label, { optional: true });
    if (!clicked) continue;
    if (await waitForPortalState(page, expectedState, 25_000)) return;
  }
  throw new Error(`O portal recebeu o clique em "${label}", mas nao abriu a etapa seguinte.`);
}

async function openServicePortal(initialPage, context, ticket, credentials) {
  let page = initialPage;
  emitStatus('Acessando o Portal Vivo Empresas...');
  await logOpenPages(context, 'inicio_open_service_portal', page);
  await page.goto(ticket.portalUrl, { waitUntil: 'domcontentloaded' });
  await navigationLog('dashboard_aberto', { url: page.url() });
  await page.waitForTimeout(1_000);
  if (await authIsRequired(page)) {
    await automaticLogin(page, credentials);
    await page.goto(ticket.portalUrl, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1_000);
  }
  await handleIntermediaryScreens(page);

  if (!isServiceNowPortalUrl(page.url())) {
    emitStatus('Abrindo a lista de servicos da Vivo...');
    const pagesBeforePortalClick = new Set(context.pages());
    // O Portal de Servicos abre uma nova aba. O observador precisa estar
    // armado antes do clique para que a pagina criada nao seja confundida
    // com uma aba antiga do navegador.
    const createdPagePromise = context.waitForEvent('page', { timeout: 20_000 }).catch(() => null);
    await clickServicePortalOption(page);
    const createdPage = await Promise.race([
      createdPagePromise,
      new Promise((resolve) => setTimeout(() => resolve(null), 5_000)),
    ]);
    if (createdPage) {
      observePage(createdPage);
      await navigationLog('nova_aba_capturada_no_clique', {
        page_id: pageId(createdPage),
        opener_page_id: pageId(page),
        url: createdPage.url(),
      });
    }
    page = await waitForPortalPage(context, pagesBeforePortalClick, page, 8_000, createdPage);
    await logOpenPages(context, 'apos_click_portal_servicos', page);
    if (!isServiceNowPortalUrl(page.url())) {
      const redirectNames = [/ir pro portal de servi[cç]os/i, /ir para o portal de servi[cç]os/i];
      const bridgeDeadline = Date.now() + 20_000;
      let redirectPage = null;
      while (Date.now() < bridgeDeadline && !isServiceNowPortalUrl(page.url())) {
        redirectPage = await pageWithTextControl(context, redirectNames);
        if (redirectPage) break;
        const exactPage = context.pages().find((candidate) =>
          isServiceNowPortalUrl(candidate.url())
          && (candidate === createdPage || candidate === page || !pagesBeforePortalClick.has(candidate)));
        if (exactPage) {
          page = exactPage;
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 400));
      }
      if (redirectPage && !isServiceNowPortalUrl(page.url())) {
        try {
          await clickText(redirectPage, redirectNames, { timeout: 8_000 });
          page = redirectPage;
          await navigationLog('ponte_ir_pro_portal_clicada', {
            page_id: pageId(redirectPage),
            url: redirectPage.url(),
          });
        } catch {
          // O SSO pode trocar de pagina enquanto o botao e clicado. A busca
          // pela aba correta continua mesmo se esse controle desaparecer.
          await navigationLog('ponte_ir_pro_portal_desapareceu', {
            page_id: pageId(redirectPage),
            url: redirectPage.url(),
          });
        }
      }
    }
    if (!isServiceNowPortalUrl(page.url())) {
      page = await waitForPortalPage(
        context,
        pagesBeforePortalClick,
        page,
        45_000,
        createdPage || page,
      );
    }
  }

  if (!isServiceNowPortalUrl(page.url())) {
    await logOpenPages(context, 'falha_selecionar_aba_servicenow', page);
    throw new Error('O Portal de Servicos abriu outra aba, mas ela nao concluiu o redirecionamento para o portal da Vivo.');
  }
  activePage = page;
  await page.bringToFront().catch(() => {});
  await logOpenPages(context, 'servicenow_confirmado', page);

  if (await authIsRequired(page)) {
    await automaticLogin(page, credentials);
  }
  await handleIntermediaryScreens(page);

  if (!(await waitForPortalState(page, 'home', 30_000))) {
    throw new Error('A aba do Portal de Servicos foi encontrada, mas a pagina inicial nao terminou de carregar.');
  }

  emitStatus('Entrando na Central de servicos...');
  await navigationLog('procurando_central_servicos', { url: page.url() });
  await clickPortalCardAndWait(
    page,
    'Central de serviços',
    [/central de servi[cç]os/i],
    'central',
  );
  await navigationLog('central_servicos_clicada', { url: page.url() });

  if (!(await waitForPortalState(page, 'central_technical', 800))) {
    emitStatus('Abrindo a aba Tecnicos...');
    await navigationLog('procurando_aba_tecnicos', { url: page.url() });
    await clickPortalCardAndWait(
      page,
      'Técnicos',
      [/^t[eé]cnicos$/i],
      'central_technical',
    );
    await navigationLog('aba_tecnicos_clicada', { url: page.url() });
  }

  emitStatus('Selecionando Abrir Chamado Tecnico...');
  await navigationLog('procurando_abrir_chamado_tecnico', { url: page.url() });
  await clickPortalCardAndWait(
    page,
    'Abrir Chamado Técnico',
    [/abrir chamado t[eé]cnico/i],
    'active_services',
  );
  await navigationLog('abrir_chamado_tecnico_clicado', { url: page.url() });
  return page;
}

async function findHeaderInput(page, names) {
  const headers = page.locator('th');
  const count = await headers.count();
  const targets = names.map(normalize);
  for (let index = 0; index < count; index += 1) {
    const header = headers.nth(index);
    const headerText = normalize(await header.innerText().catch(() => ''));
    if (!targets.some((target) => headerText === target || headerText.startsWith(`${target} `))) continue;
    const input = header.locator('input').first();
    if (await input.count() && await isVisible(input)) return input;
  }
  for (const name of names) {
    const escaped = cleanString(name).replace(/"/g, '\\"');
    const candidate = await firstVisible([
      page.getByLabel(textRegex(name)),
      page.locator(`input[aria-label*="${escaped}" i]`),
      page.locator(`input[placeholder*="${escaped}" i]`),
    ]);
    if (candidate) return candidate;
  }
  return null;
}

async function waitForHeaderInput(page, names, timeout) {
  const deadline = Date.now() + timeout;
  do {
    const input = await findHeaderInput(page, names);
    if (input) return input;
    if (Date.now() < deadline) await page.waitForTimeout(300);
  } while (Date.now() < deadline);
  return null;
}

async function setColumnFilter(page, names, value, required = false) {
  const wanted = cleanString(value);
  const timeout = required ? 30_000 : 2_500;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const input = await waitForHeaderInput(page, names, timeout);
    if (!input) {
      if (required) throw new Error(`Filtro da tabela nao encontrado: ${names.join(' / ')}.`);
      return false;
    }
    try {
      const current = cleanString(await input.inputValue().catch(() => ''));
      if (current !== wanted) {
        await input.click({ timeout: 5_000 });
        await input.fill(wanted, { timeout: 8_000 });
        await input.press('Enter').catch(() => {});
      }
      await page.waitForTimeout(wanted ? 1_500 : 500);
      const refreshed = await waitForHeaderInput(page, names, 5_000);
      const actual = cleanString(await refreshed?.inputValue().catch(() => ''));
      if (actual === wanted) {
        await navigationLog('filtro_coluna_confirmado', {
          column: names[0],
          value_present: Boolean(wanted),
          value_length: wanted.length,
          attempt,
          url: page.url(),
        });
        return true;
      }
    } catch {
      // A tabela do ServiceNow recria os inputs enquanto filtra. Rebuscamos
      // o campo e repetimos sem reaproveitar uma referencia DOM desconectada.
    }
    await navigationLog('filtro_coluna_retentativa', {
      column: names[0],
      value_present: Boolean(wanted),
      attempt,
      url: page.url(),
    });
  }
  if (required) throw new Error(`O portal nao manteve o filtro ${names[0]} preenchido.`);
  return false;
}

function canonicalHeader(header) {
  const name = normalize(header);
  if (name.startsWith('EMPRESA')) return 'company';
  if (name === 'CNPJ' || name.startsWith('CNPJ ')) return 'cnpj';
  if (name.startsWith('PRODUTO')) return 'product';
  if (name.startsWith('IDENTIFICACAO')) return 'identification';
  if (name.startsWith('ID SERVICO') || name === 'ID') return 'serviceId';
  if (name.startsWith('ENDERECO')) return 'address';
  if (name.startsWith('CIDADE')) return 'city';
  return header;
}

async function collectTableRows(page) {
  const scopes = [page, ...page.frames().filter((frame) => frame !== page.mainFrame())];
  const result = [];
  for (let scopeIndex = 0; scopeIndex < scopes.length; scopeIndex += 1) {
    const scope = scopes[scopeIndex];
    const tables = scope.locator('table');
    const tableCount = Math.min(await tables.count().catch(() => 0), 12);
    for (let tableIndex = 0; tableIndex < tableCount; tableIndex += 1) {
      const table = tables.nth(tableIndex);
      let headers = await table.locator('thead th').allInnerTexts().catch(() => []);
      if (!headers.length) headers = await table.locator('tr').first().locator('th').allInnerTexts().catch(() => []);
      headers = headers.map((item) => cleanString(item));
      if (!headers.some((item) => normalize(item).includes('PRODUTO'))) continue;
      const rows = table.locator('tr');
      const rowCount = Math.min(await rows.count().catch(() => 0), 250);
      for (let rowIndex = 0; rowIndex < rowCount; rowIndex += 1) {
        const cells = (await rows.nth(rowIndex).locator('td').allInnerTexts().catch(() => []))
          .map((item) => cleanString(item));
        if (!cells.length || !cells.join('').trim()) continue;
        const fields = {};
        cells.forEach((cell, cellIndex) => { fields[headers[cellIndex] || `col_${cellIndex}`] = cell; });
        result.push({
          kind: 'table', scopeIndex, tableIndex, rowIndex, cells, fields, text: cells.join(' | '),
        });
      }
    }

    // Algumas versoes do widget ServiceNow usam ARIA grid em vez de uma
    // tabela HTML. Esse segundo leitor cobre a mesma estrutura sem depender
    // do elemento <tbody>.
    if (result.some((row) => row.scopeIndex === scopeIndex)) continue;
    const grids = scope.locator('[role="grid"], [role="table"]');
    const gridCount = Math.min(await grids.count().catch(() => 0), 8);
    for (let tableIndex = 0; tableIndex < gridCount; tableIndex += 1) {
      const grid = grids.nth(tableIndex);
      const headers = (await grid.locator('[role="columnheader"]').allInnerTexts().catch(() => []))
        .map((item) => cleanString(item));
      if (!headers.some((item) => normalize(item).includes('PRODUTO'))) continue;
      const rows = grid.locator('[role="row"]');
      const rowCount = Math.min(await rows.count().catch(() => 0), 250);
      for (let rowIndex = 0; rowIndex < rowCount; rowIndex += 1) {
        const cells = (await rows.nth(rowIndex).locator('[role="gridcell"], [role="cell"]').allInnerTexts().catch(() => []))
          .map((item) => cleanString(item));
        if (!cells.length || !cells.join('').trim()) continue;
        const fields = {};
        cells.forEach((cell, cellIndex) => { fields[headers[cellIndex] || `col_${cellIndex}`] = cell; });
        result.push({
          kind: 'grid', scopeIndex, tableIndex, rowIndex, cells, fields, text: cells.join(' | '),
        });
      }
    }
  }
  return result;
}

function publicCandidate(row) {
  const mapped = {};
  for (const [header, value] of Object.entries(row.fields ?? {})) mapped[canonicalHeader(header)] = value;
  return {
    company: cleanString(mapped.company),
    product: cleanString(mapped.product),
    identification: cleanString(mapped.identification),
    service_id: cleanString(mapped.serviceId),
    address: cleanString(mapped.address),
    city: cleanString(mapped.city),
    cnpj: cleanString(mapped.cnpj),
    row_text: cleanString(row.text),
  };
}

function normalizedAddressNumbers(value) {
  return normalize(value)
    .split(' ')
    .filter((part) => /^\d+$/.test(part))
    .map((part) => String(Number.parseInt(part, 10)));
}

function candidateMatchesStrategy(candidate, strategy, query, ticket = null) {
  const wanted = cleanString(query);
  if (!wanted) return true;
  if (strategy === 'identification') {
    return digits(candidate.identification) === digits(wanted);
  }
  if (strategy === 'service_id') {
    return digits(candidate.service_id) === digits(wanted);
  }
  if (strategy === 'city') {
    const cityMatches = normalize(candidate.city || candidate.row_text).includes(normalize(wanted));
    const expectedNumber = normalizedAddressNumbers(ticket?.address || '')[0];
    const candidateNumbers = normalizedAddressNumbers(candidate.address || candidate.row_text);
    return cityMatches && (!expectedNumber || candidateNumbers.includes(expectedNumber));
  }
  const addressMatches = normalize(candidate.address || candidate.row_text).includes(normalize(wanted));
  const expectedNumber = normalizedAddressNumbers(ticket?.address || '')[0];
  const candidateNumbers = normalizedAddressNumbers(candidate.address || candidate.row_text);
  return addressMatches && (!expectedNumber || candidateNumbers.includes(expectedNumber));
}

async function collectTextFallbackRows(page, strategy, query) {
  const scopes = [page, ...page.frames().filter((frame) => frame !== page.mainFrame())];
  const rows = [];
  const seen = new Set();
  for (let scopeIndex = 0; scopeIndex < scopes.length; scopeIndex += 1) {
    const scope = scopes[scopeIndex];
    const matches = scope.getByText(textRegex(query), { exact: false });
    const count = Math.min(await matches.count().catch(() => 0), 30);
    for (let textMatchIndex = 0; textMatchIndex < count; textMatchIndex += 1) {
      const hit = matches.nth(textMatchIndex);
      if (!await isVisible(hit)) continue;
      const row = hit.locator('xpath=ancestor-or-self::*[self::tr or @role="row"][1]');
      if (!await row.count().catch(() => 0)) continue;
      const rowText = cleanString(await row.innerText().catch(() => ''));
      if (!rowText || seen.has(normalize(rowText))) continue;
      seen.add(normalize(rowText));
      const product = rowText.match(/VPN\s+(?:IP\s+)?MPLS/i)?.[0] || '';
      const fields = { Produto: product };
      if (strategy === 'identification') fields['Identificação'] = cleanString(query);
      if (strategy === 'service_id') fields['ID Serviço'] = cleanString(query);
      if (strategy === 'address_keyword' || strategy === 'city') fields.Endereço = rowText;
      rows.push({
        kind: 'text', scopeIndex, textMatchIndex, searchText: cleanString(query),
        cells: [rowText], fields, text: rowText,
      });
    }
  }
  return rows;
}

async function waitForMatchingRows(page, strategy, query, ticket, timeout = 8_000) {
  const deadline = Date.now() + timeout;
  let lastRowCount = 0;
  do {
    const rawRows = await collectTableRows(page).catch(() => []);
    lastRowCount = rawRows.length;
    let matching = rawRows
      .map((row) => ({ ...row, candidate: publicCandidate(row) }))
      .filter((row) => candidateMatchesStrategy(row.candidate, strategy, query, ticket));
    if (!matching.length) {
      matching = (await collectTextFallbackRows(page, strategy, query))
        .map((row) => ({ ...row, candidate: publicCandidate(row) }))
        .filter((row) => candidateMatchesStrategy(row.candidate, strategy, query, ticket));
    }
    if (matching.length) {
      await navigationLog('linhas_filtradas_encontradas', {
        strategy,
        matching_count: matching.length,
        table_row_count: rawRows.length,
        url: page.url(),
      });
      return matching;
    }
    const body = normalize(await page.locator('body').innerText({ timeout: 3_000 }).catch(() => ''));
    if (/LINHAS\s*[-:]\s*0|NENHUM (REGISTRO|RESULTADO)|NAO HA RESULTADOS/.test(body)) break;
    if (Date.now() < deadline) await page.waitForTimeout(350);
  } while (Date.now() < deadline);
  await navigationLog('nenhuma_linha_filtrada', {
    strategy,
    table_row_count: lastRowCount,
    url: page.url(),
  });
  return [];
}

function matchesExpectedProduct(value, expectedProduct = 'VPN IP MPLS') {
  const text = normalize(value);
  const expected = normalize(expectedProduct);
  if (expected && text.includes(expected)) return true;
  if (expected === 'VPN IP MPLS') {
    return text.includes('VPN IP MPLS') || (text.includes('VPN') && text.includes('MPLS'));
  }
  return false;
}

function candidateScore(candidate, ticket, strategy, strategyQuery = '') {
  let score = 0;
  if (matchesExpectedProduct(candidate.product || candidate.row_text, ticket.expectedProduct)) score += 100;
  if (ticket.identification && digits(candidate.identification) === digits(ticket.identification)) score += 90;
  if (ticket.serviceId && digits(candidate.service_id) === digits(ticket.serviceId)) score += 85;
  const address = normalize(candidate.address || candidate.row_text);
  if (ticket.searchKeyword && address.includes(normalize(ticket.searchKeyword))) score += 25;
  if (ticket.city && normalize(candidate.city || candidate.row_text).includes(normalize(ticket.city))) score += 20;
  if (strategyQuery && normalize(candidate.row_text).includes(normalize(strategyQuery))) score += 18;
  const normalizeNumber = (value) => String(Number.parseInt(value, 10));
  const expectedNumbers = normalize(ticket.address)
    .split(' ')
    .filter((part) => /^\d+$/.test(part))
    .map(normalizeNumber);
  const candidateNumbers = address
    .split(' ')
    .filter((part) => /^\d+$/.test(part))
    .map(normalizeNumber);
  if (expectedNumbers.some((number) => candidateNumbers.includes(number))) score += 15;
  if (strategy === 'identification') score += 8;
  if (strategy === 'service_id') score += 6;
  return score;
}

async function applySearch(page, strategy, query) {
  const productFilter = cleanString(query?.productFilter || 'MPLS');
  const searchQuery = typeof query === 'object' ? query.value : query;
  // Primeiro limpamos os filtros variaveis e, por ultimo, aplicamos Produto.
  // Assim uma atualizacao da tabela nao apaga o MPLS que deve permanecer em
  // todas as estrategias de pesquisa.
  await setColumnFilter(page, ['Identificação', 'Identificacao'], '', false);
  await setColumnFilter(page, ['ID Serviço', 'ID Servico'], '', false);
  await setColumnFilter(page, ['Endereço', 'Endereco'], '', false);
  await setColumnFilter(page, ['Cidade'], '', false);
  emitStatus('Aplicando filtro Produto = MPLS...');
  await navigationLog('filtro_produto_iniciado', {
    product_mpls: normalize(productFilter).includes('MPLS'),
    strategy,
    url: page.url(),
  });
  await setColumnFilter(page, ['Produto'], productFilter, true);
  if (strategy === 'identification') {
    await setColumnFilter(page, ['Identificação', 'Identificacao'], searchQuery, true);
  } else if (strategy === 'service_id') {
    await setColumnFilter(page, ['ID Serviço', 'ID Servico'], searchQuery, true);
  } else if (strategy === 'city') {
    const applied = await setColumnFilter(page, ['Cidade'], searchQuery, false);
    if (!applied) return false;
  } else {
    await setColumnFilter(page, ['Endereço', 'Endereco'], searchQuery, true);
  }
  // A filtragem da segunda coluna recria partes da tabela em algumas sessoes.
  // Confirmamos novamente que Produto continuou preenchido com MPLS.
  const productInput = await waitForHeaderInput(page, ['Produto'], 10_000);
  const currentProduct = cleanString(await productInput?.inputValue().catch(() => ''));
  if (normalize(currentProduct) !== normalize(productFilter)) {
    await setColumnFilter(page, ['Produto'], productFilter, true);
  }
  await navigationLog('filtros_pesquisa_confirmados', {
    strategy,
    product_mpls: true,
    url: page.url(),
  });
  await page.waitForTimeout(1_200);
  return true;
}

async function openTableRow(page, row) {
  const scopes = [page, ...page.frames().filter((frame) => frame !== page.mainFrame())];
  const scope = scopes[row.scopeIndex] || page;
  let locator;
  if (row.kind === 'text') {
    const hit = scope.getByText(textRegex(row.searchText), { exact: false }).nth(row.textMatchIndex);
    const ancestor = hit.locator('xpath=ancestor-or-self::*[self::tr or @role="row"][1]');
    locator = await ancestor.count().catch(() => 0) ? ancestor : hit;
  } else {
    const collection = row.kind === 'grid'
      ? scope.locator('[role="grid"], [role="table"]').nth(row.tableIndex)
      : scope.locator('table').nth(row.tableIndex);
    locator = row.kind === 'grid'
      ? collection.locator('[role="row"]').nth(row.rowIndex)
      : collection.locator('tr').nth(row.rowIndex);
  }
  if (!await locator.count()) throw new Error('O resultado selecionado deixou de existir na tabela.');
  const actionable = await firstVisible([
    locator.locator('a'),
    locator.locator('button'),
    locator.locator('[role="link"]'),
  ]);
  await (actionable ?? locator).click();
  await page.waitForLoadState('domcontentloaded', { timeout: 15_000 }).catch(() => {});
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    const body = normalize(await page.locator('body').innerText({ timeout: 3_000 }).catch(() => ''));
    if (body.includes('DETALHES DO SERVICO ATIVO')
      || (body.includes('ENDERECO DE INSTALACAO') && body.includes('ABRIR CHAMADO TECNICO'))) {
      await navigationLog('detalhes_circuito_carregados', { url: page.url() });
      return;
    }
    await page.waitForTimeout(350);
  }
  throw new Error('O circuito foi selecionado, mas a pagina de detalhes nao terminou de carregar.');
}

async function labeledValue(page, names) {
  const normalizedNames = names.map(normalize);
  for (const name of names) {
    const regex = new RegExp(`^\\s*${cleanString(name).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`, 'i');
    const label = page.getByText(regex).first();
    if (!await isVisible(label)) continue;
    const value = await label.evaluate((element) => {
      const labelText = (element.textContent || '').trim();
      const candidates = [element.nextElementSibling, ...element.parentElement?.children ?? []];
      for (const candidate of candidates) {
        if (!candidate || candidate === element) continue;
        const text = (candidate.innerText || candidate.textContent || '').replace(/\s+/g, ' ').trim();
        if (text && text !== labelText) return text;
      }
      const parentText = (element.parentElement?.innerText || '').split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
      return parentText.find((item) => item !== labelText) || '';
    }).catch(() => '');
    const cleanedValue = cleanString(value);
    // O portal pode devolver o próprio título do campo. Ignoramos esse falso
    // valor para usar o endereço já lido na linha selecionada da tabela.
    if (cleanedValue && !normalizedNames.includes(normalize(cleanedValue))) {
      return cleanedValue;
    }
  }
  return '';
}

async function readServiceDetails(page, rowCandidate) {
  const fallback = publicCandidate(rowCandidate);
  return {
    company: await labeledValue(page, ['Empresa']) || fallback.company,
    product: await labeledValue(page, ['Produto/Serviço', 'Produto / Serviço', 'Produto/Servico']) || fallback.product,
    service_id: await labeledValue(page, ['ID']) || fallback.service_id,
    address: await labeledValue(page, ['Endereço De Instalação', 'Endereço de Instalação', 'Endereco de Instalacao']) || fallback.address,
    city: await labeledValue(page, ['Cidade']) || fallback.city,
    identification: await labeledValue(page, ['Identificação', 'Identificacao']) || fallback.identification,
    speed: await labeledValue(page, ['Velocidade']),
    contract: await labeledValue(page, ['Nº De Contrato', 'Nº de Contrato', 'Numero do Contrato']),
  };
}

function isConfirmationMessage(message, action, requestId) {
  const messageType = normalize(message?.type ?? message?.event);
  const messageAction = normalize(message?.action ?? message?.for ?? message?.step);
  const expected = normalize(action);
  const id = cleanString(message?.request_id ?? message?.requestId);
  if (id && requestId && id !== requestId) return false;
  if (messageType === normalize(`${action}_response`)) return true;
  if (messageType === 'RESPONSE' && messageAction === expected) return true;
  if (messageAction === normalize(`${action}_response`)) return true;
  return false;
}

function confirmedFrom(message) {
  return boolValue(message?.confirmed ?? message?.accepted ?? message?.approve ?? message?.value, false);
}

async function returnToResults(page) {
  if (await clickText(page, [/^voltar$/i], { optional: true })) {
    await page.waitForTimeout(700);
    return;
  }
  await page.goBack({ waitUntil: 'domcontentloaded', timeout: 15_000 }).catch(() => {});
  await page.waitForTimeout(700);
}

async function choosePortalCandidate(page, ticket, bus) {
  const strategies = [];
  if (ticket.identification) strategies.push({ name: 'identification', query: ticket.identification });
  if (ticket.serviceId && digits(ticket.serviceId) !== digits(ticket.identification)) {
    strategies.push({ name: 'service_id', query: ticket.serviceId });
  }
  for (const keyword of ticket.searchKeywords) {
    strategies.push({ name: 'address_keyword', query: keyword });
  }
  if (ticket.city) strategies.push({ name: 'city', query: ticket.city });
  const visited = new Set();

  for (const strategy of strategies) {
    emitStatus(`Pesquisando o circuito por ${strategy.name === 'address_keyword' ? 'endereco/CEP' : strategy.name === 'city' ? 'cidade' : strategy.name === 'service_id' ? 'ID do servico' : 'identificacao'}...`, {
      strategy: strategy.name,
      query: strategy.query,
    });
    const applied = await applySearch(page, strategy.name, {
      value: strategy.query,
      productFilter: ticket.productFilter,
    });
    if (!applied) continue;
    let rows = await waitForMatchingRows(page, strategy.name, strategy.query, ticket);
    rows = rows
      .filter((row) => matchesExpectedProduct(
        row.candidate.product || row.candidate.row_text,
        ticket.expectedProduct,
      ))
      .map((row) => ({
        ...row,
        score: candidateScore(row.candidate, ticket, strategy.name, strategy.query),
      }))
      .sort((a, b) => b.score - a.score)
      .slice(0, 12);

    for (const row of rows) {
      const key = [digits(row.candidate.identification), digits(row.candidate.service_id), normalize(row.candidate.address)].join('|');
      if (visited.has(key)) continue;
      visited.add(key);
      await openTableRow(page, row);
      const details = await readServiceDetails(page, row);
      if (!matchesExpectedProduct(details.product, ticket.expectedProduct)) {
        emitStatus(`Resultado ignorado porque o produto nao e ${ticket.expectedProduct}.`);
        await returnToResults(page);
        await applySearch(page, strategy.name, {
          value: strategy.query,
          productFilter: ticket.productFilter,
        });
        continue;
      }

      const candidate = {
        ...details,
        strategy: strategy.name,
        query: strategy.query,
        confidence: row.score,
      };
      emit('portal_candidate', {
        request_id: ticket.requestId,
        candidate,
        expected: {
          vd: ticket.vd,
          store_name: ticket.storeName,
          address: ticket.address,
          city: ticket.city,
          identification: ticket.identification,
          service_id: ticket.serviceId,
        },
        message: 'Confirme se este e o circuito correto antes de continuar.',
      });
      const answer = await bus.waitFor((message) =>
        isConfirmationMessage(message, 'portal_candidate', ticket.requestId),
      );
      if (confirmedFrom(answer)) return candidate;
      emitStatus('Candidato recusado. Procurando o proximo resultado...');
      await returnToResults(page);
      await applySearch(page, strategy.name, {
        value: strategy.query,
        productFilter: ticket.productFilter,
      });
    }
  }
  throw new Error('Nenhum circuito VPN IP MPLS compativel foi encontrado pela identificacao, ID do servico ou endereco desta loja.');
}

async function firstVisibleMatching(locator, selectors) {
  const count = Math.min(await locator.count().catch(() => 0), 50);
  for (let index = 0; index < count; index += 1) {
    const candidate = locator.nth(index);
    if (!await isVisible(candidate)) continue;
    const matches = await candidate.evaluate(
      (element, expectedSelectors) => element.matches(expectedSelectors),
      selectors,
    ).catch(() => false);
    if (matches) return candidate;
  }
  return null;
}

async function controlByLabel(page, labels, selectors = 'input, textarea, select') {
  const scopes = [page, ...page.frames().filter((frame) => frame !== page.mainFrame())];
  for (const scope of scopes) {
    for (const labelText of labels) {
      const regex = textRegex(labelText);
      const direct = await firstVisibleMatching(scope.getByLabel(regex), selectors);
      if (direct) return direct;
      const labelsFound = scope.locator('label').filter({ hasText: regex });
      const count = Math.min(await labelsFound.count().catch(() => 0), 50);
      for (let index = 0; index < count; index += 1) {
        const label = labelsFound.nth(index);
        if (!await isVisible(label)) continue;
        const nested = await firstVisible([label.locator(selectors)]);
        if (nested) return nested;
        const id = await label.getAttribute('for').catch(() => null);
        if (id) {
          const associated = await firstVisible([scope.locator(`[id=${JSON.stringify(id)}]`)]);
          if (associated) return associated;
        }
      }

      // O painel lateral "Informacoes necessarias" repete os nomes dos campos.
      // Por isso este fallback so aceita texto curto dentro de um grupo de campo
      // conhecido; ele nunca sobe ate um ancestral amplo da pagina.
      const textNodes = scope.locator(
        'span, p, legend, .control-label, [class*="field-label"], [class*="variable-label"]',
      ).filter({ hasText: regex });
      const textCount = Math.min(await textNodes.count().catch(() => 0), 50);
      for (let index = 0; index < textCount; index += 1) {
        const marker = textNodes.nth(index);
        if (!await isVisible(marker)) continue;
        const markerText = cleanString(await marker.innerText().catch(() => ''));
        if (!markerText || markerText.length > 180) continue;
        const group = marker.locator(
          'xpath=ancestor::*['
            + 'contains(concat(" ", normalize-space(@class), " "), " form-group ") or '
            + 'contains(concat(" ", normalize-space(@class), " "), " form-field ") or '
            + 'contains(concat(" ", normalize-space(@class), " "), " variable-field ") or '
            + 'contains(concat(" ", normalize-space(@class), " "), " sc-field ")'
            + '][1]',
        );
        if (!await group.count().catch(() => 0)) continue;
        const nearby = await firstVisible([group.locator(selectors)]);
        if (nearby) return nearby;
      }
    }
  }
  return null;
}

async function waitForControlByLabel(page, labels, selectors, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  do {
    const control = await controlByLabel(page, labels, selectors);
    if (control) return control;
    if (Date.now() < deadline) await page.waitForTimeout(350);
  } while (Date.now() < deadline);
  return null;
}

async function firstVisibleAcrossScopes(page, selectors) {
  const scopes = [page, ...page.frames().filter((frame) => frame !== page.mainFrame())];
  for (const scope of scopes) {
    const found = await firstVisible(selectors.map((selector) => scope.locator(selector)));
    if (found) return found;
  }
  return null;
}

async function ticketFieldFallback(page, key) {
  if (key === 'nickname') {
    return firstVisibleAcrossScopes(page, [
      'input[placeholder*="Protocolo Interno" i]',
      'input[placeholder*="Apelido" i]',
    ]);
  }
  if (key === 'contactPhone') {
    return firstVisibleAcrossScopes(page, [
      'input[type="tel"]',
      'input[placeholder*="DDD" i]',
      'input[placeholder*="Celular" i]',
      'input[placeholder*="Fixo" i]',
    ]);
  }
  if (key === 'contactName') {
    return firstVisibleAcrossScopes(page, [
      'input[name*="nome" i][name*="contato" i]',
      'input[id*="nome" i][id*="contato" i]',
      'input[aria-label*="nome" i][aria-label*="contato" i]',
    ]);
  }
  if (key === 'contactEmail') {
    const direct = await firstVisibleAcrossScopes(page, [
      'input[type="email"]',
      'input[placeholder*="mail" i]',
      'input[name*="mail" i]',
      'input[id*="mail" i]',
    ]);
    if (direct) return direct;
    return null;
  }
  if (key === 'startTime' || key === 'endTime') {
    const timeInputs = [];
    const scopes = [page, ...page.frames().filter((frame) => frame !== page.mainFrame())];
    for (const scope of scopes) {
      const controls = scope.locator('input[type="time"]');
      const count = Math.min(await controls.count().catch(() => 0), 10);
      for (let index = 0; index < count; index += 1) {
        const candidate = controls.nth(index);
        if (await isVisible(candidate)) timeInputs.push(candidate);
      }
    }
    if (timeInputs.length >= 2) return key === 'startTime' ? timeInputs[0] : timeInputs[1];
    return null;
  }
  if (key === 'description') {
    const specific = await firstVisibleAcrossScopes(page, [
      'textarea[placeholder*="1000" i]',
      'textarea[placeholder*="limite" i]',
      'textarea[name*="descr" i]',
      'textarea[id*="descr" i]',
    ]);
    if (specific) return specific;
    const scopes = [page, ...page.frames().filter((frame) => frame !== page.mainFrame())];
    const visible = [];
    for (const scope of scopes) {
      const areas = scope.locator('textarea:not([disabled]):not([readonly])');
      const count = Math.min(await areas.count().catch(() => 0), 10);
      for (let index = 0; index < count; index += 1) {
        const candidate = areas.nth(index);
        if (await isVisible(candidate)) visible.push(candidate);
      }
    }
    return visible.length === 1 ? visible[0] : null;
  }
  return null;
}

async function findTicketField(page, key) {
  const spec = TICKET_FIELDS[key];
  if (!spec) return null;
  // A pergunta sobre envio de documentacao tambem menciona "Descricao" e
  // possui role=group. Para este campo, a caixa de texto grande/placeholder
  // de 1000 caracteres e uma identificacao mais precisa que o rotulo global.
  if (key === 'description') {
    const descriptionArea = await ticketFieldFallback(page, key);
    if (descriptionArea) return descriptionArea;
  }
  const byLabel = await controlByLabel(page, spec.labels, spec.selectors);
  if (byLabel) return byLabel;
  return ticketFieldFallback(page, key);
}

function ticketFieldValueMatches(key, actual, expected) {
  if (key === 'contactPhone') return digits(actual) === digits(expected);
  if (key === 'startTime' || key === 'endTime') return digits(actual) === digits(expected);
  return normalize(actual) === normalize(expected);
}

async function logTicketControlInventory(page, reason) {
  const scopes = [page, ...page.frames().filter((frame) => frame !== page.mainFrame())];
  const inventory = [];
  for (const scope of scopes) {
    const controls = await scope.locator('input, textarea, select, button, [role="combobox"]').evaluateAll((elements) => (
      elements.slice(0, 120).map((element) => {
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        const labels = element.labels ? Array.from(element.labels).map((label) => label.innerText || '') : [];
        return {
          tag: element.tagName.toLowerCase(),
          type: element.getAttribute('type') || '',
          id: element.id || '',
          name: element.getAttribute('name') || '',
          aria: element.getAttribute('aria-label') || '',
          placeholder: element.getAttribute('placeholder') || '',
          labels: labels.join(' | ').slice(0, 180),
          visible: style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0,
          disabled: Boolean(element.disabled),
          readonly: Boolean(element.readOnly),
        };
      })
    )).catch(() => []);
    inventory.push({ frame: safeUrl(scope.url ? scope.url() : page.url()), controls });
  }
  await navigationLog('inventario_controles_formulario', { reason, inventory });
}

async function fillTicketField(page, key, value, timeout = 45_000) {
  const expected = cleanString(value);
  const deadline = Date.now() + timeout;
  let lastError = null;
  do {
    const control = await findTicketField(page, key);
    if (control) {
      try {
        const current = cleanString(await control.inputValue().catch(() => ''));
        if (!ticketFieldValueMatches(key, current, expected)) {
          await control.scrollIntoViewIfNeeded().catch(() => {});
          await control.fill(expected);
          await control.blur().catch(() => {});
        }
        await page.waitForTimeout(250);
        const verified = await findTicketField(page, key);
        const actual = verified ? cleanString(await verified.inputValue().catch(() => '')) : '';
        if (ticketFieldValueMatches(key, actual, expected)) {
          await navigationLog('campo_formulario_confirmado', { field: key, url: page.url() });
          return true;
        }
        lastError = new Error(`O campo ${key} perdeu o valor durante a atualizacao do formulario.`);
      } catch (error) {
        lastError = error;
      }
    }
    if (Date.now() < deadline) await page.waitForTimeout(400);
  } while (Date.now() < deadline);
  await logTicketControlInventory(page, `campo_${key}_nao_confirmado`);
  const detail = lastError ? ` ${cleanString(lastError.message)}` : '';
  throw new Error(`Campo do formulario nao encontrado ou nao confirmado: ${key}.${detail}`);
}

async function select2ContainerFor(control) {
  const container = control.locator(
    'xpath=ancestor-or-self::*[contains(concat(" ", normalize-space(@class), " "), " select2-container ")][1]',
  ).first();
  if (await container.count().catch(() => 0)) return container;
  return null;
}

async function dispatchSelect2Change(container, desiredText = '') {
  return container.evaluate((node, wantedText) => {
    let original = null;
    if (node.id && node.id.startsWith('s2id_')) {
      original = document.getElementById(node.id.slice(5));
    }
    if (!original) {
      let sibling = node.previousElementSibling;
      while (sibling && !['SELECT', 'INPUT'].includes(sibling.tagName)) sibling = sibling.previousElementSibling;
      original = sibling;
    }
    if (!original) return false;
    if (original.tagName === 'SELECT' && wantedText) {
      const plain = (value) => String(value || '')
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .toUpperCase()
        .replace(/[^A-Z0-9]+/g, ' ')
        .trim();
      const wanted = plain(wantedText);
      const option = Array.from(original.options || []).find((item) => plain(item.textContent).includes(wanted));
      if (option) original.value = option.value;
    }
    original.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }, desiredText).catch(() => false);
}

async function waitForSelect2Selection(page, labels, value, timeout = 10_000) {
  const deadline = Date.now() + timeout;
  do {
    const currentControl = await controlByLabel(
      page,
      labels,
      'select, input, button, [role="combobox"]',
    );
    const currentContainer = currentControl ? await select2ContainerFor(currentControl) : null;
    const selected = currentContainer
      ? cleanString(await currentContainer.locator('.select2-chosen').first().textContent().catch(() => ''))
      : '';
    if (selected && normalize(selected) === normalize(value)) return true;
    if (Date.now() < deadline) await page.waitForTimeout(250);
  } while (Date.now() < deadline);
  return false;
}

async function chooseSelect2(page, control, labels, value, forceCommit = false) {
  const container = await select2ContainerFor(control);
  if (!container) return false;

  const selectedText = cleanString(
    await container.locator('.select2-chosen').first().textContent().catch(() => ''),
  );
  const alreadySelected = selectedText && normalize(selectedText) === normalize(value);
  if (alreadySelected && !forceCommit) {
    await navigationLog('selecao_formulario_ja_confirmada', {
      field: String(labels[0]),
      selected: selectedText,
      url: page.url(),
    });
    return true;
  }
  if (alreadySelected && forceCommit && await dispatchSelect2Change(container, value)) {
    await navigationLog('selecao_select2_forcada_por_evento', {
      field: String(labels[0]),
      selected: selectedText,
      url: page.url(),
    });
    return true;
  }

  const trigger = await firstVisible([
    container.locator('a.select2-choice'),
    container.locator('.select2-choice'),
    container.locator('[role="combobox"]:not(.select2-focusser)'),
  ]);
  if (!trigger) {
    if (alreadySelected && forceCommit && await dispatchSelect2Change(container, value)) return true;
    return false;
  }
  let opened = false;
  try {
    await trigger.click({ timeout: 10_000 });
    opened = true;
  } catch {
    if (alreadySelected && forceCommit && await dispatchSelect2Change(container, value)) {
      await navigationLog('selecao_select2_forcada_por_evento', {
        field: String(labels[0]),
        selected: selectedText,
        url: page.url(),
      });
      return true;
    }
    throw new Error(`Nao foi possivel abrir a selecao ${labels[0]}.`);
  }

  const scopes = [page, ...page.frames().filter((frame) => frame !== page.mainFrame())];
  const optionLocators = [];
  for (const scope of scopes) {
    optionLocators.push(
      scope.locator('.select2-results li, .select2-result, [role="option"]').filter({ hasText: textRegex(value) }),
    );
    optionLocators.push(scope.getByRole('option', { name: textRegex(value) }));
  }

  const deadline = Date.now() + 15_000;
  let option = null;
  do {
    option = await firstVisible(optionLocators);
    if (option || Date.now() >= deadline) break;
    await page.waitForTimeout(300);
  } while (Date.now() < deadline);
  if (!option) {
    if (opened) await page.keyboard.press('Escape').catch(() => {});
    if (alreadySelected && forceCommit && await dispatchSelect2Change(container, value)) {
      await navigationLog('selecao_select2_forcada_por_evento', {
        field: String(labels[0]),
        selected: selectedText,
        url: page.url(),
      });
      return true;
    }
    throw new Error(`Opcao "${value}" nao encontrada em ${labels[0]}.`);
  }
  await option.click({ timeout: 10_000 });
  if (!await waitForSelect2Selection(page, labels, value, 10_000)) {
    throw new Error(`A selecao "${value}" nao foi confirmada pelo portal em ${labels[0]}.`);
  }
  await navigationLog('selecao_formulario_confirmada', {
    field: String(labels[0]),
    selected: value,
    control: 'select2',
    url: page.url(),
  });
  return true;
}

async function chooseByLabelOnce(page, labels, value, required = true, options = {}) {
  const control = await waitForControlByLabel(
    page,
    labels,
    'select, input, button, [role="combobox"]',
    required ? 30_000 : 3_000,
  );
  if (!control) {
    if (required) throw new Error(`Selecao nao encontrada: ${labels.join(' / ')}.`);
    return false;
  }
  if (await chooseSelect2(page, control, labels, value, Boolean(options.forceCommit))) return true;
  const tagName = await control.evaluate((element) => element.tagName.toLowerCase());
  if (tagName === 'select') {
    const deadline = Date.now() + 15_000;
    let wanted = '';
    do {
      const options = await control.locator('option').allTextContents().catch(() => []);
      wanted = options.find((option) => normalize(option).includes(normalize(value))) || '';
      if (wanted || Date.now() >= deadline) break;
      await page.waitForTimeout(300);
    } while (Date.now() < deadline);
    if (!wanted) throw new Error(`Opcao "${value}" nao encontrada em ${labels[0]}.`);
    await control.selectOption({ label: wanted });
    await navigationLog('selecao_formulario_confirmada', { field: String(labels[0]), url: page.url() });
    return true;
  }
  await control.click();
  const scopes = [page, ...page.frames().filter((frame) => frame !== page.mainFrame())];
  const optionLocators = [];
  for (const scope of scopes) {
    optionLocators.push(scope.getByRole('option', { name: textRegex(value) }));
    optionLocators.push(scope.getByText(textRegex(value), { exact: true }));
  }
  let option = null;
  const deadline = Date.now() + 15_000;
  do {
    option = await firstVisible(optionLocators);
    if (option || Date.now() >= deadline) break;
    await page.waitForTimeout(300);
  } while (Date.now() < deadline);
  if (!option) throw new Error(`Opcao "${value}" nao encontrada em ${labels[0]}.`);
  await option.click();
  await navigationLog('selecao_formulario_confirmada', { field: String(labels[0]), url: page.url() });
  return true;
}

async function chooseByLabel(page, labels, value, required = true, options = {}) {
  let lastError = null;
  const attempts = required ? 3 : 1;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await chooseByLabelOnce(page, labels, value, required, options);
    } catch (error) {
      lastError = error;
      await navigationLog('nova_tentativa_selecao_formulario', {
        field: String(labels[0]),
        attempt,
        error: cleanString(error?.message || error),
        url: page.url(),
      });
      await page.keyboard.press('Escape').catch(() => {});
      if (attempt < attempts) await page.waitForTimeout(500);
    }
  }
  throw lastError || new Error(`Nao foi possivel confirmar a selecao ${labels[0]}.`);
}

async function chooseTrainingYes(page) {
  const deadline = Date.now() + 30_000;
  do {
    const scopes = [page, ...page.frames().filter((frame) => frame !== page.mainFrame())];
    for (const scope of scopes) {
      const question = await firstVisible([
        scope.getByText(/t[eé]cnico realiza.*capacita[cç][aã]o|documenta[cç][aã]o para acessar/i),
      ]);
      if (question) {
        const container = question.locator('xpath=ancestor::*[.//input[@type="radio"]][1]');
        if (await container.count()) {
          const yesRadio = await firstVisible([
            container.locator('input[type="radio"][aria-label="Sim" i]'),
            container.getByRole('radio', { name: /^sim$/i }),
            container.getByLabel(/^sim$/i),
          ]);
          if (yesRadio) {
            try {
              // O ServiceNow posiciona um span visual sobre o input verdadeiro.
              // check({force:true}) altera o radio real e evita o bloqueio de clique.
              await yesRadio.check({ force: true, timeout: 5_000 });
              if (await yesRadio.isChecked().catch(() => false)) {
                await navigationLog('capacitacao_tecnico_sim_confirmada', { url: page.url() });
                return;
              }
            } catch {
              // O Angular pode substituir o radio enquanto ele e marcado. O
              // proximo ciclo busca o novo elemento e confirma seu estado.
            }
          }
        }
      }
      const yesRadio = await firstVisible([
        scope.locator('input[type="radio"][aria-label="Sim" i]'),
        scope.getByRole('radio', { name: /^sim$/i }),
        scope.getByLabel(/^sim$/i),
      ]);
      if (yesRadio) {
        try {
          await yesRadio.check({ force: true, timeout: 5_000 });
          if (await yesRadio.isChecked().catch(() => false)) {
            await navigationLog('capacitacao_tecnico_sim_confirmada', { url: page.url() });
            return;
          }
        } catch {
          // Rebusca no proximo ciclo se o formulario for reconstruido.
        }
      }
    }
    if (Date.now() < deadline) await page.waitForTimeout(350);
  } while (Date.now() < deadline);
  throw new Error('Nao encontrei a opcao Sim para acesso do tecnico.');
}

async function ticketControlDescriptor(control) {
  if (!control || !await isVisible(control)) return null;
  return control.evaluate((element) => {
    const newId = (prefix) => `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    if (!document.__roboVivoDocumentId) document.__roboVivoDocumentId = newId('document');
    if (!element.__roboVivoNodeId) element.__roboVivoNodeId = newId('node');
    return {
      tag: element.tagName.toLowerCase(),
      id: element.id || '',
      name: element.getAttribute('name') || '',
      type: element.getAttribute('type') || '',
      frameId: document.__roboVivoDocumentId,
      nodeId: element.__roboVivoNodeId,
      enabled: !element.disabled
        && element.getAttribute('aria-disabled') !== 'true'
        && !element.closest('fieldset[disabled], [inert]'),
      readonly: Boolean(element.readOnly),
    };
  }).catch(() => null);
}

async function dependentTicketFieldsSignature(page) {
  const keys = ['nickname', 'contactName', 'contactPhone', 'contactEmail', 'startTime', 'endTime', 'description'];
  const descriptors = [];
  for (const key of keys) {
    const control = await findTicketField(page, key);
    const descriptor = await ticketControlDescriptor(control);
    if (!descriptor || !descriptor.enabled || descriptor.readonly) return '';
    descriptors.push({ key, ...descriptor });
  }
  const frames = new Set(descriptors.map((item) => item.frameId));
  if (frames.size !== 1) return '';
  const uniqueControls = new Set(descriptors.map((item) => `${item.frameId}|${item.nodeId}`));
  if (uniqueControls.size !== descriptors.length) return '';
  return JSON.stringify(descriptors);
}

async function waitForDependentTicketFields(page, timeout = 45_000) {
  const deadline = Date.now() + timeout;
  let previous = '';
  let stablePolls = 0;
  do {
    const signature = await dependentTicketFieldsSignature(page);
    if (signature && signature === previous) {
      stablePolls += 1;
      if (stablePolls >= 5) {
        await navigationLog('campos_dependentes_estaveis', { url: page.url() });
        return true;
      }
    } else {
      previous = signature;
      stablePolls = signature ? 1 : 0;
    }
    if (Date.now() < deadline) await page.waitForTimeout(400);
  } while (Date.now() < deadline);
  await logTicketControlInventory(page, 'campos_dependentes_nao_apareceram');
  return false;
}

async function fillTicketForm(page, ticket) {
  emitStatus('Preenchendo os dados do chamado...');
  if (!(await waitForTicketFormReady(page, 45_000))) {
    throw new Error('O formulario tecnico abriu, mas seus campos nao ficaram disponiveis.');
  }
  // O ServiceNow mostra "Indisponibilidade" como valor visual antes de emitir
  // o evento que cria os campos dependentes. A confirmacao e forcada mesmo
  // quando o texto ja parece selecionado.
  await chooseByLabel(
    page,
    TICKET_FIELDS.occurrence.labels,
    ticket.occurrence,
    true,
    { forceCommit: true },
  );
  if (!await waitForDependentTicketFields(page, 45_000)) {
    await chooseByLabel(
      page,
      TICKET_FIELDS.occurrence.labels,
      ticket.occurrence,
      true,
      { forceCommit: true },
    );
    if (!await waitForDependentTicketFields(page, 45_000)) {
      throw new Error('A opcao de indisponibilidade foi confirmada, mas o portal nao liberou os campos do chamado.');
    }
  }

  const textFields = [
    ['nickname', ticket.vd],
    ['contactName', ticket.contactName],
    ['contactPhone', ticket.contactPhone],
    ['contactEmail', ticket.contactEmail],
    ['startTime', ticket.startTime],
    ['endTime', ticket.endTime],
  ];
  for (const [key, value] of textFields) await fillTicketField(page, key, value);
  await chooseByLabel(
    page,
    TICKET_FIELDS.sharePhone.labels,
    ticket.sharePhone,
  );
  if (normalize(ticket.trainingRequired) !== 'SIM') {
    throw new Error('Esta versao foi configurada para marcar Sim no acesso do tecnico.');
  }
  await chooseTrainingYes(page);
  await fillTicketField(page, 'description', ticket.description);

  // Um blur posterior pode reconstruir variaveis do formulario. Esta segunda
  // passagem nao redigita campos corretos; apenas recompõe qualquer valor que
  // tenha sido perdido e confirma tudo antes do envio.
  for (const [key, value] of textFields) await fillTicketField(page, key, value, 30_000);
  await fillTicketField(page, 'description', ticket.description, 30_000);
  await navigationLog('formulario_tecnico_totalmente_confirmado', { url: page.url() });
}

function previewFor(ticket, candidate) {
  return {
    vd: ticket.vd,
    store_name: ticket.storeName,
    problem: ticket.problem,
    product: candidate.product,
    identification: candidate.identification,
    service_id: candidate.service_id,
    address: candidate.address,
    city: candidate.city,
    contact_name: ticket.contactName,
    contact_phone: ticket.contactPhone,
    contact_email: ticket.contactEmail,
    start_time: ticket.startTime,
    end_time: ticket.endTime,
    description: ticket.description,
  };
}

async function submitOnce(page, onAboutToClick) {
  const deadline = Date.now() + 30_000;
  let sendButton = null;
  do {
    const candidate = await locatorForText(page, [/^enviar$/i], ['button']);
    if (candidate) {
      const enabled = await candidate.isEnabled().catch(() => false);
      const ariaDisabled = await candidate.getAttribute('aria-disabled').catch(() => null);
      if (enabled && ariaDisabled !== 'true') {
        sendButton = candidate;
        break;
      }
    }
    if (Date.now() < deadline) await page.waitForTimeout(350);
  } while (Date.now() < deadline);
  if (!sendButton) throw new Error('Botao Enviar nao ficou disponivel. O chamado nao foi submetido.');
  await sendButton.scrollIntoViewIfNeeded().catch(() => {});
  // Daqui em diante, nunca se tenta clicar novamente: um timeout pode significar
  // que o ServiceNow recebeu o chamado mesmo sem confirmar a navegacao.
  onAboutToClick();
  try {
    await sendButton.click({ timeout: 20_000, noWaitAfter: true });
  } catch (error) {
    const uncertain = new Error(`Nao foi possivel confirmar o clique em Enviar: ${error.message}`);
    uncertain.submissionUncertain = true;
    throw uncertain;
  }
  await page.waitForTimeout(1_500);
}

async function extractProtocol(page) {
  const body = await page.locator('body').innerText({ timeout: 5_000 }).catch(() => '');
  const protocolLabel = await labeledValue(page, ['Protocolo vivo', 'Protocolo Vivo']);
  const caseLabel = await labeledValue(page, ['Número do caso', 'Numero do caso']);
  const protocolMatch = body.match(/protocolo\s+vivo\s*[:#-]?\s*([0-9][0-9.\/-]*)/i);
  const caseMatch = body.match(/n[uú]mero\s+do\s+caso\s*[:#-]?\s*([A-Z]{1,5}\d{4,})/i)
    ?? body.match(/\b(CS\d{4,})\b/i);
  return {
    protocol: digits(protocolLabel || protocolMatch?.[1] || ''),
    caseNumber: cleanString(caseLabel || caseMatch?.[1] || '').replace(/\s+/g, ''),
  };
}

async function waitForProtocol(page, ticket, candidate = null) {
  emitStatus('Chamado enviado. Aguardando o Protocolo Vivo...');
  await clickText(page, [/detalhes do chamado/i], { optional: true });
  const deadline = Date.now() + ticket.protocolTimeoutMs;
  const portalLp = cleanString(candidate?.identification || ticket.identification || '');
  let last = { protocol: '', caseNumber: '' };
  while (Date.now() < deadline) {
    last = await extractProtocol(page);
    if (last.protocol) {
      emit('success', {
        request_id: ticket.requestId,
        vd: ticket.vd,
        lp: portalLp,
        identification: portalLp,
        protocol: last.protocol,
        protocolo_vivo: last.protocol,
        case_number: last.caseNumber,
        numero_caso: last.caseNumber,
        message: 'Chamado aberto com sucesso.',
      });
      return true;
    }
    await page.waitForTimeout(2_500);
    await clickText(page, [/detalhes do chamado/i], { optional: true }).catch(() => {});
  }
  emit('pending', {
    request_id: ticket.requestId,
    vd: ticket.vd,
    lp: portalLp,
    identification: portalLp,
    protocol: last.protocol || null,
    case_number: last.caseNumber || null,
    reason: 'protocol_timeout',
    message: `O chamado foi enviado, mas o protocolo nao ficou disponivel em ${Math.round(ticket.protocolTimeoutMs / 60_000)} minutos. Nao houve nova tentativa de envio.`,
  });
  return false;
}

async function runDryTicket(ticket, bus) {
  emitStatus('Modo de teste: dados validados sem acessar a internet.', { request_id: ticket.requestId });
  const candidate = {
    company: 'SIMULACAO',
    product: 'VPN IP MPLS',
    identification: ticket.identification || 'SIMULADO',
    service_id: ticket.serviceId || 'SIMULADO',
    address: ticket.address,
    city: ticket.city,
    strategy: ticket.identification ? 'identification' : ticket.serviceId ? 'service_id' : 'address_keyword',
    query: ticket.identification || ticket.serviceId || ticket.searchKeyword,
    confidence: 999,
  };
  emit('portal_candidate', {
    request_id: ticket.requestId,
    candidate,
    simulated: true,
    message: 'Candidato simulado. Em producao, esta etapa exige confirmacao.',
  });

  const interactive = ticket.raw?.dry_run_auto_confirm === false;
  if (interactive) {
    const candidateAnswer = await bus.waitFor((message) =>
      isConfirmationMessage(message, 'portal_candidate', ticket.requestId),
    );
    if (!confirmedFrom(candidateAnswer)) {
      emit('pending', { request_id: ticket.requestId, reason: 'candidate_rejected', simulated: true });
      return;
    }
  }
  const preview = previewFor(ticket, candidate);
  emit('final_preview', {
    request_id: ticket.requestId,
    preview,
    simulated: true,
    message: 'Revise os dados antes do envio.',
  });
  if (interactive) {
    const finalAnswer = await bus.waitFor((message) =>
      isConfirmationMessage(message, 'final_preview', ticket.requestId),
    );
    if (!confirmedFrom(finalAnswer)) {
      emit('pending', { request_id: ticket.requestId, reason: 'cancelled_before_submit', simulated: true });
      return;
    }
  }
  emitStatus('Modo de teste: simulando submissao unica...', { request_id: ticket.requestId });
  emit('success', {
    request_id: ticket.requestId,
    protocol: '0000000',
    protocolo_vivo: '0000000',
    case_number: 'CSSIMULADO',
    numero_caso: 'CSSIMULADO',
    simulated: true,
    message: 'Simulacao concluida; nenhum chamado real foi aberto.',
  });
}

async function runOpenTicket(rawRequest, bus, { dryRun = false, credentials = null } = {}) {
  const ticket = normalizeRequest(rawRequest);
  if (dryRun) {
    await runDryTicket(ticket, bus);
    return;
  }

  const logPath = await initializeNavigationLog(ticket.requestId);
  emitStatus(`Log de navegacao: ${logPath}`);
  const { context, page: initialPage } = await launchChrome({ headless: ticket.headless });
  await logOpenPages(context, 'chrome_iniciado', initialPage);
  let submitted = false;
  try {
    let page = await openServicePortal(initialPage, context, ticket, credentials);
    if (!page) return;
    activePage = page;
    const candidate = await choosePortalCandidate(page, ticket, bus);
    emitStatus('Circuito confirmado. Abrindo o formulario tecnico...');
    await openTicketFormForCandidate(page);
    await fillTicketForm(page, ticket);

    if (!ticket.allowSubmit) {
      emit('pending', {
        request_id: ticket.requestId,
        reason: 'submission_disabled',
        message: 'Operacao encerrada antes do envio; nenhum chamado foi aberto.',
      });
      return;
    }

    emitStatus('Formulario preenchido e conferido. Enviando o chamado uma unica vez...');
    await navigationLog('envio_automatico_autorizado_na_confirmacao_inicial', {
      request_id: ticket.requestId,
      url: page.url(),
    });
    await submitOnce(page, () => { submitted = true; });
    await waitForProtocol(page, ticket, candidate);
    await navigationLog('fluxo_concluido', { submitted, url: page.url() });
  } catch (error) {
    await navigationLog('erro', {
      error_type: cleanString(error?.name || 'Error'),
      message: redactDiagnosticText(error?.message || error),
      submitted,
      url: activePage?.url?.() || '',
    });
    if (submitted || error.submissionUncertain) {
      const artifacts = await captureErrorArtifacts(activePage, error).catch(() => ({}));
      emit('pending', {
        request_id: ticket.requestId,
        reason: 'submission_state_uncertain',
        message: 'O envio pode ter ocorrido, mas nao foi possivel confirmar. O robo nao tentou enviar novamente.',
        details: redactDiagnosticText(error.message),
        artifacts,
      });
      return;
    }
    error.artifacts = await captureErrorArtifacts(activePage, error).catch(() => ({}));
    throw error;
  } finally {
    await logOpenPages(context, 'encerrando_contexto', activePage).catch(() => {});
    await context.close().catch(() => {});
    activePage = null;
  }
}

function serializedGate() {
  let tail = Promise.resolve();
  return (operation) => {
    const current = tail.then(operation, operation);
    tail = current.catch(() => {});
    return current;
  };
}

function normalizeBatchRequest(rawBatch) {
  const source = Array.isArray(rawBatch) ? { tickets: rawBatch } : rawBatch;
  if (!source || typeof source !== 'object' || !Array.isArray(source.tickets)) {
    throw new Error('O lote precisa conter a lista "tickets".');
  }
  if (!source.tickets.length) throw new Error('O lote nao possui chamados.');
  const requested = Number.parseInt(source.concurrency, 10);
  const concurrency = Math.min(5, Math.max(1, Number.isFinite(requested) ? requested : 5));
  return {
    tickets: source.tickets.map((ticket) => normalizeRequest(ticket)),
    concurrency,
  };
}

async function runBatchTicket(
  ticket,
  bus,
  context,
  launcherRef,
  portalGate,
  submitGate,
  submitSchedule,
  credentials,
) {
  let page = null;
  let submitted = false;
  const requestMeta = { request_id: ticket.requestId, vd: ticket.vd };
  try {
    emitStatus(`[${ticket.vd}] Abrindo uma aba do Portal de Servicos...`, requestMeta);
    page = await portalGate(async () => {
      const launcher = launcherRef.page;
      const opened = await openServicePortal(launcher, context, ticket, credentials);
      if (opened === launcher) {
        // Alguns perfis redirecionam a própria aba em vez de criar uma nova.
        // Nesse caso, preservamos a aba redirecionada para este VD e criamos
        // uma nova aba lançadora, dentro da mesma sessão, para os próximos.
        launcherRef.page = await context.newPage();
      }
      return opened;
    });
    if (!page) {
      emit('error', { ...requestMeta, message: 'O Portal de Servicos nao abriu para este VD.' });
      return;
    }

    emitStatus(`[${ticket.vd}] Localizando e conferindo o circuito...`, requestMeta);
    const candidate = await choosePortalCandidate(page, ticket, bus);
    emitStatus(`[${ticket.vd}] Circuito confirmado. Preenchendo o formulario...`, requestMeta);
    await openTicketFormForCandidate(page);
    await fillTicketForm(page, ticket);

    if (!ticket.allowSubmit) {
      emit('pending', {
        ...requestMeta,
        reason: 'submission_disabled',
        message: 'Operacao encerrada antes do envio; nenhum chamado foi aberto.',
      });
      return;
    }

    // Até cinco formulários podem ser preparados/monitorados em paralelo. O
    // clique final fica levemente espaçado para não disparar cinco gravações no
    // mesmo instante na sessão compartilhada do portal.
    await submitGate(async () => {
      const waitMs = Math.max(0, submitSchedule.nextAt - Date.now());
      if (waitMs) await page.waitForTimeout(waitMs);
      emitStatus(`[${ticket.vd}] Enviando o chamado uma unica vez...`, requestMeta);
      await navigationLog('envio_lote_autorizado', {
        request_id: ticket.requestId,
        vd: ticket.vd,
        url: page.url(),
      });
      await submitOnce(page, () => { submitted = true; });
      submitSchedule.nextAt = Date.now() + 1_500;
    });
    emit('submitted', {
      ...requestMeta,
      message: `[${ticket.vd}] Chamado enviado. Aguardando o protocolo sem reenviar.`,
    });
    await waitForProtocol(page, ticket, candidate);
    await navigationLog('fluxo_lote_concluido', {
      request_id: ticket.requestId,
      vd: ticket.vd,
      submitted,
      url: page.url(),
    });
  } catch (error) {
    await navigationLog('erro_lote', {
      request_id: ticket.requestId,
      vd: ticket.vd,
      error_type: cleanString(error?.name || 'Error'),
      message: redactDiagnosticText(error?.message || error),
      submitted,
      url: page?.url?.() || '',
    }).catch(() => {});
    const artifacts = await captureErrorArtifacts(page, error).catch(() => ({}));
    const details = redactDiagnosticText(error?.message || error || 'Erro desconhecido.');
    if (error?.authRequired) {
      emit('auth_required', {
        ...requestMeta,
        message: details || 'A Vivo solicitou uma validacao adicional.',
        artifacts,
      });
    } else if (submitted || error?.submissionUncertain) {
      emit('pending', {
        ...requestMeta,
        reason: 'submission_state_uncertain',
        message: 'O envio pode ter ocorrido, mas nao foi possivel confirmar. O robo nao tentou enviar novamente.',
        details,
        artifacts,
      });
    } else {
      emit('error', { ...requestMeta, message: details, details, artifacts });
    }
  } finally {
    if (page && page !== launcherRef.page) await page.close().catch(() => {});
  }
}

async function runOpenBatch(rawBatch, bus, { dryRun = false, credentials = null } = {}) {
  const { tickets, concurrency } = normalizeBatchRequest(rawBatch);
  emit('batch_started', {
    total: tickets.length,
    concurrency,
    message: `Lote iniciado com ate ${concurrency} abas ativas na mesma sessao.`,
  });

  if (dryRun) {
    let cursor = 0;
    const dryWorkers = Array.from({ length: Math.min(concurrency, tickets.length) }, async () => {
      while (cursor < tickets.length) {
        const ticket = tickets[cursor];
        cursor += 1;
        await runDryTicket(ticket, bus);
      }
    });
    await Promise.all(dryWorkers);
    emit('batch_complete', { total: tickets.length, concurrency, message: 'Simulacao do lote concluida.' });
    return;
  }

  const logPath = await initializeNavigationLog(`lote-${Date.now()}`);
  emitStatus(`Log de navegacao do lote: ${logPath}`);
  const { context, page: initialPage } = await launchChrome({ headless: tickets.every((ticket) => ticket.headless) });
  const launcherRef = { page: initialPage };
  const portalGate = serializedGate();
  const submitGate = serializedGate();
  const submitSchedule = { nextAt: 0 };
  let cursor = 0;
  try {
    await logOpenPages(context, 'chrome_lote_iniciado', initialPage);
    const workers = Array.from({ length: Math.min(concurrency, tickets.length) }, async () => {
      while (cursor < tickets.length) {
        const ticket = tickets[cursor];
        cursor += 1;
        await runBatchTicket(
          ticket,
          bus,
          context,
          launcherRef,
          portalGate,
          submitGate,
          submitSchedule,
          credentials,
        );
      }
    });
    await Promise.all(workers);
    emit('batch_complete', {
      total: tickets.length,
      concurrency,
      message: 'Todas as abas do lote foram concluídas.',
    });
  } finally {
    await logOpenPages(context, 'encerrando_contexto_lote', launcherRef.page).catch(() => {});
    await context.close().catch(() => {});
    activePage = null;
  }
}

async function captureErrorArtifacts(page, error) {
  if (!page) return {};
  const locations = localPaths();
  await fsp.mkdir(locations.errors, { recursive: true });
  await pruneDiagnosticFiles(locations.errors, 'erro-', ERROR_ARTIFACT_RETENTION_DAYS);
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const base = path.join(locations.errors, `erro-${stamp}`);
  const screenshotPath = `${base}.png`;
  const privateFields = page.locator('input, textarea, select, [contenteditable="true"]');
  await page.screenshot({
    path: screenshotPath,
    fullPage: false,
    mask: [privateFields],
    maskColor: '#555555',
  }).catch(() => {});
  await fsp.writeFile(`${base}.txt`, redactDiagnosticText(error?.message || error), 'utf8').catch(() => {});
  return {
    screenshot: fs.existsSync(screenshotPath) ? screenshotPath : null,
    html: null,
    navigation_log: navigationLogPath && fs.existsSync(navigationLogPath) ? navigationLogPath : null,
  };
}

function commandAction(message) {
  return normalize(message?.action ?? message?.command ?? message?.mode);
}

async function main() {
  const args = process.argv.slice(2);
  const dryRun = args.includes('--dry-run');
  const credentials = takeCredentialsFromEnvironment();
  const bus = new JsonLineBus();
  try {
    if (args.includes('--auth')) {
      await runAuthentication({ dryRun, credentials });
      return;
    }
    if (args.includes('--open-ticket')) {
      const raw = await parseJsonRequest(argValue(args, '--open-ticket'));
      await runOpenTicket(raw, bus, { dryRun, credentials });
      return;
    }
    if (args.includes('--open-batch')) {
      const raw = await parseJsonRequest(argValue(args, '--open-batch'));
      await runOpenBatch(raw, bus, { dryRun, credentials });
      return;
    }

    emitStatus('Worker pronto. Aguardando comando JSONL...');
    const command = await bus.waitFor((message) => {
      const action = commandAction(message);
      return ['OPEN TICKET', 'OPEN_TICKET', 'OPEN CHAMADO', 'OPEN_LOGIN', 'OPEN LOGIN', 'AUTH'].includes(action);
    }, 24 * 60 * 60 * 1000);
    const action = commandAction(command);
    if (['OPEN_LOGIN', 'OPEN LOGIN', 'AUTH'].includes(action)) {
      await runAuthentication({ dryRun: dryRun || boolValue(command.dry_run, false), credentials });
      return;
    }
    const commandData = command.data ?? command.request ?? command.payload ?? command;
    const raw = commandData === command
      ? command
      : {
          ...commandData,
          request_id: commandData.request_id ?? commandData.requestId ?? command.request_id ?? command.requestId,
        };
    await runOpenTicket(raw, bus, { dryRun: dryRun || boolValue(command.dry_run, false), credentials });
  } finally {
    bus.close();
  }
}

main().catch(async (error) => {
  const artifacts = error?.artifacts ?? await captureErrorArtifacts(activePage, error).catch(() => ({}));
  const baseMessage = redactDiagnosticText(error?.message || error || 'Erro desconhecido.');
  const diagnosticHint = artifacts?.screenshot
    ? ` Diagnostico da tela salvo em: ${artifacts.screenshot}`
    : '';
  const logHint = artifacts?.navigation_log
    ? ` Log de navegacao: ${artifacts.navigation_log}`
    : (navigationLogPath ? ` Log de navegacao: ${navigationLogPath}` : '');
  emit('error', {
    message: `${baseMessage}${diagnosticHint}${logHint}`,
    details: baseMessage,
    artifacts,
  });
  process.exitCode = 1;
});
