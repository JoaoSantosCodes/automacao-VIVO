/**
 * Entrada Serverless (Vercel / Fluid Compute) da Interface Web do Robô Vivo.
 *
 * Serve os arquivos originais da interface (`Automação Vivo/arquivos/app/web`
 * e `assets`) e expõe os endpoints `/api/*` em Modo Simulação/Demonstração
 * seguro (sem expor planilhas corporativas, credenciais reais ou IPs internos).
 */

import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '..');

function locatePackageDir() {
  const direct = path.join(REPO_ROOT, 'Automação Vivo', 'arquivos');
  if (fs.existsSync(direct)) return direct;
  try {
    for (const entry of fs.readdirSync(REPO_ROOT, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const candidate = path.join(REPO_ROOT, entry.name, 'arquivos');
      if (fs.existsSync(candidate)) return candidate;
    }
  } catch {
    // Continua com fallback padrão se a leitura do diretório falhar.
  }
  return direct;
}

const PACKAGE_DIR = locatePackageDir();
const WEB_DIR = path.join(PACKAGE_DIR, 'app', 'web');
const ASSETS_DIR = path.join(PACKAGE_DIR, 'assets');
const MODELOS_DIR = path.join(PACKAGE_DIR, 'modelos');
const CONFIG_PATH = path.join(PACKAGE_DIR, 'configuracao.ini');

const COOKIE_NAME = 'robo_vivo_sessao';
const CSRF_HEADER = 'x-robo-vivo';
const MAX_BODY_BYTES = 64 * 1024;
const BATCH_CONCURRENCY = 5;
const BATCH_LIMIT = 50;
const PREPARED_BATCH_TTL_MS = 30 * 60 * 1000;
const SESSION_DIR = path.join(os.tmpdir(), 'robo-vivo-cloud-sessions');

const ISSUES = {
  link_interrompido: 'Link inoperante',
  perca_pacotes: 'Perda de pacotes',
};

const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.json': 'application/json; charset=utf-8',
};

/* ------------------------------------------------------------------ base demo fictícia */

const DEMO_STORES = {
  VD1517: {
    store: {
      nome: 'Filial Demonstração Paulista (Demo)',
      endereco: 'Av. Paulista, 1517',
      bairro: 'Bela Vista',
      cidade: 'São Paulo',
      uf: 'SP',
      cep: '01311-200',
      endereco_completo: 'Av. Paulista, 1517 — Bela Vista — São Paulo/SP',
      horario: '24 horas',
      hora_inicio: '00:00',
      hora_fim: '23:59',
      vinte_quatro_horas: true,
    },
    links: [
      {
        designacao: 'SPO-1517-MPLS-01',
        usuario: 'SVC-DEMO-151701',
        id_servico: 'SVC-DEMO-151701',
        operadora: 'VIVO',
        tipo_acesso: 'VPN IP MPLS',
        velocidade: '100 Mbps',
      },
    ],
  },
  VD1344: {
    store: {
      nome: 'Filial Demonstração Pinheiros (Demo)',
      endereco: 'Rua dos Pinheiros, 1344',
      bairro: 'Pinheiros',
      cidade: 'São Paulo',
      uf: 'SP',
      cep: '05422-002',
      endereco_completo: 'Rua dos Pinheiros, 1344 — Pinheiros — São Paulo/SP',
      horario: 'Seg a Dom das 07:00 às 23:00',
      hora_inicio: '07:00',
      hora_fim: '23:00',
      vinte_quatro_horas: false,
    },
    links: [
      {
        designacao: 'SPO-1344-MPLS-01',
        usuario: 'SVC-DEMO-134401',
        id_servico: 'SVC-DEMO-134401',
        operadora: 'VIVO',
        tipo_acesso: 'VPN IP MPLS',
        velocidade: '50 Mbps',
      },
      {
        designacao: 'SPO-1344-MPLS-02',
        usuario: 'SVC-DEMO-134402',
        id_servico: 'SVC-DEMO-134402',
        operadora: 'VIVO',
        tipo_acesso: 'VPN IP MPLS Contingência',
        velocidade: '50 Mbps',
      },
    ],
  },
  VD2574: {
    store: {
      nome: 'Filial Demonstração Copacabana (Demo)',
      endereco: 'Av. Nossa Senhora de Copacabana, 574',
      bairro: 'Copacabana',
      cidade: 'Rio de Janeiro',
      uf: 'RJ',
      cep: '22020-001',
      endereco_completo: 'Av. Nossa Senhora de Copacabana, 574 — Copacabana — Rio de Janeiro/RJ',
      horario: 'Seg a Sáb das 07:00 às 22:00',
      hora_inicio: '07:00',
      hora_fim: '22:00',
      vinte_quatro_horas: false,
    },
    links: [
      {
        designacao: 'RJO-2574-MPLS-01',
        usuario: 'SVC-DEMO-257401',
        id_servico: 'SVC-DEMO-257401',
        operadora: 'VIVO',
        tipo_acesso: 'VPN IP MPLS',
        velocidade: '50 Mbps',
      },
    ],
  },
  VD1627: {
    store: {
      nome: 'Filial Demonstração Savassi (Demo)',
      endereco: 'Av. Cristóvão Colombo, 627',
      bairro: 'Savassi',
      cidade: 'Belo Horizonte',
      uf: 'MG',
      cep: '30140-140',
      endereco_completo: 'Av. Cristóvão Colombo, 627 — Savassi — Belo Horizonte/MG',
      horario: 'Seg a Dom das 07:00 às 22:00',
      hora_inicio: '07:00',
      hora_fim: '22:00',
      vinte_quatro_horas: false,
    },
    links: [
      {
        designacao: 'BHZ-1627-MPLS-01',
        usuario: 'SVC-DEMO-162701',
        id_servico: 'SVC-DEMO-162701',
        operadora: 'VIVO',
        tipo_acesso: 'VPN IP MPLS',
        velocidade: '50 Mbps',
      },
    ],
  },
  VD003: {
    store: {
      nome: 'Filial Demonstração Centro (Demo)',
      endereco: 'Rua Direita, 103',
      bairro: 'Sé',
      cidade: 'São Paulo',
      uf: 'SP',
      cep: '01002-001',
      endereco_completo: 'Rua Direita, 103 — Sé — São Paulo/SP',
      horario: 'Seg a Sáb das 07:00 às 21:00',
      hora_inicio: '07:00',
      hora_fim: '21:00',
      vinte_quatro_horas: false,
    },
    links: [
      {
        designacao: 'SPO-0003-MPLS-01',
        usuario: 'SVC-DEMO-000301',
        id_servico: 'SVC-DEMO-000301',
        operadora: 'VIVO',
        tipo_acesso: 'VPN IP MPLS',
        velocidade: '100 Mbps',
      },
    ],
  },
};

