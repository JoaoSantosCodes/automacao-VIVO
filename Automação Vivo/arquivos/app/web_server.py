"""Interface web local do Robô de Chamados Vivo.

O servidor escuta somente em 127.0.0.1 e atende apenas o navegador aberto pelo
próprio robô: cada execução gera um token aleatório que vira um cookie
``HttpOnly``/``SameSite=Strict``. Toda requisição de alteração exige também um
cabeçalho próprio, o que impede que outra página aberta no navegador dispare
ações no robô.

As regras de negócio (duplicidade, histórico, Zabbix, credenciais e montagem do
chamado) vêm do ``main.py`` por injeção do módulo ``core``; esta camada só
orquestra o worker Node e traduz os eventos para a página.
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import hmac
import http.server
import json
import logging
import mimetypes
import os
import queue
import secrets
import socketserver
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_BODY_BYTES = 64 * 1024
COOKIE_NAME = "robo_vivo_sessao"
CSRF_HEADER = "X-Robo-Vivo"
SESSION_FILE_NAME = "web_session.json"
ISSUES = {"link_interrompido": "Link inoperante", "perca_pacotes": "Perda de pacotes"}
BATCH_CONCURRENCY = 5
BATCH_LIMIT = 50
PREPARED_BATCH_TTL_SECONDS = 30 * 60
LOG_LIMIT = 400
NOT_SUBMITTED_REASONS = {"candidate_rejected", "cancelled_before_submit", "submission_disabled"}
SUCCESS_EVENTS = {"success", "result", "completed", "concluido", "login_ready", "auth_ok"}
INFO_EVENTS = {"status", "progress", "progresso", "ready", "batch_started", "batch_complete"}


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def mask_cpf(cpf: str) -> str:
    digits = "".join(char for char in str(cpf or "") if char.isdigit())
    if len(digits) != 11:
        return "cadastrado"
    return f"***.***.{digits[6:9]}-{digits[9:]}"


def session_file_path() -> Path:
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if base:
        return Path(base) / "RoboVivo" / SESSION_FILE_NAME
    # Fora do Windows (testes), usa uma pasta temporária por usuário.
    user = str(os.getuid()) if hasattr(os, "getuid") else "usuario"
    return Path(tempfile.gettempdir()) / f"RoboVivo-{user}" / SESSION_FILE_NAME


# ---------------------------------------------------------------------------
# Eventos para a página (Server-Sent Events)
# ---------------------------------------------------------------------------


class EventHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self.last_activity = time.monotonic()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        channel: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.append(channel)
            self.last_activity = time.monotonic()
        return channel

    def unsubscribe(self, channel: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)
            self.last_activity = time.monotonic()

    def publish(self, event: Mapping[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for channel in subscribers:
            try:
                channel.put_nowait(dict(event))
            except queue.Full:
                # Uma aba travada não pode segurar o robô; ela recebe o estado
                # completo de novo quando reconectar.
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------


class RoboService:
    """Estado da interface web e ponte com o worker Node."""

    def __init__(
        self,
        core: Any,
        config: Any,
        *,
        simulation: bool = False,
        credentials: Any = None,
        zabbix_factory: Optional[Callable[[Any], Any]] = None,
        node_and_worker: Optional[Callable[[], tuple[str, Path]]] = None,
    ) -> None:
        self.core = core
        self.config = config
        self.simulation = simulation
        self._credentials = credentials
        self.zabbix_factory = zabbix_factory or core.zabbix_client_for
        self.node_and_worker = node_and_worker or core.find_node_and_worker
        self.hub = EventHub()
        self.lock = threading.RLock()
        self.adapter: Any = None
        self.operation: Optional[dict[str, Any]] = None
        self.prompts: dict[str, dict[str, Any]] = {}
        self.log: collections.deque[dict[str, str]] = collections.deque(maxlen=LOG_LIMIT)
        self.worker: Any = None
        self.prepared_batches: dict[str, dict[str, Any]] = {}

    # -- utilidades -------------------------------------------------------

    @property
    def credentials(self) -> Any:
        if self._credentials is None:
            self._credentials = self.core.credential_api()
        return self._credentials

    def _adapter(self) -> Any:
        with self.lock:
            if self.adapter is None:
                self.adapter = self.core.DataAdapter(self.config)
            return self.adapter

    def busy(self) -> bool:
        with self.lock:
            return bool(self.operation and self.operation.get("phase") != "concluido")

    def add_log(self, message: str, level: str = "info") -> None:
        text = str(message or "").strip()
        if not text:
            return
        entry = {"time": now_iso(), "message": text, "level": level}
        with self.lock:
            self.log.append(entry)
        self.hub.publish({"type": "log", "entry": entry})

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            operation = None
            if self.operation:
                operation = {
                    key: value
                    for key, value in self.operation.items()
                    if not key.startswith("_")
                }
                operation["items"] = [
                    {key: value for key, value in item.items() if not key.startswith("_")}
                    for item in self.operation.get("items", [])
                ]
            return {
                "busy": self.busy(),
                "simulation": self.simulation,
                "operation": operation,
                "prompts": list(self.prompts.values()),
                "log": list(self.log)[-150:],
            }

    def publish_state(self) -> None:
        self.hub.publish({"type": "state", "state": self.snapshot()})

    def friendly(self, exc: BaseException) -> str:
        return self.core.friendly_error(exc)

    # -- consultas --------------------------------------------------------

    def lookup(self, entrada: str) -> dict[str, Any]:
        try:
            vd = self.core.clean_vd(entrada)
            zabbix_host = self.core.extract_zabbix_host(entrada)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        try:
            store, links = self._adapter().lookup(vd)
        except Exception as exc:
            logging.info("Consulta web sem resultado para %s: %s", vd, self.friendly(exc))
            raise ApiError(422, self.friendly(exc)) from None
        return {
            "vd": vd,
            "zabbix_host": zabbix_host,
            "store": self._store_view(store),
            "links": [self._link_view(link) for link in links],
        }

    def _store_view(self, store: Mapping[str, Any]) -> dict[str, Any]:
        field = self.core.field
        return {
            "nome": field(store, "nome", "nome_loja"),
            "endereco": field(store, "endereco"),
            "bairro": field(store, "bairro"),
            "cidade": field(store, "cidade"),
            "uf": field(store, "uf"),
            "cep": field(store, "cep"),
            "endereco_completo": field(store, "endereco_completo"),
            "horario": field(store, "horario_script", "horario"),
            "hora_inicio": field(store, "hora_inicio"),
            "hora_fim": field(store, "hora_fim", "hora_termino"),
            "vinte_quatro_horas": bool(field(store, "vinte_quatro_horas", default=False)),
        }

    def _link_view(self, link: Mapping[str, Any]) -> dict[str, Any]:
        field = self.core.field
        return {
            "designacao": field(link, "designacao", "identificacao"),
            "usuario": field(link, "usuario", "id_servico"),
            "operadora": field(link, "operadora"),
            "tipo_acesso": field(link, "tipo_acesso", "produto"),
            "velocidade": field(link, "velocidade"),
        }

    @staticmethod
    def _issue(value: Any) -> str:
        issue = str(value or "").strip()
        if issue not in ISSUES:
            raise ApiError(400, "Escolha o tipo de problema: link inoperante ou perda de pacotes.")
        return issue

    def _prepare_single(self, entrada: str, issue: str, designacao: str) -> dict[str, Any]:
        info = self.lookup(entrada)
        vd = info["vd"]
        designacao = str(designacao or "").strip()
        if not designacao:
            raise ApiError(400, "Escolha o circuito (designação) do chamado.")
        try:
            store, links = self._adapter().lookup(vd)
            link = next(
                (
                    item
                    for item in links
                    if str(self.core.field(item, "designacao", "identificacao")) == designacao
                ),
                None,
            )
            if link is None:
                raise ApiError(422, f"A designação {designacao} não pertence a {vd}.")
            ticket = self._adapter().build_ticket(vd, issue, designacao)
            payload = self.core.build_ticket_payload(self.config, ticket, store, link, vd, issue)
        except ApiError:
            raise
        except Exception as exc:
            logging.exception("Falha ao preparar chamado web")
            raise ApiError(422, self.friendly(exc)) from None
        return {
            "vd": vd,
            "zabbix_host": info["zabbix_host"],
            "store": store,
            "link": link,
            "ticket": ticket,
            "payload": payload,
            "duplicate": self.core.recent_duplicate(self.config, payload, fallback_vd=vd),
        }

    def preview(self, entrada: str, issue: Any, designacao: str) -> dict[str, Any]:
        issue = self._issue(issue)
        prepared = self._prepare_single(entrada, issue, designacao)
        return self._preview_view(prepared, issue)

    def _preview_view(self, prepared: Mapping[str, Any], issue: str) -> dict[str, Any]:
        field = self.core.field
        payload = prepared["payload"]
        store = payload.get("loja", {})
        link = payload.get("circuito", {})
        return {
            "vd": prepared["vd"],
            "zabbix_host": prepared["zabbix_host"],
            "duplicado": bool(prepared["duplicate"]),
            "janela_duplicidade_horas": self.config.getint(
                "seguranca", "janela_duplicidade_horas", fallback=24
            ),
            "descricao": str(payload.get("descricao") or ""),
            "confirmacao": {
                "loja": f"{prepared['vd']} — {field(store, 'nome', 'nome_loja')}",
                "host": prepared["zabbix_host"]
                or "Será localizado pelo VD, exigindo um único incidente ativo",
                "endereco": f"{field(store, 'endereco')} — {field(store, 'cidade')}/{field(store, 'uf')}",
                "designacao": str(field(link, "designacao", "identificacao")),
                "horario": f"{payload.get('hora_inicio')} às {payload.get('hora_fim')}",
                "problema": ISSUES[issue],
            },
        }

    # -- chamado individual ----------------------------------------------

    def _require_idle(self) -> None:
        if self.busy():
            raise ApiError(409, "Há uma operação em andamento. Aguarde ela terminar.")

    def _load_vivo_credentials(self) -> Any:
        if self.simulation:
            return None
        try:
            credentials = self.credentials.carregar()
        except Exception as exc:
            raise ApiError(503, f"Não foi possível ler o acesso automático: {self.friendly(exc)}") from None
        if not credentials:
            raise ApiError(
                412,
                "Cadastre o CPF e a senha do Portal Vivo em Configurações antes de abrir chamados.",
                action="configurar_vivo",
            )
        return credentials

    def start_ticket(
        self, entrada: str, issue: Any, designacao: str, accept_duplicate: bool
    ) -> dict[str, Any]:
        issue = self._issue(issue)
        with self.lock:
            self._require_idle()
            prepared = self._prepare_single(entrada, issue, designacao)
            if prepared["duplicate"] and not accept_duplicate:
                raise ApiError(
                    409,
                    "Há um chamado recente registrado para este VD e circuito. "
                    "Confirme que deseja abrir mesmo assim.",
                    action="confirmar_duplicidade",
                )
            credentials = self._load_vivo_credentials()
            try:
                node, worker_path = self.node_and_worker()
            except Exception as exc:
                raise ApiError(503, self.friendly(exc)) from None
            payload = dict(prepared["payload"])
            if self.simulation:
                payload["dry_run_auto_confirm"] = False
            field = self.core.field
            self.prompts.clear()
            self.operation = {
                "id": str(uuid.uuid4()),
                "kind": "ticket",
                "phase": "executando",
                "simulation": self.simulation,
                "started_at": now_iso(),
                "finished_at": "",
                "message": "Iniciando acesso ao portal Vivo...",
                "vd": prepared["vd"],
                "zabbix_host": prepared["zabbix_host"],
                "issue": issue,
                "issue_label": ISSUES[issue],
                "loja": str(field(prepared["store"], "nome", "nome_loja")),
                "designacao": str(field(prepared["link"], "designacao", "identificacao")),
                "request_id": payload["request_id"],
                "submission_seen": False,
                "terminal_seen": False,
                "result": None,
                "items": [],
                "_store": prepared["store"],
                "_link": prepared["link"],
                "_token": object(),
            }
            token = self.operation["_token"]
            command = [node, str(worker_path), "--open-ticket", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))]
            if self.simulation:
                command.append("--dry-run")
        self.add_log(
            f"Automação iniciada para {prepared['vd']}"
            + (" em MODO SIMULAÇÃO (nada será enviado à Vivo)." if self.simulation else ". Nenhum reenvio automático será realizado.")
        )
        self._spawn(command, credentials, token)
        self.publish_state()
        return {"ok": True, "operation_id": self.operation["id"]}

    # -- lote --------------------------------------------------------------

    def prepare_batch(self, entrada: str, issue: Any) -> dict[str, Any]:
        issue = self._issue(issue)
        try:
            jobs = self.core.parse_vd_jobs(entrada, limit=BATCH_LIMIT)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        ignored = self._ignored_batch_tokens(entrada)
        items: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        field = self.core.field
        adapter = self._adapter()
        for position, job in enumerate(jobs, start=1):
            vd = job["vd"]
            zabbix_host = job.get("zabbix_host", "")
            try:
                store, links = adapter.lookup(vd)
                if not links:
                    raise RuntimeError("Nenhum circuito foi encontrado na base.")
                link = links[0]
                designation = str(field(link, "designacao", "identificacao", default=""))
                ticket = adapter.build_ticket(vd, issue, designation)
                payload = self.core.build_ticket_payload(self.config, ticket, store, link, vd, issue)
                items.append(
                    {
                        "position": position,
                        "vd": vd,
                        "zabbix_host": zabbix_host,
                        "loja": str(field(store, "nome", "nome_loja", default="Loja sem nome")),
                        "designacao": designation,
                        "circuitos": len(links),
                        "duplicado": self.core.recent_duplicate(self.config, payload, fallback_vd=vd),
                        "_store": store,
                        "_link": link,
                        "_payload": payload,
                    }
                )
            except Exception as exc:
                failures.append(
                    {
                        "position": position,
                        "vd": vd,
                        "zabbix_host": zabbix_host,
                        "message": self.friendly(exc),
                    }
                )
        batch_id = str(uuid.uuid4())
        with self.lock:
            self._expire_prepared_batches()
            self.prepared_batches = {
                batch_id: {
                    "created": time.monotonic(),
                    "issue": issue,
                    "items": items,
                    "failures": failures,
                }
            }
        public_items = [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in items
        ]
        return {
            "batch_id": batch_id,
            "issue": issue,
            "issue_label": ISSUES[issue],
            "items": public_items,
            "failures": failures,
            "ignorados": ignored,
            "concorrencia": BATCH_CONCURRENCY,
        }

    @staticmethod
    def _ignored_batch_tokens(entrada: str) -> list[str]:
        """Itens que não parecem VD nem host, para avisar o operador."""

        import re

        ignored: list[str] = []
        text = (entrada or "").replace("\\_", "_")
        host = re.compile(r"(?:DSP|DPA)_(?:VD_?)?\d{1,7}_MPLS_VIVO", re.IGNORECASE)
        for part in re.split(r"[\s,;]+", text):
            token = part.strip().strip("[](){}.:|")
            if not token:
                continue
            if host.fullmatch(token) or re.fullmatch(r"(?:VD)?0*\d{1,7}", token, re.IGNORECASE):
                continue
            if token not in ignored:
                ignored.append(token)
        return ignored[:20]

    def _expire_prepared_batches(self) -> None:
        limit = time.monotonic() - PREPARED_BATCH_TTL_SECONDS
        for key in [key for key, value in self.prepared_batches.items() if value["created"] < limit]:
            self.prepared_batches.pop(key, None)

    def start_batch(self, batch_id: str) -> dict[str, Any]:
        with self.lock:
            self._require_idle()
            self._expire_prepared_batches()
            prepared = self.prepared_batches.pop(str(batch_id or ""), None)
            if not prepared:
                raise ApiError(410, "A revisão do lote expirou. Revise o lote novamente.")
            if not prepared["items"]:
                raise ApiError(422, "Nenhum VD válido ficou disponível para processamento.")
            credentials = self._load_vivo_credentials()
            try:
                node, worker_path = self.node_and_worker()
            except Exception as exc:
                raise ApiError(503, self.friendly(exc)) from None
            items: list[dict[str, Any]] = []
            tickets: list[dict[str, Any]] = []
            for source in prepared["items"]:
                payload = dict(source["_payload"])
                if self.simulation:
                    payload["dry_run_auto_confirm"] = False
                tickets.append(payload)
                items.append(
                    {
                        "position": source["position"],
                        "vd": source["vd"],
                        "zabbix_host": source["zabbix_host"],
                        "loja": source["loja"],
                        "designacao": source["designacao"],
                        "status": "NA FILA",
                        "protocol": "",
                        "case_number": "",
                        "lp": "",
                        "message": "",
                        "zabbix_status": "",
                        "zabbix_detail": "",
                        "request_id": payload["request_id"],
                        "_recorded": False,
                        "_submitted": False,
                        "_store": source["_store"],
                        "_link": source["_link"],
                    }
                )
            for failure in prepared["failures"]:
                items.append(
                    {
                        "position": failure["position"],
                        "vd": failure["vd"],
                        "zabbix_host": failure["zabbix_host"],
                        "loja": "",
                        "designacao": "",
                        "status": "ERRO NA BASE",
                        "protocol": "",
                        "case_number": "",
                        "lp": "",
                        "message": failure["message"],
                        "zabbix_status": "NÃO SOLICITADO",
                        "zabbix_detail": "Chamado Vivo não preparado.",
                        "request_id": "",
                        "_recorded": True,
                        "_submitted": False,
                    }
                )
            items.sort(key=lambda item: int(item["position"]))
            self.prompts.clear()
            self.operation = {
                "id": str(uuid.uuid4()),
                "kind": "batch",
                "phase": "executando",
                "simulation": self.simulation,
                "started_at": now_iso(),
                "finished_at": "",
                "message": f"Processando {len(tickets)} chamado(s) em até {BATCH_CONCURRENCY} abas...",
                "issue": prepared["issue"],
                "issue_label": ISSUES[prepared["issue"]],
                "total": len(items),
                "summary": None,
                "items": items,
                "_token": object(),
                "_zabbix_started": False,
            }
            token = self.operation["_token"]
            request = {"concurrency": BATCH_CONCURRENCY, "tickets": tickets}
            command = [node, str(worker_path), "--open-batch", json.dumps(request, ensure_ascii=False, separators=(",", ":"))]
            if self.simulation:
                command.append("--dry-run")
        self.add_log(
            f"Lote autorizado: {len(tickets)} chamado(s) válido(s); {len(prepared['failures'])} erro(s) de preparação."
            + (" MODO SIMULAÇÃO: nada será enviado à Vivo." if self.simulation else "")
        )
        self._spawn(command, credentials, token)
        self.publish_state()
        return {"ok": True, "operation_id": self.operation["id"]}

    # -- worker -------------------------------------------------------------

    def _spawn(self, command: list[str], credentials: Any, token: object) -> None:
        environment: dict[str, str] = {}
        if credentials is not None:
            environment = {
                "ROBO_VIVO_CPF": credentials.cpf,
                "ROBO_VIVO_PASSWORD": credentials.senha,
            }
        try:
            worker = self.core.WorkerClient(
                command,
                lambda event: self._on_worker_event(token, event),
                lambda code: self._on_worker_exit(token, code),
                environment=environment,
            )
            with self.lock:
                self.worker = worker
            worker.start()
        except Exception as exc:
            logging.exception("Falha ao iniciar automação pela interface web")
            with self.lock:
                if self.operation and self.operation.get("_token") is token:
                    self.operation["terminal_seen"] = True
                    if self.operation["kind"] == "ticket":
                        self.operation["result"] = {
                            "status": "ERRO",
                            "message": f"Não foi possível iniciar a automação: {self.friendly(exc)}",
                        }
                    else:
                        for item in self.operation["items"]:
                            if not item["_recorded"]:
                                self._record_item(item, "ERRO", message=self.friendly(exc))
                    self._finish_operation("Não foi possível iniciar a automação.")
                self.worker = None
            self.add_log(f"ERRO ao iniciar a automação: {self.friendly(exc)}", "error")
            self.publish_state()

    def _current(self, token: object) -> Optional[dict[str, Any]]:
        operation = self.operation
        if operation and operation.get("_token") is token:
            return operation
        return None

    def _item_for(self, operation: Mapping[str, Any], event: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        request_id = str(event.get("request_id") or event.get("requestId") or event.get("id") or "")
        if not request_id:
            return None
        return next((item for item in operation["items"] if item.get("request_id") == request_id), None)

    def _on_worker_event(self, token: object, event: dict[str, Any]) -> None:
        core = self.core
        with self.lock:
            operation = self._current(token)
            if operation is None:
                return
            name = core.normalized_key(event.get("type") or event.get("event") or event.get("status") or "")
            message = str(event.get("message") or event.get("mensagem") or "").strip()
            batch = operation["kind"] == "batch"
            item = self._item_for(operation, event) if batch else None
            prefix = f"[{item['vd']}] " if item else ""
            if message:
                operation["message"] = prefix + message
            log_message = prefix + message if message else ""
            try:
                self._dispatch(operation, name, event, message, item)
            except Exception as exc:  # nunca derruba a thread de leitura
                logging.exception("Falha ao tratar evento do worker")
                log_message = f"Falha ao tratar evento da automação: {self.friendly(exc)}"
        if log_message:
            self.add_log(log_message, "error" if name in {"error", "erro", "failed", "failure"} else "info")
        self.publish_state()

    def _dispatch(
        self,
        operation: dict[str, Any],
        name: str,
        event: Mapping[str, Any],
        message: str,
        item: Optional[dict[str, Any]],
    ) -> None:
        batch = operation["kind"] == "batch"
        if name in INFO_EVENTS:
            if batch and item and item["status"] == "NA FILA":
                item["status"] = "EM ANDAMENTO"
            return
        if batch and item and item["status"] == "NA FILA":
            item["status"] = "EM ANDAMENTO"
        if name in {"auth_required", "login_required", "autenticacao_necessaria"}:
            detail = message or "A Vivo solicitou uma validação adicional."
            if batch:
                self._record_item(item, "VALIDAÇÃO NECESSÁRIA", message=detail)
                return
            operation["terminal_seen"] = True
            operation["result"] = {
                "status": "VALIDAÇÃO NECESSÁRIA",
                "message": detail + " Nenhum chamado foi enviado.",
            }
            return
        if name in {"submitted", "ticket_submitted", "chamado_enviado", "protocol_pending"}:
            if batch:
                if item:
                    item["_submitted"] = True
                    item["status"] = "ENVIADO — AGUARDANDO PROTOCOLO"
                return
            operation["submission_seen"] = True
            return
        if name in {"portal_candidate", "candidate", "candidato_portal"}:
            self._add_prompt(operation, "portal_candidate", event, item)
            if item:
                item["status"] = "AGUARDANDO CONFIRMAÇÃO"
            return
        if name in {"final_preview", "preview", "confirm_submit", "revisao_final"}:
            if batch:
                # Mesmo comportamento da tela Tkinter: no lote, a autorização do
                # envio foi dada na revisão inicial; o circuito continua exigindo
                # confirmação individual.
                self._send_response("final_preview", event, True)
                self.add_log(f"[{item['vd'] if item else '?'}] Envio autorizado pela confirmação inicial do lote.")
                return
            self._add_prompt(operation, "final_preview", event, None)
            return
        if name in SUCCESS_EVENTS:
            status = self.core.normalized_key(event.get("status", ""))
            if name == "result" and status in {"error", "erro", "failed"}:
                self._handle_error(operation, item, message or "A automação informou uma falha.")
                return
            if batch:
                self._complete_item(event, item)
            else:
                self._complete_ticket(operation, event)
            return
        if name in {"cancelled", "canceled", "cancelado"}:
            if batch:
                self._record_item(item, "CANCELADO", message=message or "Operação cancelada.")
                return
            operation["terminal_seen"] = True
            operation["result"] = {"status": "CANCELADO", "message": message or "Operação cancelada."}
            return
        if name in {"pending", "pendente"}:
            if batch:
                self._batch_pending(event, item)
            else:
                self._ticket_pending(operation, event)
            return
        if name in {"error", "erro", "failed", "failure"}:
            self._handle_error(
                operation, item, message or str(event.get("details") or "Falha não detalhada.")
            )

    def _add_prompt(
        self,
        operation: Mapping[str, Any],
        stage: str,
        event: Mapping[str, Any],
        item: Optional[Mapping[str, Any]],
    ) -> None:
        request_id = str(event.get("request_id") or event.get("id") or operation.get("request_id") or "")
        prompt_id = str(uuid.uuid4())
        prompt: dict[str, Any] = {
            "id": prompt_id,
            "stage": stage,
            "request_id": request_id,
            "vd": (item or {}).get("vd") or operation.get("vd", ""),
            "loja": (item or {}).get("loja") or operation.get("loja", ""),
            "position": (item or {}).get("position"),
            "total": operation.get("total"),
            "simulated": bool(event.get("simulated")),
            "created_at": now_iso(),
        }
        if stage == "portal_candidate":
            prompt["candidate"] = event.get("candidate") or event.get("candidato") or event.get("data") or {}
            expected = dict(self._expected_from_base(operation, item))
            # O que o worker informar tem prioridade; a base completa o resto.
            for key, value in dict(event.get("expected") or event.get("esperado") or {}).items():
                if str(value or "").strip():
                    expected[key] = value
            prompt["expected"] = expected
        else:
            preview = event.get("preview") or event.get("data") or {}
            prompt["preview"] = preview
        self.prompts[prompt_id] = prompt

    def _expected_from_base(
        self, operation: Mapping[str, Any], item: Optional[Mapping[str, Any]]
    ) -> dict[str, Any]:
        field = self.core.field
        source = item if item is not None else operation
        store = source.get("_store") or {}
        link = source.get("_link") or {}
        return {
            "vd": source.get("vd", ""),
            "store_name": field(store, "nome", "nome_loja"),
            "address": field(store, "endereco_completo", "endereco"),
            "city": field(store, "cidade"),
            "identification": field(link, "designacao", "identificacao"),
            "service_id": field(link, "id_servico") or "Não disponível na base",
        }

    def respond(self, prompt_id: str, confirmed: bool) -> dict[str, Any]:
        with self.lock:
            prompt = self.prompts.pop(str(prompt_id or ""), None)
            if not prompt:
                raise ApiError(410, "Esta confirmação não está mais pendente.")
            operation = self.operation
            event = {"request_id": prompt["request_id"]}
            self._send_response(prompt["stage"], event, bool(confirmed))
            item = None
            if operation and operation["kind"] == "batch":
                item = self._item_for(operation, event)
                if item and item["status"] == "AGUARDANDO CONFIRMAÇÃO":
                    item["status"] = "EM ANDAMENTO"
        if prompt["stage"] == "portal_candidate":
            text = "Circuito do portal confirmado." if confirmed else "Candidato recusado; procurando outro."
        else:
            text = "Envio autorizado pelo usuário." if confirmed else "Envio cancelado pelo usuário."
        self.add_log((f"[{prompt['vd']}] " if prompt.get("vd") else "") + text)
        self.publish_state()
        return {"ok": True}

    def _send_response(self, action: str, event: Mapping[str, Any], confirmed: bool) -> None:
        worker = self.worker
        try:
            if not worker:
                raise RuntimeError("Processo de automação indisponível.")
            worker.send(
                {
                    "type": "response",
                    "event": action,
                    "action": action,
                    "request_id": event.get("request_id") or event.get("id"),
                    "confirmed": confirmed,
                    "approved": confirmed,
                }
            )
        except Exception as exc:
            message = f"Não foi possível responder à automação: {exc}"
            operation = self.operation
            if operation:
                item = self._item_for(operation, event) if operation["kind"] == "batch" else None
                self._handle_error(operation, item, message)

    def _write_history(self, status: str, protocol: str, case_number: str, *, vd: str, store: Any, link: Any, issue: str) -> None:
        if self.simulation:
            return
        self.core.write_history(self.config, status, protocol, case_number, vd, store or {}, link or {}, issue)

    def _complete_ticket(self, operation: dict[str, Any], event: Mapping[str, Any]) -> None:
        protocol, case_number, lp = self.core.event_result_fields(event)
        operation["terminal_seen"] = True
        operation["submission_seen"] = True
        status = "ABERTO" if protocol else "PROTOCOLO PENDENTE"
        self._write_history(
            status, protocol, case_number,
            vd=operation["vd"], store=operation["_store"], link=operation["_link"], issue=operation["issue"],
        )
        if not lp:
            lp = operation.get("designacao", "")
        result = {
            "status": status,
            "protocol": protocol,
            "case_number": case_number,
            "lp": lp,
            "message": "Chamado aberto com sucesso." if protocol else "O chamado foi enviado, mas o protocolo ainda não apareceu. Não haverá reenvio.",
            "zabbix_status": "",
            "zabbix_detail": "",
            "support_phrase": "",
        }
        if self.simulation:
            result["message"] = "Simulação concluída; nenhum chamado real foi aberto."
        operation["result"] = result
        if not protocol:
            result["zabbix_status"] = "NÃO SOLICITADO"
            result["zabbix_detail"] = "Protocolo Vivo ainda pendente."
            result["support_phrase"] = self.core.protocol_support_phrase(operation["vd"], lp, case_number)

    def _ticket_pending(self, operation: dict[str, Any], event: Mapping[str, Any]) -> None:
        reason = self.core.normalized_key(event.get("reason", ""))
        message = str(event.get("message") or "Operação encerrada sem protocolo.")
        operation["terminal_seen"] = True
        if reason in NOT_SUBMITTED_REASONS:
            operation["result"] = {"status": "NÃO ENVIADO", "message": message}
            return
        operation["submission_seen"] = True
        protocol, case_number, lp = self.core.event_result_fields(event)
        status = "ENVIO INCERTO" if reason == "submission_state_uncertain" else "PROTOCOLO PENDENTE"
        self._write_history(
            status, protocol, case_number,
            vd=operation["vd"], store=operation["_store"], link=operation["_link"], issue=operation["issue"],
        )
        details = str(event.get("details") or "").strip()
        result = {
            "status": status,
            "protocol": protocol,
            "case_number": case_number,
            "lp": lp or operation.get("designacao", ""),
            "message": message + (f" Detalhe: {details}" if details else "")
            + " O robô não fará uma nova tentativa. Consulte o portal antes de abrir outro chamado.",
            "zabbix_status": "NÃO SOLICITADO",
            "zabbix_detail": "Protocolo Vivo pendente.",
            "support_phrase": "",
        }
        if reason == "protocol_timeout":
            result["zabbix_detail"] = "Não solicitado: Protocolo Vivo pendente após 5 minutos."
            result["support_phrase"] = self.core.protocol_support_phrase(
                operation["vd"], result["lp"], case_number
            )
        operation["result"] = result

    def _handle_error(self, operation: dict[str, Any], item: Optional[dict[str, Any]], message: str) -> None:
        if operation["kind"] == "batch":
            if item is None:
                logging.error("Erro do worker no lote sem VD associado: %s", message)
                operation["message"] = message
                return
            submitted = bool(item.get("_submitted"))
            status = "ENVIO INCERTO" if submitted else "ERRO"
            if submitted:
                self._write_history(
                    status, "", "",
                    vd=item["vd"], store=item.get("_store"), link=item.get("_link"), issue=operation["issue"],
                )
            self._record_item(item, status, message=message)
            return
        operation["terminal_seen"] = True
        logging.error("Erro do worker (web): %s", message)
        note = " Nenhuma nova tentativa foi feita automaticamente."
        if operation.get("submission_seen"):
            note += " O envio pode ter ocorrido; consulte o portal antes de tentar novamente."
        operation["result"] = {"status": "ERRO", "message": message + note}

    def _record_item(
        self,
        item: Optional[dict[str, Any]],
        status: str,
        *,
        protocol: str = "",
        case_number: str = "",
        lp: str = "",
        message: str = "",
    ) -> None:
        if not item or item.get("_recorded"):
            return
        item["_recorded"] = True
        item["status"] = status
        item["protocol"] = protocol
        item["case_number"] = case_number
        item["lp"] = lp or item.get("designacao", "")
        item["message"] = message
        item["zabbix_status"] = "AGUARDANDO" if protocol else "NÃO SOLICITADO"

    def _complete_item(self, event: Mapping[str, Any], item: Optional[dict[str, Any]]) -> None:
        if not item:
            return
        protocol, case_number, lp = self.core.event_result_fields(event)
        item["_submitted"] = True
        status = "ABERTO" if protocol else "PROTOCOLO PENDENTE"
        operation = self.operation or {}
        self._write_history(
            status, protocol, case_number,
            vd=item["vd"], store=item.get("_store"), link=item.get("_link"), issue=operation.get("issue", ""),
        )
        self._record_item(
            item,
            status,
            protocol=protocol,
            case_number=case_number,
            lp=lp,
            message=(
                "Simulação concluída; nenhum chamado real foi aberto."
                if self.simulation
                else ("Chamado aberto com sucesso." if protocol else "Chamado enviado; protocolo pendente.")
            ),
        )

    def _batch_pending(self, event: Mapping[str, Any], item: Optional[dict[str, Any]]) -> None:
        if not item:
            return
        reason = self.core.normalized_key(event.get("reason", ""))
        message = str(event.get("message") or "Operação encerrada sem protocolo.")
        if reason in NOT_SUBMITTED_REASONS:
            self._record_item(item, "NÃO ENVIADO", message=message)
            return
        protocol, case_number, lp = self.core.event_result_fields(event)
        item["_submitted"] = True
        status = "ENVIO INCERTO" if reason == "submission_state_uncertain" else "PROTOCOLO PENDENTE"
        operation = self.operation or {}
        self._write_history(
            status, protocol, case_number,
            vd=item["vd"], store=item.get("_store"), link=item.get("_link"), issue=operation.get("issue", ""),
        )
        details = str(event.get("details") or "").strip()
        self._record_item(
            item, status, protocol=protocol, case_number=case_number, lp=lp, message=details or message
        )

    def _on_worker_exit(self, token: object, code: int) -> None:
        start_zabbix: Optional[str] = None
        with self.lock:
            operation = self._current(token)
            if operation is None:
                return
            self.worker = None
            dropped = len(self.prompts)
            self.prompts.clear()
            cancelled = bool(operation.get("_cancelled"))
            if operation["kind"] == "batch":
                unfinished = [item for item in operation["items"] if not item.get("_recorded")]
                if unfinished:
                    if cancelled:
                        detail = "Automação interrompida pelo operador."
                    elif code != 0:
                        detail = f"A automação foi encerrada inesperadamente (código {code})."
                    else:
                        detail = "A automação terminou sem informar o resultado deste VD."
                    for item in unfinished:
                        self._handle_error(operation, item, detail)
                if not operation["_zabbix_started"]:
                    operation["_zabbix_started"] = True
                    start_zabbix = "batch"
            else:
                if not operation.get("terminal_seen"):
                    if cancelled:
                        self._handle_error(operation, None, "Automação interrompida pelo operador.")
                    elif code != 0:
                        self._handle_error(operation, None, f"A automação foi encerrada inesperadamente (código {code}).")
                    else:
                        self._handle_error(operation, None, "A automação terminou sem informar o resultado do chamado.")
                result = operation.get("result") or {}
                if result.get("status") == "ABERTO" and result.get("protocol"):
                    start_zabbix = "ticket"
                else:
                    self._finish_operation(result.get("message") or "Operação encerrada.")
        if dropped:
            self.add_log(f"{dropped} confirmação(ões) pendente(s) foram canceladas porque a automação terminou.", "warning")
        if start_zabbix:
            self._start_zabbix(token, start_zabbix)
        self.publish_state()

    def _start_zabbix(self, token: object, kind: str) -> None:
        with self.lock:
            operation = self._current(token)
            if operation is None:
                return
            operation["phase"] = "zabbix"
            operation["message"] = "Reconhecendo incidentes no Zabbix..."
        self.add_log("Iniciando reconhecimento seguro dos protocolos no Zabbix.")
        threading.Thread(target=self._run_zabbix, args=(token, kind), daemon=True).start()

    def _zabbix_credentials(self) -> Any:
        if self.simulation:
            return None
        try:
            return self.credentials.carregar_zabbix()
        except Exception as exc:
            logging.exception("Falha ao carregar token do Zabbix")
            self.add_log(f"Zabbix não configurado: {self.friendly(exc)}", "warning")
            return None

    def _zabbix_one(self, result: dict[str, str], credentials: Any) -> dict[str, str]:
        if self.simulation:
            updated = dict(result)
            if updated.get("status") != "ABERTO" or not updated.get("protocol"):
                updated["zabbix_status"] = "NÃO SOLICITADO"
                updated["zabbix_detail"] = "Sem Protocolo Vivo confirmado."
                return updated
            updated["zabbix_status"] = "SIMULAÇÃO"
            updated["zabbix_detail"] = "Nenhum incidente foi alterado no modo simulação."
            return updated
        return self.core.zabbix_update_result(
            self.config, result, credentials, client_factory=self.zabbix_factory
        )

    def _run_zabbix(self, token: object, kind: str) -> None:
        credentials = self._zabbix_credentials()
        with self.lock:
            operation = self._current(token)
            if operation is None:
                return
            if kind == "ticket":
                sources = [
                    {
                        "position": "1",
                        "vd": operation["vd"],
                        "zabbix_host": operation["zabbix_host"],
                        "status": operation["result"]["status"],
                        "protocol": operation["result"]["protocol"],
                        "case_number": operation["result"]["case_number"],
                    }
                ]
            else:
                sources = [
                    {
                        "position": str(item["position"]),
                        "vd": item["vd"],
                        "zabbix_host": item["zabbix_host"],
                        "status": item["status"],
                        "protocol": item["protocol"],
                        "case_number": item["case_number"],
                    }
                    for item in operation["items"]
                ]
        updated = []
        for source in sources:
            try:
                updated.append(self._zabbix_one(dict(source), credentials))
            except Exception as exc:  # proteção extra; zabbix_update_result já trata
                logging.exception("Falha inesperada no reconhecimento Zabbix")
                failed = dict(source)
                failed["zabbix_status"] = "PENDENTE"
                failed["zabbix_detail"] = self.friendly(exc)
                updated.append(failed)
        with self.lock:
            operation = self._current(token)
            if operation is None:
                return
            if kind == "ticket":
                result = operation["result"]
                result["zabbix_status"] = updated[0].get("zabbix_status", "PENDENTE")
                result["zabbix_detail"] = updated[0].get("zabbix_detail", "")
                if updated[0].get("zabbix_host"):
                    operation["zabbix_host"] = updated[0]["zabbix_host"]
                summary = f"Chamado aberto; Zabbix {result['zabbix_status']}."
            else:
                by_position = {str(value.get("position")): value for value in updated}
                for item in operation["items"]:
                    value = by_position.get(str(item["position"]))
                    if value:
                        item["zabbix_status"] = value.get("zabbix_status", "")
                        item["zabbix_detail"] = value.get("zabbix_detail", "")
                        if value.get("zabbix_host"):
                            item["zabbix_host"] = value["zabbix_host"]
                summary = self._batch_summary(operation)
            self._finish_operation(summary)
        self.add_log(summary)
        self.publish_state()

    @staticmethod
    def _batch_summary(operation: dict[str, Any]) -> str:
        items = operation["items"]
        successes = sum(1 for item in items if item["status"] in {"ABERTO", "ENVIADO"})
        pending = sum(1 for item in items if item["status"] in {"PROTOCOLO PENDENTE", "ENVIO INCERTO"})
        failures = len(items) - successes - pending
        recognized = sum(1 for item in items if item.get("zabbix_status") == "RECONHECIDO")
        zabbix_pending = sum(1 for item in items if item.get("zabbix_status") == "PENDENTE")
        operation["summary"] = {
            "abertos": successes,
            "pendentes": pending,
            "falhas": failures,
            "zabbix_reconhecidos": recognized,
            "zabbix_pendentes": zabbix_pending,
        }
        return (
            f"Lote: {successes} aberto(s), {pending} pendente(s), {failures} falha(s). "
            f"Zabbix: {recognized} reconhecido(s), {zabbix_pending} pendente(s)."
        )

    def _finish_operation(self, message: str) -> None:
        operation = self.operation
        if not operation:
            return
        operation["phase"] = "concluido"
        operation["finished_at"] = now_iso()
        operation["message"] = message

    def cancel(self) -> dict[str, Any]:
        with self.lock:
            if not self.busy():
                raise ApiError(409, "Não há operação em andamento.")
            worker = self.worker
            if self.operation:
                self.operation["_cancelled"] = True
            submitted = bool(
                self.operation
                and (
                    self.operation.get("submission_seen")
                    or any(item.get("_submitted") for item in self.operation.get("items", []))
                )
            )
        if worker:
            worker.terminate()
        self.add_log(
            "Interrupção solicitada pelo operador."
            + (" Algum envio pode já ter ocorrido; confira o portal antes de tentar de novo." if submitted else ""),
            "warning",
        )
        return {"ok": True}

    def clear_operation(self) -> dict[str, Any]:
        with self.lock:
            if self.busy():
                raise ApiError(409, "A operação ainda está em andamento.")
            self.operation = None
            self.prompts.clear()
        self.publish_state()
        return {"ok": True}

    # -- histórico e configuração ----------------------------------------

    def history(self, limit: int = 300) -> dict[str, Any]:
        path = self.core.history_path(self.config)
        tickets = self._read_csv(path)
        audits = self._read_csv(path.parent / self.core.ZABBIX_AUDIT_FILE)
        latest_audit: dict[str, dict[str, str]] = {}
        for row in audits:
            protocol = row.get("protocolo_vivo", "").strip()
            if protocol:
                latest_audit[protocol] = row
        for row in tickets:
            audit = latest_audit.get(row.get("protocolo_vivo", "").strip())
            row["status_zabbix"] = audit.get("status_zabbix", "") if audit else ""
            row["detalhe_zabbix"] = audit.get("detalhe", "") if audit else ""
            row["host_zabbix"] = audit.get("host", "") if audit else ""
        tickets.reverse()
        audits.reverse()
        return {
            "chamados": tickets[:limit],
            "zabbix": audits[:limit],
            "total_chamados": len(tickets),
            "total_zabbix": len(audits),
            "janela_duplicidade_horas": self.config.getint(
                "seguranca", "janela_duplicidade_horas", fallback=24
            ),
        }

    @staticmethod
    def _read_csv(path: Path) -> list[dict[str, str]]:
        if not path.is_file():
            return []
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                return [
                    {str(key): str(value or "") for key, value in row.items() if key is not None}
                    for row in csv.DictReader(stream, delimiter=";")
                ]
        except Exception:
            logging.exception("Não foi possível ler %s", path)
            return []

    def settings(self) -> dict[str, Any]:
        core = self.core
        config = self.config
        bases = []
        for section, option, label in (
            ("bases", "lojas", "Relação de lojas"),
            ("bases", "links", "Inventário de links"),
            ("arquivos", "modelo_link_interrompido", "Modelo: link inoperante"),
            ("arquivos", "modelo_perca_pacotes", "Modelo: perda de pacotes"),
        ):
            try:
                path = core.resolve_config_path(config, section, option)
                exists = path.is_file()
                stat = path.stat() if exists else None
                bases.append(
                    {
                        "nome": label,
                        "arquivo": path.name,
                        "existe": exists,
                        "tamanho_kb": max(1, round(stat.st_size / 1024)) if stat and stat.st_size else 0,
                        "atualizado_em": dt.datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="minutes") if stat else "",
                    }
                )
            except Exception as exc:
                bases.append({"nome": label, "arquivo": "", "existe": False, "erro": self.friendly(exc)})
        vivo: dict[str, Any] = {"disponivel": True, "configurado": False, "cpf": ""}
        zabbix: dict[str, Any] = {"disponivel": True, "configurado": False, "api_url": "", "automatico": True}
        try:
            current = self.credentials.carregar()
            if current:
                vivo.update(configurado=True, cpf=mask_cpf(current.cpf))
        except Exception as exc:
            vivo.update(disponivel=False, erro=self.friendly(exc))
        try:
            current_z = self.credentials.carregar_zabbix()
            if current_z:
                zabbix.update(configurado=True, api_url=current_z.api_url, automatico=bool(current_z.automatico))
        except Exception as exc:
            zabbix.update(disponivel=False, erro=self.friendly(exc))
        return {
            "simulation": self.simulation,
            "credenciais_vivo": vivo,
            "credenciais_zabbix": zabbix,
            "zabbix_url_padrao": "http://172.16.2.250/api_jsonrpc.php",
            "bases": bases,
            "portal": {
                "url": config.get("portal", "url_portal", fallback=""),
                "produto_esperado": config.get("portal", "produto_esperado", fallback=""),
                "ocorrencia": config.get("portal", "ocorrencia", fallback=""),
                "navegador_oculto": config.get("portal", "navegador_oculto", fallback=""),
                "tempo_protocolo_segundos": config.get("portal", "tempo_protocolo_segundos", fallback=""),
            },
            "contato": {
                "nome": config.get("contato", "nome", fallback=""),
                "telefone": config.get("contato", "telefone", fallback=""),
                "email": config.get("contato", "email", fallback=""),
            },
            "janela_duplicidade_horas": config.getint("seguranca", "janela_duplicidade_horas", fallback=24),
            "limite_lote": BATCH_LIMIT,
            "abas_paralelas": BATCH_CONCURRENCY,
        }

    def _require_credential_changes_allowed(self) -> None:
        if self.busy():
            raise ApiError(409, "Aguarde a operação atual terminar.")

    def save_vivo(self, cpf: str, senha: str) -> dict[str, Any]:
        self._require_credential_changes_allowed()
        try:
            # Trocar o colaborador também invalida a sessão do perfil do robô.
            self.core.clear_robot_profile()
            self.credentials.salvar(cpf, senha)
        except Exception as exc:
            logging.exception("Falha ao salvar acesso automático (web)")
            raise ApiError(400, self.friendly(exc)) from None
        self.add_log("Acesso automático configurado e protegido pelo Windows.")
        return {"ok": True}

    def remove_vivo(self) -> dict[str, Any]:
        self._require_credential_changes_allowed()
        try:
            self.credentials.remover()
            self.core.clear_robot_profile()
        except Exception as exc:
            raise ApiError(400, self.friendly(exc)) from None
        self.add_log("Acesso automático ao Portal Vivo removido deste computador.")
        return {"ok": True}

    def _zabbix_token(self, token: str) -> str:
        token = str(token or "").strip()
        if token:
            return token
        current = self.credentials.carregar_zabbix()
        return current.token if current else ""

    def test_zabbix(self, api_url: str, token: str) -> dict[str, Any]:
        try:
            from zabbix_client import ZabbixClient, normalize_api_url  # type: ignore

            client = ZabbixClient(normalize_api_url(api_url), self._zabbix_token(token))
            version = client.test_connection()
        except Exception as exc:
            raise ApiError(400, self.friendly(exc)) from None
        return {"ok": True, "versao": str(version)}

    def save_zabbix(self, api_url: str, token: str, automatico: bool) -> dict[str, Any]:
        self._require_credential_changes_allowed()
        try:
            from zabbix_client import normalize_api_url  # type: ignore

            self.credentials.salvar_zabbix(normalize_api_url(api_url), self._zabbix_token(token), bool(automatico))
        except Exception as exc:
            logging.exception("Falha ao salvar configuração do Zabbix (web)")
            raise ApiError(400, self.friendly(exc)) from None
        self.add_log("Integração Zabbix configurada e token protegido pelo Windows.")
        return {"ok": True}

    def remove_zabbix(self) -> dict[str, Any]:
        self._require_credential_changes_allowed()
        try:
            self.credentials.remover_zabbix()
        except Exception as exc:
            raise ApiError(400, self.friendly(exc)) from None
        self.add_log("Integração Zabbix removida deste computador.")
        return {"ok": True}

    def shutdown_worker(self) -> None:
        with self.lock:
            worker = self.worker
        if worker:
            worker.terminate()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class WebServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, service: RoboService, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), RequestHandler)
        self.service = service
        self.token = secrets.token_urlsafe(32)
        self.stopping = threading.Event()

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def launch_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    def request_stop(self) -> None:
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.service.hub.publish({"type": "shutdown"})
        threading.Thread(target=self.shutdown, daemon=True).start()


class RequestHandler(http.server.BaseHTTPRequestHandler):
    server: WebServer
    protocol_version = "HTTP/1.1"
    server_version = "RoboVivo"
    sys_version = ""

    # Nunca registra URLs: a primeira contém o token da sessão.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    # -- segurança -----------------------------------------------------------

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host in {f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}"}

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value
        return ""

    def _authenticated(self) -> bool:
        return hmac.compare_digest(self._cookie_token().encode(), self.server.token.encode())

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin.lower() in {f"http://127.0.0.1:{self.server.port}", f"http://localhost:{self.server.port}"}

    def _security_headers(self) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    # -- respostas -----------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Mapping[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error_page(self, status: int, message: str) -> None:
        html = (
            "<!doctype html><html lang='pt-BR'><meta charset='utf-8'><title>Robô Vivo</title>"
            "<body style='font-family:system-ui;padding:40px;color:#2f2933'>"
            f"<h1 style='color:#660099'>Robô de Chamados Vivo</h1><p>{message}</p></body></html>"
        )
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "Requisição grande demais.")
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ApiError(415, "Envie os dados em JSON.")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "JSON inválido.") from None
        if not isinstance(value, dict):
            raise ApiError(400, "JSON inválido.")
        return value

    # -- GET -----------------------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._error_page(421, "Endereço não permitido.")
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/" and parsed.query:
            token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
            if hmac.compare_digest(token.encode(), self.server.token.encode()):
                self._send(
                    303,
                    b"",
                    "text/plain",
                    {
                        "Location": "/",
                        "Set-Cookie": f"{COOKIE_NAME}={self.server.token}; HttpOnly; SameSite=Strict; Path=/",
                    },
                )
                return
        if not self._authenticated():
            self._error_page(
                403,
                "Sessão inválida. Feche esta aba e abra o robô pelo atalho "
                "<b>Executar automação vivo (web)</b>.",
            )
            return
        if path == "/":
            self._static("index.html")
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        elif path.startswith("/assets/"):
            self._asset(path[len("/assets/"):])
        elif path == "/api/state":
            self._json(200, self.server.service.snapshot())
        elif path == "/api/events":
            self._events()
        elif path == "/api/history":
            self._json(200, self.server.service.history())
        elif path == "/api/settings":
            self._json(200, self.server.service.settings())
        else:
            self._json(404, {"error": "Não encontrado."})

    def _static(self, name: str) -> None:
        target = (WEB_DIR / name).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            self._json(404, {"error": "Não encontrado."})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "text/javascript"}:
            content_type += "; charset=utf-8"
        self._send(200, target.read_bytes(), content_type)

    def _asset(self, name: str) -> None:
        if name not in {"app_icon.png", "app_icon.ico", "vivo_wordmark.png"}:
            self._json(404, {"error": "Não encontrado."})
            return
        path = Path(self.server.service.core.asset_path(name))
        if not path.is_file():
            self._json(404, {"error": "Não encontrado."})
            return
        content_type = "image/x-icon" if name.endswith(".ico") else "image/png"
        self._send(200, path.read_bytes(), content_type)

    def _events(self) -> None:
        service = self.server.service
        channel = service.hub.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Connection", "keep-alive")
            self._security_headers()
            self.end_headers()
            self._write_event({"type": "state", "state": service.snapshot()})
            while not self.server.stopping.is_set():
                try:
                    event = channel.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._write_event(event)
                if event.get("type") == "shutdown":
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            service.hub.unsubscribe(channel)
            self.close_connection = True

    def _write_event(self, event: Mapping[str, Any]) -> None:
        data = json.dumps(event, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # -- POST ----------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok() or not self._origin_ok():
            self._json(403, {"error": "Origem não permitida."})
            return
        if not self._authenticated() or self.headers.get(CSRF_HEADER) != "1":
            self._json(403, {"error": "Sessão inválida. Abra o robô pelo atalho novamente."})
            return
        service = self.server.service
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self._read_json()
            routes: dict[str, Callable[[], Any]] = {
                "/api/lookup": lambda: service.lookup(str(body.get("entrada") or "")),
                "/api/ticket/preview": lambda: service.preview(
                    str(body.get("entrada") or ""), body.get("problema"), str(body.get("designacao") or "")
                ),
                "/api/ticket/start": lambda: service.start_ticket(
                    str(body.get("entrada") or ""),
                    body.get("problema"),
                    str(body.get("designacao") or ""),
                    bool(body.get("aceitar_duplicidade")),
                ),
                "/api/batch/prepare": lambda: service.prepare_batch(
                    str(body.get("entrada") or ""), body.get("problema")
                ),
                "/api/batch/start": lambda: service.start_batch(str(body.get("batch_id") or "")),
                "/api/respond": lambda: service.respond(str(body.get("prompt_id") or ""), body.get("confirmado") is True),
                "/api/cancel": service.cancel,
                "/api/operation/clear": service.clear_operation,
                "/api/credentials/vivo": lambda: service.save_vivo(str(body.get("cpf") or ""), str(body.get("senha") or "")),
                "/api/credentials/vivo/remove": service.remove_vivo,
                "/api/credentials/zabbix": lambda: service.save_zabbix(
                    str(body.get("api_url") or ""), str(body.get("token") or ""), body.get("automatico") is not False
                ),
                "/api/credentials/zabbix/test": lambda: service.test_zabbix(
                    str(body.get("api_url") or ""), str(body.get("token") or "")
                ),
                "/api/credentials/zabbix/remove": service.remove_zabbix,
                "/api/shutdown": self._shutdown,
            }
            handler = routes.get(path)
            if handler is None:
                raise ApiError(404, "Não encontrado.")
            self._json(200, handler())
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message, **exc.extra})
        except Exception as exc:
            logging.exception("Erro inesperado na interface web")
            self._json(500, {"error": service.friendly(exc)})

    def _shutdown(self) -> dict[str, Any]:
        service = self.server.service
        if service.busy():
            raise ApiError(409, "Há uma operação em andamento. Interrompa ou aguarde antes de encerrar.")
        self.server.request_stop()
        return {"ok": True}


# ---------------------------------------------------------------------------
# Inicialização
# ---------------------------------------------------------------------------


def write_session_file(server: WebServer) -> Path:
    path = session_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"port": server.port, "token": server.token, "pid": os.getpid()}),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def read_session_url() -> Optional[str]:
    """URL da instância web já aberta neste usuário, se ela responder."""

    try:
        data = json.loads(session_file_path().read_text(encoding="utf-8"))
        port = int(data["port"])
        token = str(data["token"])
    except Exception:
        return None
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            pass
    except OSError:
        return None
    return f"http://127.0.0.1:{port}/?token={token}"


def remove_session_file(server: WebServer) -> None:
    path = session_file_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if int(data.get("port", 0)) == server.port:
            path.unlink()
    except Exception:
        pass


def idle_watchdog(server: WebServer, idle_minutes: int) -> None:
    """Encerra o servidor quando nenhuma aba fica aberta por muito tempo."""

    if idle_minutes <= 0:
        return
    limit = idle_minutes * 60
    while not server.stopping.wait(30):
        service = server.service
        if service.hub.subscriber_count or service.busy():
            service.hub.last_activity = time.monotonic()
            continue
        if time.monotonic() - service.hub.last_activity >= limit:
            logging.info("Interface web encerrada após %s minutos sem uso.", idle_minutes)
            server.request_stop()
            return


def serve(
    core: Any,
    config: Any,
    *,
    simulation: bool = False,
    open_browser: bool = True,
    port: Optional[int] = None,
    credentials: Any = None,
    on_ready: Optional[Callable[[WebServer], None]] = None,
) -> int:
    service = RoboService(core, config, simulation=simulation, credentials=credentials)
    chosen_port = port if port is not None else config.getint("web", "porta", fallback=0)
    server = WebServer(service, chosen_port)
    write_session_file(server)
    idle = config.getint("web", "encerrar_sem_uso_minutos", fallback=30)
    threading.Thread(target=idle_watchdog, args=(server, idle), daemon=True).start()
    logging.info("Interface web iniciada na porta %s%s.", server.port, " (simulação)" if simulation else "")
    if on_ready:
        on_ready(server)
    if open_browser:
        webbrowser.open(server.launch_url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        service.shutdown_worker()
        remove_session_file(server)
        server.server_close()
    return 0


__all__ = ["RoboService", "WebServer", "read_session_url", "serve"]