/* ------------------------------------------------------------------ utilitários e INI */

class ApiError extends Error {
  constructor(status, message, extra = {}) {
    super(message);
    this.status = status;
    this.extra = extra;
  }
}

function nowIso() {
  return new Date().toISOString();
}

function maskCpf(cpf) {
  const digits = String(cpf || '').replace(/\D/g, '');
  if (digits.length !== 11) return 'cadastrado';
  return `***.***.${digits.slice(6, 9)}-${digits.slice(9)}`;
}

function normalizeCpf(value) {
  const digits = String(value || '').replace(/\D/g, '');
  if (digits.length !== 11) throw new ApiError(400, 'Informe o CPF com 11 números.');
  return digits;
}

function loadIniConfig() {
  const defaults = {
    portal: {
      url_portal: 'https://mve.vivo.com.br/sec/dashboard',
      produto_filtro: 'mpls',
      produto_esperado: 'VPN IP MPLS',
      ocorrencia: 'Indisponibilidade',
      navegador_oculto: 'sim',
      tempo_protocolo_segundos: '300',
    },
    contato: {
      nome: 'central de comando',
      telefone: '1132747527',
      email: 'central.comando@dpsp.com.br',
    },
    seguranca: {
      janela_duplicidade_horas: '24',
    },
  };
  try {
    if (!fs.existsSync(CONFIG_PATH)) return defaults;
    const raw = fs.readFileSync(CONFIG_PATH, 'utf8').replace(/^\uFEFF/, '');
    let section = '';
    for (const line of raw.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith(';') || trimmed.startsWith('#')) continue;
      if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
        section = trimmed.slice(1, -1).trim().toLowerCase();
        if (!defaults[section]) defaults[section] = {};
        continue;
      }
      const eq = trimmed.indexOf('=');
      if (eq > 0 && section) {
        const key = trimmed.slice(0, eq).trim().toLowerCase();
        const val = trimmed.slice(eq + 1).trim();
        defaults[section][key] = val;
      }
    }
  } catch {
    // Mantém valores padrão em caso de erro de leitura.
  }
  return defaults;
}

const CONFIG = loadIniConfig();

function readTemplate(issue) {
  const fileName = issue === 'perca_pacotes' ? 'perca_pacotes.txt' : 'link_interrompido.txt';
  const filePath = path.join(MODELOS_DIR, fileName);
  try {
    if (fs.existsSync(filePath)) {
      return fs.readFileSync(filePath, 'utf8').replace(/^\uFEFF/, '');
    }
  } catch {
    // Usa fallback abaixo caso não exista.
  }
  const reason = issue === 'perca_pacotes' ? 'com perca de pacotes' : 'com o link interrompido';
  return (
    `Prezados,\n\nFavor verificar a filial: {NOME_LOJA}\n\nSe encontra ${reason}.\n\n` +
    `Filial: {VD} - {NOME_LOJA}\n\nReset: Sim\n\nDesignação: {DESIGNACAO}\n\n` +
    `Horário Funcionamento: {HORARIO}\n\nGrato!\n`
  );
}

function cleanVd(value) {
  const digits = String(value || '').replace(/\D/g, '');
  if (!digits) throw new ApiError(400, 'Informe o VD da loja.');
  const normalized = String(Number.parseInt(digits, 10));
  return `VD${normalized.padStart(3, '0')}`;
}

function parseVdJobs(value, limit = BATCH_LIMIT) {
  const jobs = [];
  const seen = new Map();
  const text = String(value || '').replace(/\\_/g, '_');
  const hostRe = /^((?:DSP|DPA)_(?:VD_?)?(\d{1,7})_MPLS_VIVO)$/i;
  const vdRe = /^(?:VD)?0*(\d{1,7})$/i;

  for (const part of text.split(/[\s,;]+/)) {
    const token = part.trim().replace(/^[\[\](){}.:|]+|[\[\](){}.:|]+$/g, '');
    if (!token) continue;
    let vd = '';
    let host = '';
    const hostMatch = token.match(hostRe);
    if (hostMatch) {
      vd = cleanVd(hostMatch[2]);
      host = hostMatch[1].toUpperCase().replace('_VD_', '_').replace('_VD', '_');
    } else {
      const vdMatch = token.match(vdRe);
      if (!vdMatch) continue;
      vd = cleanVd(vdMatch[1]);
    }
    const key = vd.toLowerCase();
    if (seen.has(key)) {
      const prev = seen.get(key);
      if (prev && host && prev !== host) {
        throw new ApiError(400, `O ${vd} apareceu com dois hosts diferentes (${prev} e ${host}).`);
      }
      if (!prev && host) {
        seen.set(key, host);
        const existing = jobs.find((item) => item.vd.toLowerCase() === key);
        if (existing) existing.zabbix_host = host;
      }
      continue;
    }
    seen.set(key, host);
    jobs.push({ vd, zabbix_host: host });
  }

  if (!jobs.length) throw new ApiError(400, 'Informe pelo menos um VD.');
  if (jobs.length > limit) {
    throw new ApiError(
      400,
      limit === 1
        ? 'Informe apenas um VD na consulta individual (use a aba Lote para vários VDs).'
        : `O lote permite no máximo ${limit} VDs por vez.`,
    );
  }
  return jobs;
}

function ignoredBatchTokens(value) {
  const ignored = [];
  const text = String(value || '').replace(/\\_/g, '_');
  const hostRe = /^(?:DSP|DPA)_(?:VD_?)?\d{1,7}_MPLS_VIVO$/i;
  const vdRe = /^(?:VD)?0*\d{1,7}$/i;
  for (const part of text.split(/[\s,;]+/)) {
    const token = part.trim().replace(/^[\[\](){}.:|]+|[\[\](){}.:|]+$/g, '');
    if (!token || hostRe.test(token) || vdRe.test(token)) continue;
    if (!ignored.includes(token)) ignored.push(token);
  }
  return ignored.slice(0, 20);
}

/* ------------------------------------------------------------------ estado por sessão (/tmp + memória) */

const memorySessions = new Map();
const sseSubscribers = new Map(); // sessionId -> Set<res>

function defaultSessionState() {
  return {
    operation: null,
    prompts: {},
    preparedBatches: {},
    log: [
      {
        time: nowIso(),
        level: 'info',
        message:
          'Ambiente de demonstração online (Vercel) iniciado em MODO SIMULAÇÃO. Experimente os VDs: VD1517, VD1344, VD2574, VD1627 ou VD003.',
      },
    ],
    history: [],
    zabbixAudit: [],
    vivoCreds: null,
    zabbixCreds: null,
  };
}

function loadSession(sessionId) {
  const file = path.join(SESSION_DIR, `${sessionId}.json`);
  try {
    if (fs.existsSync(file)) {
      const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
      memorySessions.set(sessionId, parsed);
      return parsed;
    }
  } catch {
    // Recria sessão se arquivo temporário estiver corrompido.
  }
  if (memorySessions.has(sessionId)) return memorySessions.get(sessionId);
  const created = defaultSessionState();
  saveSession(sessionId, created);
  return created;
}

function saveSession(sessionId, session) {
  memorySessions.set(sessionId, session);
  try {
    fs.mkdirSync(SESSION_DIR, { recursive: true });
    const file = path.join(SESSION_DIR, `${sessionId}.json`);
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(session), 'utf8');
    fs.renameSync(tmp, file);
  } catch {
    // Em caso de falha de escrita no /tmp, mantém em memória na instância ativa.
  }
}

function isBusy(session) {
  return Boolean(session.operation && session.operation.phase !== 'concluido');
}

function snapshot(session) {
  let operation = null;
  if (session.operation) {
    operation = {};
    for (const [k, v] of Object.entries(session.operation)) {
      if (!k.startsWith('_')) operation[k] = v;
    }
    operation.items = (session.operation.items || []).map((item) => {
      const clean = {};
      for (const [k, v] of Object.entries(item)) {
        if (!k.startsWith('_')) clean[k] = v;
      }
      return clean;
    });
  }
  return {
    busy: isBusy(session),
    simulation: true,
    operation,
    prompts: Object.values(session.prompts || {}),
    log: (session.log || []).slice(-150),
  };
}

function broadcast(sessionId, event) {
  const subs = sseSubscribers.get(sessionId);
  if (!subs || !subs.size) return;
  const payload = `data: ${JSON.stringify(event)}\n\n`;
  for (const client of [...subs]) {
    try {
      client.write(payload);
    } catch {
      subs.delete(client);
    }
  }
}

function addLog(sessionId, session, message, level = 'info') {
  const entry = { time: nowIso(), message: String(message || '').trim(), level };
  if (!entry.message) return;
  session.log = [...(session.log || []).slice(-399), entry];
  broadcast(sessionId, { type: 'log', entry });
}

function publishState(sessionId, session) {
  saveSession(sessionId, session);
  broadcast(sessionId, { type: 'state', state: snapshot(session) });
}

/* ------------------------------------------------------------------ regras de negócio (simulação fiel) */

function validateIssue(raw) {
  const issue = String(raw || '').trim();
  if (!ISSUES[issue]) {
    throw new ApiError(400, 'Escolha o tipo de problema: link inoperante ou perda de pacotes.');
  }
  return issue;
}

function lookupDemoStore(vd) {
  const entry = DEMO_STORES[vd];
  if (!entry) {
    throw new ApiError(
      422,
      `${vd} não consta na base fictícia de demonstração. Experimente: VD1517 (24h), VD1344 (2 circuitos), VD2574, VD1627 ou VD003.`,
    );
  }
  return entry;
}

function buildDescription(vd, store, link, issue) {
  const horario = store.vinte_quatro_horas
    ? '24 horas (00:00 às 23:59)'
    : `${store.horario} (${store.hora_inicio} às ${store.hora_fim})`;
  return readTemplate(issue)
    .replaceAll('{NOME_LOJA}', store.nome)
    .replaceAll('{VD}', vd)
    .replaceAll('{DESIGNACAO}', link.designacao)
    .replaceAll('{HORARIO}', horario)
    .trim();
}

function hasRecentDuplicate(session, vd, designacao) {
  const windowHours = Number.parseInt(CONFIG.seguranca.janela_duplicidade_horas || '24', 10) || 24;
  const cutoff = Date.now() - windowHours * 3600 * 1000;
  return (session.history || []).some(
    (row) =>
      row.vd === vd &&
      row.designacao === designacao &&
      new Date(row.data_hora).getTime() >= cutoff,
  );
}

function prepareSingle(session, entrada, issue, designacaoInput) {
  const jobs = parseVdJobs(entrada, 1);
  const { vd, zabbix_host } = jobs[0];
  const { store, links } = lookupDemoStore(vd);
  const designacao = String(designacaoInput || '').trim();
  if (!designacao) throw new ApiError(400, 'Escolha o circuito (designação) do chamado.');
  const link = links.find((item) => item.designacao === designacao);
  if (!link) throw new ApiError(422, `A designação ${designacao} não pertence a ${vd}.`);

  const hora_inicio = store.vinte_quatro_horas ? '00:00' : store.hora_inicio;
  const hora_fim = store.vinte_quatro_horas ? '23:59' : store.hora_fim;
  const descricao = buildDescription(vd, store, link, issue);
  const duplicate = hasRecentDuplicate(session, vd, link.designacao);

  return {
    vd,
    zabbix_host,
    store,
    link,
    hora_inicio,
    hora_fim,
    descricao,
    duplicate,
    request_id: crypto.randomUUID(),
  };
}

function makeCandidatePrompt(prepared, position = null, total = null) {
  const id = crypto.randomUUID();
  return {
    id,
    stage: 'portal_candidate',
    request_id: prepared.request_id,
    vd: prepared.vd,
    loja: prepared.store.nome,
    position,
    total,
    simulated: true,
    created_at: nowIso(),
    candidate: {
      empresa: 'DROGARIA SAO PAULO S/A (SIMULAÇÃO)',
      produto: `${CONFIG.portal.produto_esperado} · ${prepared.link.velocidade}`,
      endereco: prepared.store.endereco_completo,
      cidade: prepared.store.cidade,
      identificacao: prepared.link.designacao,
      id_servico: prepared.link.id_servico,
      velocidade: prepared.link.velocidade,
      contrato: 'CTR-SIM-884920',
    },
    expected: {
      vd: prepared.vd,
      store_name: prepared.store.nome,
      address: prepared.store.endereco_completo,
      city: prepared.store.cidade,
      identification: prepared.link.designacao,
      service_id: prepared.link.id_servico,
    },
  };
}

function makePreviewPrompt(op) {
  const id = crypto.randomUUID();
  return {
    id,
    stage: 'final_preview',
    request_id: op.request_id,
    vd: op.vd,
    loja: op.loja,
    position: null,
    total: null,
    simulated: true,
    created_at: nowIso(),
    preview: {
      vd: op.vd,
      store_name: op.loja,
      endereco: op._store.endereco_completo,
      identificacao: op.designacao,
      hora_inicio: op._hora_inicio,
      hora_fim: op._hora_fim,
      contact_name: CONFIG.contato.nome,
      contact_phone: CONFIG.contato.telefone,
      contact_email: CONFIG.contato.email,
      descricao: op._descricao,
    },
  };
}

function recordDemoHistory(session, { vd, loja, issue, designacao, protocol, caseNumber, zabbixHost }) {
  const timestamp = nowIso();
  const host = zabbixHost || `DSP_${vd.replace(/^VD/, '')}_MPLS_VIVO`;
  session.history = [
    ...(session.history || []),
    {
      data_hora: timestamp,
      vd,
      loja,
      problema: issue,
      designacao,
      protocolo_vivo: protocol,
      numero_caso: caseNumber,
      status: 'ABERTO',
      status_zabbix: 'SIMULAÇÃO',
      detalhe_zabbix: 'Demonstração online na Vercel (sem conexão à rede interna).',
      host_zabbix: host,
    },
  ];
  session.zabbixAudit = [
    ...(session.zabbixAudit || []),
    {
      data_hora: timestamp,
      vd,
      host,
      protocolo_vivo: protocol,
      status_zabbix: 'SIMULAÇÃO',
      detalhe: 'Reconhecimento simulado (ambiente em nuvem sem acesso a 172.16.2.250).',
    },
  ];
}

function simulatedIdsFor(vd) {
  const digits = vd.replace(/\D/g, '').padStart(4, '0');
  const suffix = String(Math.floor(100 + Math.random() * 900));
  return {
    protocol: `2026${digits}${suffix}`,
    caseNumber: `CS-SIM-${digits}`,
  };
}

/* ------------------------------------------------------------------ handlers da API */

function apiLookup(body) {
  const jobs = parseVdJobs(body.entrada, 1);
  const { vd, zabbix_host } = jobs[0];
  const { store, links } = lookupDemoStore(vd);
  return { vd, zabbix_host, store, links };
}

function apiPreview(session, body) {
  const issue = validateIssue(body.problema);
  const prepared = prepareSingle(session, body.entrada, issue, body.designacao);
  const windowHours = Number.parseInt(CONFIG.seguranca.janela_duplicidade_horas || '24', 10) || 24;
  return {
    vd: prepared.vd,
    zabbix_host: prepared.zabbix_host,
    duplicado: prepared.duplicate,
    janela_duplicidade_horas: windowHours,
    descricao: prepared.descricao,
    confirmacao: {
      loja: `${prepared.vd} — ${prepared.store.nome}`,
      host: prepared.zabbix_host || 'Será localizado pelo VD, exigindo um único incidente ativo',
      endereco: `${prepared.store.endereco} — ${prepared.store.cidade}/${prepared.store.uf}`,
      designacao: prepared.link.designacao,
      horario: `${prepared.hora_inicio} às ${prepared.hora_fim}`,
      problema: ISSUES[issue],
    },
  };
}

function apiStartTicket(sessionId, session, body) {
  if (isBusy(session)) throw new ApiError(409, 'Há uma operação em andamento. Aguarde ela terminar.');
  const issue = validateIssue(body.problema);
  const prepared = prepareSingle(session, body.entrada, issue, body.designacao);
  if (prepared.duplicate && !body.aceitar_duplicidade) {
    throw new ApiError(
      409,
      'Há um chamado recente registrado para este VD e circuito. Confirme que deseja abrir mesmo assim.',
      { action: 'confirmar_duplicidade' },
    );
  }
  const prompt = makeCandidatePrompt(prepared);
  session.prompts = { [prompt.id]: prompt };
  session.operation = {
    id: crypto.randomUUID(),
    kind: 'ticket',
    phase: 'executando',
    simulation: true,
    started_at: nowIso(),
    finished_at: '',
    message: 'Circuito localizado (simulação). Aguardando sua confirmação...',
    vd: prepared.vd,
    zabbix_host: prepared.zabbix_host,
    issue,
    issue_label: ISSUES[issue],
    loja: prepared.store.nome,
    designacao: prepared.link.designacao,
    request_id: prepared.request_id,
    submission_seen: false,
    terminal_seen: false,
    result: null,
    items: [],
    _store: prepared.store,
    _link: prepared.link,
    _hora_inicio: prepared.hora_inicio,
    _hora_fim: prepared.hora_fim,
    _descricao: prepared.descricao,
  };
  addLog(sessionId, session, `Automação iniciada para ${prepared.vd} em MODO SIMULAÇÃO (nada será enviado à Vivo).`);
  publishState(sessionId, session);
  return { ok: true, operation_id: session.operation.id };
}

function apiPrepareBatch(sessionId, session, body) {
  const issue = validateIssue(body.problema);
  const jobs = parseVdJobs(body.entrada, BATCH_LIMIT);
  const ignorados = ignoredBatchTokens(body.entrada);
  const items = [];
  const failures = [];

  jobs.forEach((job, idx) => {
    const position = idx + 1;
    try {
      const { store, links } = lookupDemoStore(job.vd);
      const link = links[0];
      const prepared = prepareSingle(session, job.zabbix_host || job.vd, issue, link.designacao);
      items.push({
        position,
        vd: job.vd,
        zabbix_host: job.zabbix_host,
        loja: store.nome,
        designacao: link.designacao,
        circuitos: links.length,
        duplicado: prepared.duplicate,
        _prepared: prepared,
      });
    } catch (err) {
      failures.push({
        position,
        vd: job.vd,
        zabbix_host: job.zabbix_host,
        message: err.message || 'Erro ao consultar loja na base demo.',
      });
    }
  });

  const batchId = crypto.randomUUID();
  const now = Date.now();
  session.preparedBatches = Object.fromEntries(
    Object.entries(session.preparedBatches || {}).filter(([, val]) => now - val.created < PREPARED_BATCH_TTL_MS),
  );
  session.preparedBatches[batchId] = { created: now, issue, items, failures };
  saveSession(sessionId, session);

  return {
    batch_id: batchId,
    issue,
    issue_label: ISSUES[issue],
    items: items.map(({ _prepared, ...pub }) => pub),
    failures,
    ignorados,
    concorrencia: BATCH_CONCURRENCY,
  };
}

function pumpBatchPrompts(session) {
  const op = session.operation;
  if (!op || op.kind !== 'batch' || op.phase === 'concluido') return;
  const activePrompts = Object.keys(session.prompts || {}).length;
  const slots = Math.max(0, BATCH_CONCURRENCY - activePrompts);
  const queued = op.items.filter((item) => item.status === 'NA FILA').slice(0, slots);

  for (const item of queued) {
    item.status = 'AGUARDANDO CONFIRMAÇÃO';
    item.message = 'Aguardando confirmação do circuito pelo operador.';
    const prompt = makeCandidatePrompt(item._prepared, item.position, op._validTotal);
    session.prompts[prompt.id] = prompt;
  }

  const remaining = op.items.some((item) =>
    ['NA FILA', 'EM ANDAMENTO', 'AGUARDANDO CONFIRMAÇÃO'].includes(item.status),
  );
  if (!remaining && Object.keys(session.prompts || {}).length === 0) {
    op.phase = 'concluido';
    op.finished_at = nowIso();
    op.message = 'Simulação do lote concluída; nenhum chamado real foi aberto.';
  } else {
    op.message = `Processando lote em simulação (${Object.keys(session.prompts || {}).length} confirmação(ões) aguardando)...`;
  }
}

function apiStartBatch(sessionId, session, body) {
  if (isBusy(session)) throw new ApiError(409, 'Há uma operação em andamento. Aguarde ela terminar.');
  const batchId = String(body.batch_id || '');
  const prepared = (session.preparedBatches || {})[batchId];
  delete (session.preparedBatches || {})[batchId];
  if (!prepared) throw new ApiError(410, 'A revisão do lote expirou. Revise o lote novamente.');
  if (!prepared.items.length) throw new ApiError(422, 'Nenhum VD válido ficou disponível para processamento.');

  const items = [
    ...prepared.items.map((src) => ({
      position: src.position,
      vd: src.vd,
      zabbix_host: src.zabbix_host,
      loja: src.loja,
      designacao: src.designacao,
      status: 'NA FILA',
      protocol: '',
      case_number: '',
      lp: src.designacao,
      message: '',
      zabbix_status: '',
      zabbix_detail: '',
      request_id: src._prepared.request_id,
      _prepared: src._prepared,
    })),
    ...prepared.failures.map((fail) => ({
      position: fail.position,
      vd: fail.vd,
      zabbix_host: fail.zabbix_host,
      loja: '',
      designacao: '',
      status: 'ERRO NA BASE',
      protocol: '',
      case_number: '',
      lp: '',
      message: fail.message,
      zabbix_status: 'NÃO SOLICITADO',
      zabbix_detail: '',
      request_id: '',
    })),
  ].sort((a, b) => a.position - b.position);

  session.prompts = {};
  session.operation = {
    id: crypto.randomUUID(),
    kind: 'batch',
    phase: 'executando',
    simulation: true,
    started_at: nowIso(),
    finished_at: '',
    message: 'Iniciando lote em modo simulação...',
    issue: prepared.issue,
    issue_label: ISSUES[prepared.issue],
    total: items.length,
    _validTotal: prepared.items.length,
    items,
  };

  pumpBatchPrompts(session);
  addLog(sessionId, session, `Lote iniciado em MODO SIMULAÇÃO com ${prepared.items.length} loja(s) válida(s).`);
  publishState(sessionId, session);
  return { ok: true, operation_id: session.operation.id };
}

function apiRespond(sessionId, session, body) {
  const promptId = String(body.prompt_id || '');
  const confirmed = body.confirmado === true;
  const prompt = (session.prompts || {})[promptId];
  if (!prompt) throw new ApiError(410, 'Esta confirmação não está mais pendente.');
  delete session.prompts[promptId];

  const op = session.operation;
  if (!op) throw new ApiError(409, 'Nenhuma operação ativa.');

  if (op.kind === 'ticket') {
    if (prompt.stage === 'portal_candidate') {
      if (!confirmed) {
        op.phase = 'concluido';
        op.finished_at = nowIso();
        op.terminal_seen = true;
        op.message = 'Operação encerrada sem envio.';
        op.result = {
          status: 'NÃO ENVIADO',
          message: 'Circuito recusado pelo operador. Nenhum chamado foi enviado.',
        };
        addLog(sessionId, session, `[${op.vd}] Circuito recusado pelo operador.`);
      } else {
        const nextPrompt = makePreviewPrompt(op);
        session.prompts[nextPrompt.id] = nextPrompt;
        op.message = 'Revisão final pronta. Confirme para concluir a simulação.';
        addLog(sessionId, session, `[${op.vd}] Circuito confirmado. Aguardando revisão final.`);
      }
    } else {
      if (!confirmed) {
        op.phase = 'concluido';
        op.finished_at = nowIso();
        op.terminal_seen = true;
        op.message = 'Envio cancelado na revisão final.';
        op.result = {
          status: 'NÃO ENVIADO',
          message: 'Envio cancelado pelo operador antes da submissão.',
        };
        addLog(sessionId, session, `[${op.vd}] Envio cancelado pelo operador.`);
      } else {
        const { protocol, caseNumber } = simulatedIdsFor(op.vd);
        op.phase = 'concluido';
        op.finished_at = nowIso();
        op.submission_seen = true;
        op.terminal_seen = true;
        op.message = 'Simulação concluída com sucesso.';
        op.result = {
          status: 'ABERTO',
          protocol,
          case_number: caseNumber,
          lp: op.designacao,
          message: 'Simulação concluída; nenhum chamado real foi aberto na Vivo.',
          zabbix_status: 'SIMULAÇÃO',
          zabbix_detail: 'Demonstração online na Vercel (sem acesso à rede corporativa 172.16.2.250).',
          support_phrase: '',
        };
        recordDemoHistory(session, {
          vd: op.vd,
          loja: op.loja,
          issue: op.issue,
          designacao: op.designacao,
          protocol,
          caseNumber,
          zabbixHost: op.zabbix_host,
        });
        addLog(sessionId, session, `[${op.vd}] Simulação concluída — Protocolo fictício ${protocol} (${caseNumber}).`);
      }
    }
  } else if (op.kind === 'batch') {
    const item = op.items.find((entry) => entry.request_id === prompt.request_id);
    if (item) {
      if (!confirmed) {
        item.status = 'NÃO ENVIADO';
        item.message = 'Circuito recusado pelo operador.';
        item.zabbix_status = 'NÃO SOLICITADO';
        addLog(sessionId, session, `[${item.vd}] Circuito recusado no lote.`);
      } else {
        const { protocol, caseNumber } = simulatedIdsFor(item.vd);
        item.status = 'ABERTO';
        item.protocol = protocol;
        item.case_number = caseNumber;
        item.message = 'Simulação concluída; nenhum chamado real foi aberto.';
        item.zabbix_status = 'SIMULAÇÃO';
        item.zabbix_detail = 'Reconhecimento simulado (Vercel).';
        recordDemoHistory(session, {
          vd: item.vd,
          loja: item.loja,
          issue: op.issue,
          designacao: item.designacao,
          protocol,
          caseNumber,
          zabbixHost: item.zabbix_host,
        });
        addLog(sessionId, session, `[${item.vd}] Simulado com sucesso (${protocol}).`);
      }
    }
    pumpBatchPrompts(session);
  }

  publishState(sessionId, session);
  return { ok: true };
}

function apiCancel(sessionId, session) {
  if (!isBusy(session)) throw new ApiError(409, 'Não há operação em andamento.');
  const op = session.operation;
  session.prompts = {};
  op.phase = 'concluido';
  op.finished_at = nowIso();
  op.message = 'Operação interrompida pelo operador.';
  if (op.kind === 'ticket') {
    op.result = { status: 'NÃO ENVIADO', message: 'Automação interrompida pelo operador.' };
  } else {
    for (const item of op.items || []) {
      if (['NA FILA', 'EM ANDAMENTO', 'AGUARDANDO CONFIRMAÇÃO'].includes(item.status)) {
        item.status = 'NÃO ENVIADO';
        item.message = 'Interrompido pelo operador.';
        item.zabbix_status = 'NÃO SOLICITADO';
      }
    }
  }
  addLog(sessionId, session, 'Interrupção solicitada pelo operador.', 'warning');
  publishState(sessionId, session);
  return { ok: true };
}

function apiClearOperation(sessionId, session) {
  if (isBusy(session)) throw new ApiError(409, 'A operação ainda está em andamento.');
  session.operation = null;
  session.prompts = {};
  publishState(sessionId, session);
  return { ok: true };
}

function apiHistory(session) {
  const windowHours = Number.parseInt(CONFIG.seguranca.janela_duplicidade_horas || '24', 10) || 24;
  const chamados = [...(session.history || [])].reverse().slice(0, 300);
  const zabbix = [...(session.zabbixAudit || [])].reverse().slice(0, 300);
  return {
    chamados,
    zabbix,
    total_chamados: (session.history || []).length,
    total_zabbix: (session.zabbixAudit || []).length,
    janela_duplicidade_horas: windowHours,
  };
}

function statTemplateInfo(label, fileName) {
  const full = path.join(MODELOS_DIR, fileName);
  try {
    const stat = fs.statSync(full);
    return {
      nome: label,
      arquivo: fileName,
      existe: true,
      tamanho_kb: Math.max(1, Math.round(stat.size / 1024)),
      atualizado_em: stat.mtime.toISOString(),
    };
  } catch {
    return { nome: label, arquivo: fileName, existe: false, tamanho_kb: 0, atualizado_em: '' };
  }
}

function apiSettings(session) {
  const now = nowIso();
  const vivo = session.vivoCreds
    ? { disponivel: true, configurado: true, cpf: maskCpf(session.vivoCreds.cpf) }
    : { disponivel: true, configurado: false, cpf: '' };
  const zabbix = session.zabbixCreds
    ? {
        disponivel: true,
        configurado: true,
        api_url: session.zabbixCreds.api_url,
        automatico: Boolean(session.zabbixCreds.automatico),
      }
    : { disponivel: true, configurado: false, api_url: '', automatico: true };

  return {
    simulation: true,
    credenciais_vivo: vivo,
    credenciais_zabbix: zabbix,
    zabbix_url_padrao: 'http://172.16.2.250/api_jsonrpc.php',
    bases: [
      {
        nome: 'Relação de lojas (Demo em memória)',
        arquivo: 'demo_lojas_ficticias (5 filiais)',
        existe: true,
        tamanho_kb: 4,
        atualizado_em: now,
      },
      {
        nome: 'Inventário de links (Demo em memória)',
        arquivo: 'demo_links_mpls (6 circuitos)',
        existe: true,
        tamanho_kb: 4,
        atualizado_em: now,
      },
      statTemplateInfo('Modelo: link inoperante', 'link_interrompido.txt'),
      statTemplateInfo('Modelo: perda de pacotes', 'perca_pacotes.txt'),
    ],
    portal: {
      url: CONFIG.portal.url_portal,
      produto_esperado: CONFIG.portal.produto_esperado,
      ocorrencia: CONFIG.portal.ocorrencia,
      navegador_oculto: CONFIG.portal.navegador_oculto,
      tempo_protocolo_segundos: CONFIG.portal.tempo_protocolo_segundos,
    },
    contato: {
      nome: CONFIG.contato.nome,
      telefone: CONFIG.contato.telefone,
      email: CONFIG.contato.email,
    },
    janela_duplicidade_horas: Number.parseInt(CONFIG.seguranca.janela_duplicidade_horas || '24', 10) || 24,
    limite_lote: BATCH_LIMIT,
    abas_paralelas: BATCH_CONCURRENCY,
  };
}

/* ------------------------------------------------------------------ HTTP / roteamento */

function securityHeaders(extra = {}) {
  return {
    'Content-Security-Policy':
      "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
    'Cache-Control': 'no-store',
    ...extra,
  };
}

function parseCookies(header) {
  const out = {};
  for (const part of String(header || '').split(';')) {
    const idx = part.indexOf('=');
    if (idx > 0) {
      out[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
    }
  }
  return out;
}

function resolveSession(req) {
  const cookies = parseCookies(req.headers.cookie);
  const existing = cookies[COOKIE_NAME];
  if (existing && /^[a-zA-Z0-9_-]{16,80}$/.test(existing)) {
    return { sessionId: existing, isNew: false };
  }
  return { sessionId: crypto.randomBytes(24).toString('base64url'), isNew: true };
}

function buildCookieHeader(req, sessionId) {
  const proto = String(req.headers['x-forwarded-proto'] || '').toLowerCase();
  const secure = proto === 'https' ? '; Secure' : '';
  return `${COOKIE_NAME}=${sessionId}; HttpOnly; SameSite=Lax; Path=/${secure}`;
}

function sendBytes(req, res, status, body, contentType, extraHeaders = {}) {
  const headers = securityHeaders({
    'Content-Type': contentType,
    'Content-Length': String(body.length),
    ...extraHeaders,
  });
  res.writeHead(status, headers);
  if ((req.method || 'GET').toUpperCase() === 'HEAD') {
    res.end();
  } else {
    res.end(body);
  }
}

function sendJson(req, res, status, data, extraHeaders = {}) {
  sendBytes(req, res, status, Buffer.from(JSON.stringify(data), 'utf8'), 'application/json; charset=utf-8', extraHeaders);
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const contentType = String(req.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
    if (contentType !== 'application/json') {
      reject(new ApiError(415, 'Envie os dados em JSON.'));
      return;
    }
    const chunks = [];
    let total = 0;
    req.on('data', (chunk) => {
      total += chunk.length;
      if (total > MAX_BODY_BYTES) {
        reject(new ApiError(413, 'Requisição grande demais.'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      try {
        const raw = Buffer.concat(chunks).toString('utf8') || '{}';
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          reject(new ApiError(400, 'JSON inválido.'));
          return;
        }
        resolve(parsed);
      } catch {
        reject(new ApiError(400, 'JSON inválido.'));
      }
    });
    req.on('error', () => reject(new ApiError(400, 'Falha ao ler requisição.')));
  });
}

function originAllowed(req) {
  const origin = req.headers.origin;
  if (!origin) return true;
  try {
    const originHost = new URL(origin).host.toLowerCase();
    const host = String(req.headers['x-forwarded-host'] || req.headers.host || '')
      .split(',')[0]
      .trim()
      .toLowerCase();
    return Boolean(host && originHost === host);
  } catch {
    return false;
  }
}

function resolveRoutePath(req) {
  const url = new URL(req.url || '/', 'http://localhost');
  const rewritten = url.searchParams.get('__robo_path');
  if (rewritten) {
    return rewritten.startsWith('/') ? rewritten : `/${rewritten}`;
  }
  return url.pathname || '/';
}

function serveStaticFile(req, res, baseDir, relativeName, cookieHeader) {
  const safeBase = path.resolve(baseDir);
  const target = path.resolve(safeBase, relativeName);
  if (!target.startsWith(`${safeBase}${path.sep}`) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
    sendJson(req, res, 404, { error: 'Não encontrado.' });
    return;
  }
  const ext = path.extname(target).toLowerCase();
  const contentType = MIME_TYPES[ext] || 'application/octet-stream';
  const extra = cookieHeader ? { 'Set-Cookie': cookieHeader } : {};
  sendBytes(req, res, 200, fs.readFileSync(target), contentType, extra);
}

function handleSse(req, res, sessionId, session, cookieHeader) {
  const headers = securityHeaders({
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
    ...(cookieHeader ? { 'Set-Cookie': cookieHeader } : {}),
  });
  res.writeHead(200, headers);
  res.write(`retry: 2000\ndata: ${JSON.stringify({ type: 'state', state: snapshot(session) })}\n\n`);

  if (!sseSubscribers.has(sessionId)) sseSubscribers.set(sessionId, new Set());
  const subs = sseSubscribers.get(sessionId);
  subs.add(res);

  const heartbeat = setInterval(() => {
    try {
      res.write(': ping\n\n');
    } catch {
      clearInterval(heartbeat);
    }
  }, 15_000);

  // Encerra proativamente antes do limite de duração serverless; o EventSource reconecta sozinho.
  const maxLifetime = setTimeout(() => {
    cleanup();
    try {
      res.end();
    } catch {
      // Conexão já fechada.
    }
  }, 180_000);

  function cleanup() {
    clearInterval(heartbeat);
    clearTimeout(maxLifetime);
    const set = sseSubscribers.get(sessionId);
    if (set) {
      set.delete(res);
      if (!set.size) sseSubscribers.delete(sessionId);
    }
  }

  req.on('close', cleanup);
  req.on('aborted', cleanup);
}

export default async function handler(req, res) {
  const method = (req.method || 'GET').toUpperCase();
  const routePath = resolveRoutePath(req);
  const { sessionId, isNew } = resolveSession(req);
  const cookieHeader = isNew ? buildCookieHeader(req, sessionId) : '';
  const extraCookie = cookieHeader ? { 'Set-Cookie': cookieHeader } : {};

  if (method === 'GET' || method === 'HEAD') {
    if (routePath === '/' || routePath === '/index.html') {
      serveStaticFile(req, res, WEB_DIR, 'index.html', cookieHeader);
      return;
    }
    if (routePath === '/favicon.ico') {
      serveStaticFile(req, res, ASSETS_DIR, 'app_icon.ico', cookieHeader);
      return;
    }
    if (routePath.startsWith('/static/')) {
      serveStaticFile(req, res, WEB_DIR, routePath.slice('/static/'.length), cookieHeader);
      return;
    }
    if (routePath.startsWith('/assets/')) {
      const assetName = routePath.slice('/assets/'.length);
      if (!['app_icon.png', 'app_icon.ico', 'vivo_wordmark.png'].includes(assetName)) {
        sendJson(req, res, 404, { error: 'Não encontrado.' });
        return;
      }
      serveStaticFile(req, res, ASSETS_DIR, assetName, cookieHeader);
      return;
    }

    const session = loadSession(sessionId);
    if (routePath === '/api/state') {
      sendJson(req, res, 200, snapshot(session), extraCookie);
      return;
    }
    if (routePath === '/api/events') {
      handleSse(req, res, sessionId, session, cookieHeader);
      return;
    }
    if (routePath === '/api/history') {
      sendJson(req, res, 200, apiHistory(session), extraCookie);
      return;
    }
    if (routePath === '/api/settings') {
      sendJson(req, res, 200, apiSettings(session), extraCookie);
      return;
    }

    sendJson(req, res, 404, { error: 'Não encontrado.' });
    return;
  }

  if (method !== 'POST') {
    sendJson(req, res, 405, { error: 'Método não permitido.' });
    return;
  }

  if (!originAllowed(req) || req.headers[CSRF_HEADER] !== '1') {
    sendJson(req, res, 403, { error: 'Sessão ou origem inválida. Recarregue a página.' });
    return;
  }

  const session = loadSession(sessionId);
  try {
    const body = await readJsonBody(req);
    let result;
    switch (routePath) {
      case '/api/lookup':
        result = apiLookup(body);
        break;
      case '/api/ticket/preview':
        result = apiPreview(session, body);
        break;
      case '/api/ticket/start':
        result = apiStartTicket(sessionId, session, body);
        break;
      case '/api/batch/prepare':
        result = apiPrepareBatch(sessionId, session, body);
        break;
      case '/api/batch/start':
        result = apiStartBatch(sessionId, session, body);
        break;
      case '/api/respond':
        result = apiRespond(sessionId, session, body);
        break;
      case '/api/cancel':
        result = apiCancel(sessionId, session);
        break;
      case '/api/operation/clear':
        result = apiClearOperation(sessionId, session);
        break;
      case '/api/credentials/vivo': {
        if (isBusy(session)) throw new ApiError(409, 'Aguarde a operação atual terminar.');
        const cpf = normalizeCpf(body.cpf);
        if (!String(body.senha || '').trim()) throw new ApiError(400, 'Informe a senha (fictícia para simulação).');
        session.vivoCreds = { cpf };
        addLog(sessionId, session, 'Credenciais simuladas registradas apenas nesta sessão temporária.');
        publishState(sessionId, session);
        result = { ok: true };
        break;
      }
      case '/api/credentials/vivo/remove':
        session.vivoCreds = null;
        addLog(sessionId, session, 'Credenciais simuladas removidas da sessão.');
        publishState(sessionId, session);
        result = { ok: true };
        break;
      case '/api/credentials/zabbix': {
        if (isBusy(session)) throw new ApiError(409, 'Aguarde a operação atual terminar.');
        const apiUrl = String(body.api_url || '').trim();
        const token = String(body.token || '').trim() || (session.zabbixCreds ? 'mantido' : '');
        if (!apiUrl || !token) throw new ApiError(400, 'Informe URL e token (fictícios na simulação).');
        session.zabbixCreds = { api_url: apiUrl, automatico: body.automatico !== false };
        addLog(sessionId, session, 'Configuração simulada do Zabbix salva nesta sessão.');
        publishState(sessionId, session);
        result = { ok: true };
        break;
      }
      case '/api/credentials/zabbix/test':
        result = { ok: true, versao: '6.4.0 (Simulação Vercel)' };
        break;
      case '/api/credentials/zabbix/remove':
        session.zabbixCreds = null;
        addLog(sessionId, session, 'Configuração simulada do Zabbix removida da sessão.');
        publishState(sessionId, session);
        result = { ok: true };
        break;
      case '/api/shutdown':
        if (isBusy(session)) {
          throw new ApiError(409, 'Há uma operação em andamento. Interrompa ou aguarde antes de encerrar.');
        }
        broadcast(sessionId, { type: 'shutdown' });
        memorySessions.delete(sessionId);
        try {
          fs.rmSync(path.join(SESSION_DIR, `${sessionId}.json`), { force: true });
        } catch {
          // Ignora falha ao remover arquivo temporário.
        }
        result = { ok: true };
        break;
      default:
        throw new ApiError(404, 'Não encontrado.');
    }
    sendJson(req, res, 200, result, extraCookie);
  } catch (err) {
    if (err instanceof ApiError) {
      sendJson(req, res, err.status, { error: err.message, ...err.extra }, extraCookie);
    } else {
      sendJson(req, res, 500, { error: 'Erro interno na simulação web.' }, extraCookie);
    }
  }
}
