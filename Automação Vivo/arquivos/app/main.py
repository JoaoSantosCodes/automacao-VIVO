"""Interface do Robô de Abertura de Chamados Vivo.

Este módulo é deliberadamente o único ponto que conhece Tkinter. A leitura das
bases fica em ``data_service.py`` e a navegação no portal, em
``vivo_worker.mjs``. A comunicação com o worker usa JSON Lines para que a
janela continue responsiva enquanto o navegador trabalha em segundo plano.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import ctypes
import dataclasses
import datetime as dt
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import traceback
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional


FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    # No pacote portátil, o executável fica na raiz e os arquivos editáveis
    # continuam nas pastas app/data/modelos ao lado dele.
    PACKAGE_DIR = Path(sys.executable).resolve().parent
    APP_DIR = PACKAGE_DIR / "app"
else:
    APP_DIR = Path(__file__).resolve().parent
    PACKAGE_DIR = APP_DIR.parent
CONFIG_PATH = PACKAGE_DIR / "configuracao.ini"


def asset_path(name: str) -> Path:
    """Localiza um recurso visual no código-fonte ou no pacote PyInstaller."""

    candidates = []
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidates.append(Path(bundle_dir) / "assets" / name)
    candidates.extend(
        [
            PACKAGE_DIR / "assets" / name,
            APP_DIR.parent / "assets" / name,
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0] if candidates else Path(name)


class AlreadyRunning(RuntimeError):
    """Indica que outra janela do robô já está aberta."""


class SingleInstanceMutex:
    """Mutex nomeado no Windows; o handle permanece vivo até o encerramento."""

    NAME = r"Local\DPSP_RoboVivo_AbrirChamado_v1"

    def __init__(self) -> None:
        self.handle: Optional[int] = None
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, self.NAME)
        if not handle:
            raise OSError("Não foi possível criar o controle de instância única.")
        self.handle = int(handle)
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None
            raise AlreadyRunning("O Robô de Chamados Vivo já está aberto.")

    def close(self) -> None:
        if self.handle and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None


def load_config(path: Path = CONFIG_PATH) -> configparser.ConfigParser:
    config = configparser.ConfigParser(interpolation=None)
    if not path.is_file():
        raise FileNotFoundError(f"Arquivo de configuração não encontrado: {path}")
    with path.open("r", encoding="utf-8-sig") as stream:
        config.read_file(stream)
    return config


def resolve_config_path(config: configparser.ConfigParser, section: str, option: str) -> Path:
    raw = config.get(section, option, fallback="").strip()
    if not raw:
        raise ValueError(f"Configuração ausente: [{section}] {option}")
    normalized = raw.replace("\\", os.sep).replace("/", os.sep)
    path = Path(normalized)
    return path if path.is_absolute() else PACKAGE_DIR / path


def config_bool(
    config: configparser.ConfigParser, section: str, option: str, fallback: bool = False
) -> bool:
    """Lê booleanos aceitando também ``sim``/``não`` em português."""

    raw = config.get(section, option, fallback="sim" if fallback else "nao")
    normalized = normalized_key(raw)
    if normalized in {"1", "sim", "s", "yes", "y", "true", "verdadeiro"}:
        return True
    if normalized in {"0", "nao", "n", "no", "false", "falso"}:
        return False
    raise ValueError(f"Valor booleano inválido em [{section}] {option}: {raw}")


def plain(value: Any) -> Any:
    """Converte dataclasses/objetos do serviço para estruturas serializáveis."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Os objetos do serviço expõem métodos públicos que acrescentam campos
    # calculados importantes (por exemplo, endereço completo e palavras-chave).
    # Eles precisam ter prioridade sobre ``dataclasses.asdict``, que retornaria
    # apenas os campos armazenados.
    for method_name in ("to_payload", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            return plain(method())
    if dataclasses.is_dataclass(value):
        return {key: plain(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [plain(item) for item in value]
    if hasattr(value, "_asdict"):
        return plain(value._asdict())
    if hasattr(value, "__dict__"):
        return {
            key: plain(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and not callable(item)
        }
    return str(value)


def normalized_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def field(data: Any, *aliases: str, default: Any = "") -> Any:
    mapping = plain(data)
    if not isinstance(mapping, Mapping):
        return default
    indexed = {normalized_key(key): value for key, value in mapping.items()}
    for alias in aliases:
        value = indexed.get(normalized_key(alias))
        if value is not None and str(value).strip() != "":
            return value
    return default


def clean_vd(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        raise ValueError("Informe o VD da loja.")
    return f"VD{str(int(digits)).zfill(3)}"


def protocol_support_phrase(vd: Any, lp: Any, case_number: Any) -> str:
    """Monta a mensagem pronta para solicitar o protocolo ao time."""

    vd_text = str(vd or "").strip() or "não informado"
    lp_text = str(lp or "").strip() or "não informada"
    case_text = str(case_number or "").strip() or "não informado"
    return (
        "Time, poderiam nos apoiar com o Protocolo Vivo da filial abaixo? "
        "Ele não ficou disponível no portal após 5 minutos.\n\n"
        f"VD: {vd_text}\n"
        f"LP: {lp_text}\n"
        f"Número do caso: {case_text}\n"
        "---------------"
    )


def parse_vd_jobs(value: str, limit: int = 50) -> list[dict[str, str]]:
    """Preserva o host do Zabbix e extrai o VD sem misturar lojas."""

    jobs: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    text = (value or "").replace("\\_", "_")
    host_pattern = re.compile(
        r"(?P<host>(?:DSP|DPA)_(?:VD_?)?(?P<digits>\d{1,7})_MPLS_VIVO)",
        re.IGNORECASE,
    )
    for part in re.split(r"[\s,;]+", text):
        token = part.strip().strip("[](){}.:|")
        host_match = host_pattern.fullmatch(token)
        if host_match:
            vd = clean_vd(host_match.group("digits"))
            host = host_match.group("host").upper().replace("_VD_", "_").replace("_VD", "_")
        else:
            vd_match = re.fullmatch(r"(?:VD)?0*(\d{1,7})", token, re.IGNORECASE)
            if not vd_match:
                continue
            vd = clean_vd(vd_match.group(1))
            host = ""
        key = vd.casefold()
        previous = seen.get(key)
        if previous and host and previous != host:
            raise ValueError(
                f"O {vd} apareceu com dois hosts diferentes ({previous} e {host})."
            )
        if key in seen:
            if not previous and host:
                seen[key] = host
                for existing in jobs:
                    if existing["vd"].casefold() == key:
                        existing["zabbix_host"] = host
                        break
            continue
        seen[key] = host
        jobs.append({"vd": vd, "zabbix_host": host})

    if not jobs:
        raise ValueError("Informe pelo menos um VD para o lote.")
    if len(jobs) > limit:
        raise ValueError(f"O lote permite no máximo {limit} VDs por vez.")
    return jobs


def parse_vd_list(value: str, limit: int = 50) -> list[str]:
    """Compatibilidade: retorna somente os VDs do lote."""

    return [job["vd"] for job in parse_vd_jobs(value, limit=limit)]


def extract_zabbix_host(value: str) -> str:
    jobs = parse_vd_jobs(value, limit=1)
    return jobs[0]["zabbix_host"]


class DataAdapter:
    """Adaptador explícito para o contrato público de ``data_service``.

    Contrato preferido:
      ``DataService(lojas_path, links_path, modelo_link..., modelo_perca...)``
      ``localizar_loja(vd)``, ``localizar_links(vd)`` e
      ``preparar_chamado(vd, problema, designacao=None)``.

    Os aliases abaixo apenas tornam a GUI tolerante a uma renomeação simples do
    serviço, sem duplicar nenhuma regra de negócio das bases.
    """

    def __init__(self, config: configparser.ConfigParser) -> None:
        if str(APP_DIR) not in sys.path:
            sys.path.insert(0, str(APP_DIR))
        import data_service  # type: ignore

        self.module = data_service
        lojas = resolve_config_path(config, "bases", "lojas")
        links = resolve_config_path(config, "bases", "links")
        model_link = resolve_config_path(config, "arquivos", "modelo_link_interrompido")
        model_loss = resolve_config_path(config, "arquivos", "modelo_perca_pacotes")
        service_class = getattr(data_service, "DataService", None)
        if service_class is None:
            raise RuntimeError("data_service.py não fornece a classe DataService.")
        self.service = service_class(
            lojas,
            links,
            modelo_link_interrompido=model_link,
            modelo_perca_pacotes=model_loss,
        )

    def _method(self, *names: str) -> Callable[..., Any]:
        for owner in (self.service, self.module):
            for name in names:
                candidate = getattr(owner, name, None)
                if callable(candidate):
                    return candidate
        raise RuntimeError(f"Operação de dados indisponível: {', '.join(names)}")

    def lookup(self, vd: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        locate_store = self._method("localizar_loja", "find_store", "buscar_loja")
        locate_links = self._method("localizar_links", "find_links", "buscar_links")
        store = plain(locate_store(vd))
        links = plain(locate_links(vd))
        if not isinstance(store, dict):
            raise RuntimeError("A base retornou dados inválidos para a loja.")
        if not isinstance(links, list):
            links = [links] if links else []
        return store, [item for item in links if isinstance(item, dict)]

    def build_ticket(self, vd: str, issue: str, designation: str = "") -> dict[str, Any]:
        prepare = self._method("preparar_chamado", "build_ticket", "montar_chamado")
        try:
            result = prepare(vd, issue, designacao=designation or None)
        except TypeError:
            # Compatibilidade com um possível alias cujo terceiro argumento seja
            # apenas posicional. Não há regra de negócio alternativa aqui.
            result = prepare(vd, issue, designation or None)
        converted = plain(result)
        if not isinstance(converted, dict):
            raise RuntimeError("A base retornou um chamado inválido.")
        return converted


class WorkerClient:
    """Processo Node controlado por JSONL, sem bloquear a thread da interface."""

    def __init__(
        self,
        command: list[str],
        on_event: Callable[[dict[str, Any]], None],
        on_exit: Callable[[int], None],
        environment: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.command = command
        self.on_event = on_event
        self.on_exit = on_exit
        self.environment = dict(environment or {})
        self.process: Optional[subprocess.Popen[str]] = None
        self._write_lock = threading.Lock()
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        flags = 0
        startupinfo = None
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        child_environment = os.environ.copy()
        child_environment.update(self.environment)
        self.process = subprocess.Popen(
            self.command,
            cwd=str(PACKAGE_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
            startupinfo=startupinfo,
            env=child_environment,
        )
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        threading.Thread(target=self._wait, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        for raw_line in self.process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("evento não é um objeto")
            except Exception:
                event = {"type": "status", "message": line}
            self.on_event(event)

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for raw_line in self.process.stderr:
            line = raw_line.strip()
            if line:
                logging.getLogger("worker").warning("%s", line)

    def _wait(self) -> None:
        assert self.process
        code = self.process.wait()
        # Garante que o último evento JSON (success/error/pending) entre na fila
        # antes da notificação de encerramento do processo.
        if self._stdout_thread:
            self._stdout_thread.join(timeout=2)
        if self._stderr_thread:
            self._stderr_thread.join(timeout=1)
        self.on_exit(code)

    def send(self, value: Mapping[str, Any]) -> None:
        process = self.process
        if not process or not process.stdin or process.poll() is not None:
            raise RuntimeError("A automação do portal já foi encerrada.")
        serialized = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            process.stdin.write(serialized + "\n")
            process.stdin.flush()

    def terminate(self) -> None:
        process = self.process
        if process and process.poll() is None:
            process.terminate()


# ---------------------------------------------------------------------------
# Regras compartilhadas entre a interface Tkinter e a interface web.
# Nenhuma delas depende de widgets; ambas as telas chamam estas funções para
# garantir as mesmas travas (duplicidade, histórico, Zabbix e credenciais).
# ---------------------------------------------------------------------------


def credential_api() -> Any:
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    import credential_store  # type: ignore

    return credential_store


def zabbix_client_for(credentials: Any) -> Any:
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    from zabbix_client import ZabbixClient  # type: ignore

    return ZabbixClient(credentials.api_url, credentials.token)


def find_node_and_worker() -> tuple[str, Path]:
    node = os.environ.get("ROBO_NODE_EXE", "").strip()
    if not node:
        node = shutil.which("node") or ""
    worker_candidates = (APP_DIR / "vivo_worker.mjs", PACKAGE_DIR / "automation" / "vivo_worker.mjs")
    worker = next((path for path in worker_candidates if path.is_file()), worker_candidates[0])
    if node and not Path(node).is_file():
        # Alguns pacotes antigos apontavam para ``node\node.exe`` enquanto
        # o runtime atual o mantém em ``node\bin\node.exe``.
        alternate = Path(node).parent / "bin" / "node.exe"
        if alternate.is_file():
            node = str(alternate)
    if not node or not Path(node).is_file():
        # shutil.which pode retornar nome resolvível, mas normalmente retorna caminho.
        resolved = shutil.which(node) if node else None
        if not resolved:
            raise FileNotFoundError("O componente Node.js não foi encontrado.")
        node = resolved
    if not worker.is_file():
        raise FileNotFoundError(f"Automação do portal não encontrada: {worker}")
    return node, worker


def clear_robot_profile() -> None:
    """Remove somente a sessão do perfil exclusivo deste robô."""

    local_app_data_raw = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data_raw:
        raise OSError("A pasta LOCALAPPDATA do Windows não está disponível.")
    local_app_data = Path(local_app_data_raw).resolve()
    robot_root = (local_app_data / "RoboVivo").resolve()
    profile = (robot_root / "ChromeProfile").resolve()
    if robot_root not in profile.parents or profile.name != "ChromeProfile":
        raise RuntimeError("O caminho do perfil do robô não pôde ser validado.")
    if profile.is_dir():
        shutil.rmtree(profile)


def build_ticket_payload(
    config: configparser.ConfigParser,
    ticket: dict[str, Any],
    store: dict[str, Any],
    link: dict[str, Any],
    target_vd: str,
    target_issue: str,
) -> dict[str, Any]:
    ticket_store = field(ticket, "loja", default=store)
    if not isinstance(ticket_store, Mapping):
        ticket_store = store
    ticket_link = field(ticket, "link", "circuito", default=link)
    if not isinstance(ticket_link, Mapping):
        ticket_link = link
    start = field(ticket, "hora_inicio", default=field(ticket_store, "hora_inicio"))
    finish = field(
        ticket,
        "hora_fim",
        "hora_termino",
        default=field(ticket_store, "hora_fim", "hora_termino"),
    )
    is_24h = bool(field(ticket_store, "vinte_quatro_horas", default=False))
    if is_24h:
        start, finish = "00:00", "23:59"
    contact = {
        "apelido": target_vd,
        "nome": config.get("contato", "nome", fallback="central de comando"),
        "telefone": config.get("contato", "telefone", fallback="1132747527"),
        "email": config.get("contato", "email", fallback="central.comando@dpsp.com.br"),
        "compartilhar_celular": config.get("contato", "compartilhar_celular", fallback="Sim"),
        "capacitacao_documentacao": config.get(
            "contato", "capacitacao_documentacao", fallback="Sim"
        ),
    }
    description = field(ticket, "descricao")
    if not description:
        raise RuntimeError("A descrição do chamado não foi gerada pela base.")
    return {
        "request_id": str(uuid.uuid4()),
        "config_path": str(CONFIG_PATH),
        "vd": target_vd,
        "problema": target_issue,
        "issue": target_issue,
        "descricao": description,
        "description": description,
        "loja": dict(ticket_store),
        "store": dict(ticket_store),
        "circuito": dict(ticket_link),
        "link": dict(ticket_link),
        "horario_inicio": str(start),
        "horario_termino": str(finish),
        "hora_inicio": str(start),
        "hora_fim": str(finish),
        "contato": contact,
        "contact": contact,
        "produto_filtro": config.get("portal", "produto_filtro", fallback="mpls"),
        "produto_esperado": config.get("portal", "produto_esperado", fallback="VPN IP MPLS"),
        "portal_url": config.get(
            "portal", "url_portal", fallback="https://mve.vivo.com.br/sec/dashboard"
        ),
        "ocorrencia": config.get("portal", "ocorrencia", fallback="Indisponibilidade"),
        "tempo_protocolo_segundos": config.getint(
            "portal", "tempo_protocolo_segundos", fallback=300
        ),
        "navegador_oculto": config_bool(config, "portal", "navegador_oculto", fallback=True),
        "allow_submit": True,
    }


def history_path(config: configparser.ConfigParser) -> Path:
    return resolve_config_path(config, "arquivos", "historico")


def recent_duplicate(
    config: configparser.ConfigParser, payload: Mapping[str, Any], fallback_vd: str = ""
) -> bool:
    path = history_path(config)
    if not path.is_file():
        return False
    window = config.getint("seguranca", "janela_duplicidade_horas", fallback=24)
    cutoff = dt.datetime.now().astimezone() - dt.timedelta(hours=max(window, 0))
    designation = str(field(payload.get("circuito", {}), "designacao", "identificacao"))
    target_vd = str(payload.get("vd") or fallback_vd)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream, delimiter=";"):
                if row.get("vd", "").casefold() != target_vd.casefold():
                    continue
                if designation and row.get("designacao", "").casefold() != designation.casefold():
                    continue
                try:
                    recorded = dt.datetime.fromisoformat(row.get("data_hora", ""))
                    if recorded.tzinfo is None:
                        recorded = recorded.astimezone()
                    if recorded >= cutoff:
                        return True
                except ValueError:
                    continue
    except Exception:
        logging.exception("Não foi possível consultar duplicidades no histórico")
    return False


def write_history(
    config: configparser.ConfigParser,
    status: str,
    protocol: str,
    case_number: str,
    vd_value: str,
    store: Mapping[str, Any],
    link: Mapping[str, Any],
    issue_value: str,
) -> None:
    try:
        path = history_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.is_file() and path.stat().st_size > 0
        row = {
            "data_hora": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "vd": vd_value,
            "loja": field(store, "nome", "nome_loja"),
            "problema": issue_value,
            "designacao": field(link, "designacao", "identificacao"),
            "usuario_id": field(link, "usuario", "id_servico"),
            "protocolo_vivo": protocol,
            "numero_caso": case_number,
            "status": status,
        }
        with path.open("a", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row), delimiter=";")
            if not exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception:
        logging.exception("Não foi possível registrar o histórico")


ZABBIX_AUDIT_FILE = "zabbix_reconhecimentos.csv"


def write_zabbix_audit(config: configparser.ConfigParser, result: Mapping[str, Any]) -> None:
    try:
        path = history_path(config).parent / ZABBIX_AUDIT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.is_file() and path.stat().st_size > 0
        row = {
            "data_hora": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "vd": str(result.get("vd") or ""),
            "host": str(result.get("zabbix_host") or ""),
            "protocolo_vivo": str(result.get("protocol") or ""),
            "status_zabbix": str(result.get("zabbix_status") or ""),
            "detalhe": str(result.get("zabbix_detail") or "").replace("\r", " ").replace("\n", " "),
        }
        with path.open("a", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row), delimiter=";")
            if not exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception:
        logging.exception("Não foi possível registrar a auditoria do Zabbix")


def zabbix_update_result(
    config: configparser.ConfigParser,
    result: dict[str, str],
    credentials: Any,
    client_factory: Callable[[Any], Any] = zabbix_client_for,
) -> dict[str, str]:
    updated = dict(result)

    def finish() -> dict[str, str]:
        write_zabbix_audit(config, updated)
        return updated

    protocol = str(updated.get("protocol") or "").strip()
    host = str(updated.get("zabbix_host") or "").strip()
    vd = str(updated.get("vd") or "").strip()
    zabbix_target = host or vd
    if updated.get("status") != "ABERTO" or not protocol:
        updated["zabbix_status"] = "NÃO SOLICITADO"
        updated["zabbix_detail"] = "Sem Protocolo Vivo confirmado."
        return finish()
    if not zabbix_target:
        updated["zabbix_status"] = "PENDENTE"
        updated["zabbix_detail"] = "VD ou host Zabbix não foi informado."
        return finish()
    if not credentials:
        updated["zabbix_status"] = "NÃO CONFIGURADO"
        updated["zabbix_detail"] = "Cadastre a URL e o token da API no botão Configurar Zabbix."
        return finish()
    if not credentials.automatico:
        updated["zabbix_status"] = "DESATIVADO"
        updated["zabbix_detail"] = "Reconhecimento automático desativado na configuração."
        return finish()
    try:
        client = client_factory(credentials)
        acknowledgement = client.acknowledge_host_problem(zabbix_target, protocol)
        updated["zabbix_host"] = acknowledgement.host
        updated["zabbix_status"] = "RECONHECIDO"
        updated["zabbix_detail"] = (
            f"Evento {acknowledgement.eventid} reconhecido com o protocolo {protocol}."
        )
    except Exception as exc:
        logging.warning(
            "Reconhecimento Zabbix pendente para %s: %s",
            zabbix_target,
            friendly_error(exc),
        )
        updated["zabbix_status"] = "PENDENTE"
        updated["zabbix_detail"] = friendly_error(exc)
    return finish()


def event_result_fields(event: Mapping[str, Any]) -> tuple[str, str, str]:
    """Extrai protocolo, número do caso e LP de um evento do worker."""

    nested = event.get("result") if isinstance(event.get("result"), Mapping) else {}
    protocol = str(
        event.get("protocol")
        or event.get("protocolo")
        or event.get("protocolo_vivo")
        or field(nested, "protocol", "protocolo", "protocolo_vivo")
        or ""
    ).strip()
    case_number = str(
        event.get("case_number")
        or event.get("numero_caso")
        or event.get("case")
        or field(nested, "case_number", "numero_caso", "case")
        or ""
    ).strip()
    lp = str(
        event.get("lp")
        or event.get("identification")
        or event.get("identificacao")
        or field(nested, "lp", "identification", "identificacao")
        or ""
    ).strip()
    return protocol, case_number, lp


class VivoApp:
    POLL_MS = 80

    def __init__(self, root: Any, config: configparser.ConfigParser) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.config = config
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.adapter: Optional[DataAdapter] = None
        self.store: Optional[dict[str, Any]] = None
        self.links: list[dict[str, Any]] = []
        self.ticket: Optional[dict[str, Any]] = None
        self.worker: Optional[WorkerClient] = None
        self.worker_kind = ""
        self.worker_finished = True
        self.ticket_started = False
        self.submission_seen = False
        self.terminal_event_seen = False
        self.batch_active = False
        self.batch_queue: list[dict[str, Any]] = []
        self.batch_results: list[dict[str, str]] = []
        self.batch_current: Optional[dict[str, Any]] = None
        self.batch_credentials: Any = None
        self.batch_node = ""
        self.batch_worker_path: Optional[Path] = None
        self.batch_total = 0
        self.batch_position = 0
        self.batch_issue = ""
        self.batch_items_by_request: dict[str, dict[str, Any]] = {}
        self.batch_concurrency = 5
        self.batch_zabbix_started = False
        self.current_zabbix_host = ""
        self.zabbix_single_pending = False

        self.vd_var = tk.StringVar()
        self.store_var = tk.StringVar(value="Informe o VD para consultar as bases.")
        self.link_var = tk.StringVar()
        self.link_detail_var = tk.StringVar(value="Nenhum circuito selecionado.")
        self.issue_var = tk.StringVar(value="link_interrompido")
        self.status_var = tk.StringVar(value="Pronto.")
        self.protocol_var = tk.StringVar()
        self.case_var = tk.StringVar()
        self.zabbix_var = tk.StringVar(value="Não solicitado.")

        self._build()
        self.root.after(self.POLL_MS, self._drain_events)
        self.root.after(450, self._offer_credentials_if_missing)
        self.root.after(550, self._refresh_zabbix_button)

    def _build(self) -> None:
        from tkinter import ttk

        self.root.title("Abrir Chamado Técnico Vivo")
        self.root.geometry("940x820")
        self.root.minsize(840, 760)
        self.root.option_add("*Font", ("Segoe UI", 10))
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        purple = "#660099"
        purple_dark = "#4B0073"
        purple_light = "#F0E6F5"
        page_bg = "#F7F5F9"
        card_bg = "#FFFFFF"
        text_main = "#2F2933"
        text_soft = "#6B6370"
        border = "#DED7E2"
        success = "#157A4A"

        self.root.configure(background=page_bg)
        try:
            icon_file = asset_path("app_icon.ico")
            if icon_file.is_file():
                self.root.iconbitmap(default=str(icon_file))
        except Exception:
            logging.debug("Não foi possível aplicar o ícone ICO.", exc_info=True)
        try:
            icon_png = asset_path("app_icon.png")
            if icon_png.is_file():
                self.app_icon_image = self.tk.PhotoImage(file=str(icon_png))
                self.root.iconphoto(True, self.app_icon_image)
        except Exception:
            logging.debug("Não foi possível aplicar o ícone PNG.", exc_info=True)

        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Page.TFrame", background=page_bg)
        style.configure("Header.TFrame", background=purple)
        style.configure(
            "Card.TLabelframe",
            background=card_bg,
            bordercolor=border,
            lightcolor=border,
            darkcolor=border,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=page_bg,
            foreground=purple_dark,
            font=("Segoe UI Semibold", 11),
        )
        style.configure("Card.TFrame", background=card_bg)
        style.configure("Card.TLabel", background=card_bg, foreground=text_main)
        style.configure("Muted.Card.TLabel", background=card_bg, foreground=text_soft)
        style.configure(
            "Success.Card.TLabel",
            background=card_bg,
            foreground=success,
            font=("Segoe UI Semibold", 10),
        )
        style.configure(
            "Primary.TButton",
            background=purple,
            foreground="#FFFFFF",
            bordercolor=purple,
            focusthickness=2,
            focuscolor=purple_light,
            padding=(16, 9),
            font=("Segoe UI Semibold", 10),
        )
        style.map(
            "Primary.TButton",
            background=[("active", purple_dark), ("pressed", purple_dark), ("disabled", "#C7B8CE")],
            foreground=[("disabled", "#F7F3F9")],
        )
        style.configure(
            "Secondary.TButton",
            background="#FFFFFF",
            foreground=purple_dark,
            bordercolor="#BFA8CC",
            padding=(12, 7),
            font=("Segoe UI Semibold", 9),
        )
        style.map("Secondary.TButton", background=[("active", purple_light)])
        style.configure("Small.TButton", padding=(10, 5), foreground=purple_dark)
        style.configure(
            "Vivo.Horizontal.TProgressbar",
            troughcolor="#EAE4ED",
            background=purple,
            bordercolor="#EAE4ED",
            lightcolor=purple,
            darkcolor=purple,
        )
        style.configure("TEntry", padding=7, fieldbackground="#FFFFFF", bordercolor=border)
        style.configure("TCombobox", padding=6, fieldbackground="#FFFFFF", bordercolor=border)
        style.configure("TRadiobutton", background=card_bg, foreground=text_main)

        header = self.tk.Frame(self.root, background=purple, height=106)
        header.pack(fill="x")
        header.pack_propagate(False)
        header_inner = self.tk.Frame(header, background=purple)
        header_inner.pack(fill="both", expand=True, padx=26, pady=14)
        try:
            logo_file = asset_path("vivo_wordmark.png")
            self.header_logo_image = self.tk.PhotoImage(file=str(logo_file))
            self.tk.Label(
                header_inner,
                image=self.header_logo_image,
                background=purple,
                borderwidth=0,
            ).pack(side="left", padx=(0, 24))
        except Exception:
            logging.debug("Não foi possível carregar o logotipo do cabeçalho.", exc_info=True)
            self.tk.Label(
                header_inner,
                text="vivo",
                background=purple,
                foreground="#FFFFFF",
                font=("Segoe UI Semibold", 28),
            ).pack(side="left", padx=(0, 24))
        header_copy = self.tk.Frame(header_inner, background=purple)
        header_copy.pack(side="left", fill="y")
        self.tk.Label(
            header_copy,
            text="Central de chamados técnicos",
            background=purple,
            foreground="#FFFFFF",
            font=("Segoe UI Semibold", 18),
        ).pack(anchor="w", pady=(5, 1))
        self.tk.Label(
            header_copy,
            text="Consulta, validação e abertura segura em um só lugar",
            background=purple,
            foreground="#EEDDF5",
            font=("Segoe UI", 10),
        ).pack(anchor="w")
        self.tk.Label(
            header_inner,
            text="AUTOMAÇÃO SEGURA",
            background="#FFFFFF",
            foreground=purple_dark,
            font=("Segoe UI Semibold", 9),
            padx=13,
            pady=7,
        ).pack(side="right", pady=18)

        outer = ttk.Frame(self.root, padding=(22, 12, 22, 10), style="Page.TFrame")
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        search = ttk.LabelFrame(
            outer, text="  1  LOCALIZAR LOJA  ", padding=(16, 9), style="Card.TLabelframe"
        )
        search.grid(row=0, column=0, sticky="ew")
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="VD ou host Zabbix", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10)
        )
        entry = ttk.Entry(search, textvariable=self.vd_var, width=20)
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<Return>", lambda _event: self.search())
        self.search_button = ttk.Button(
            search, text="Buscar loja", command=self.search, style="Primary.TButton"
        )
        self.search_button.grid(row=0, column=2, padx=(10, 0))
        ttk.Label(
            search,
            textvariable=self.store_var,
            wraplength=790,
            justify="left",
            style="Muted.Card.TLabel",
        ).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )

        circuit = ttk.LabelFrame(
            outer, text="  2  CONFIRMAR CIRCUITO  ", padding=(16, 9), style="Card.TLabelframe"
        )
        circuit.grid(row=1, column=0, sticky="ew", pady=(9, 0))
        circuit.columnconfigure(0, weight=1)
        self.link_combo = ttk.Combobox(
            circuit, textvariable=self.link_var, state="disabled", values=(), width=75
        )
        self.link_combo.grid(row=0, column=0, sticky="ew")
        self.link_combo.bind("<<ComboboxSelected>>", self._link_changed)
        ttk.Label(
            circuit,
            textvariable=self.link_detail_var,
            wraplength=790,
            justify="left",
            style="Muted.Card.TLabel",
        ).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )

        problem = ttk.LabelFrame(
            outer, text="  3  TIPO DE PROBLEMA  ", padding=(16, 8), style="Card.TLabelframe"
        )
        problem.grid(row=2, column=0, sticky="ew", pady=(9, 0))
        ttk.Radiobutton(
            problem,
            text="1 — Link inoperante",
            value="link_interrompido",
            variable=self.issue_var,
        ).grid(row=0, column=0, sticky="w", padx=(0, 24))
        ttk.Radiobutton(
            problem,
            text="2 — Perda de pacotes",
            value="perca_pacotes",
            variable=self.issue_var,
        ).grid(row=0, column=1, sticky="w")

        actions = ttk.Frame(outer, style="Page.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.login_button = ttk.Button(
            actions,
            text="Configurar login do portal da Vivo",
            command=self.configure_credentials,
            style="Secondary.TButton",
        )
        self.login_button.pack(side="left")
        self.zabbix_button = ttk.Button(
            actions,
            text="Configurar API do Zabbix",
            command=self.configure_zabbix,
            style="Secondary.TButton",
        )
        self.zabbix_button.pack(side="left", padx=(8, 0))
        self.batch_button = ttk.Button(
            actions,
            text="Abrir vários VDs",
            command=self.open_batch_dialog,
            style="Secondary.TButton",
        )
        self.batch_button.pack(side="left", padx=(8, 0))
        self.start_button = ttk.Button(
            actions,
            text="Confirmar e iniciar",
            command=self.start_ticket,
            state="disabled",
            style="Primary.TButton",
        )
        self.start_button.pack(side="right")

        progress = ttk.LabelFrame(
            outer, text="  ANDAMENTO  ", padding=(16, 9), style="Card.TLabelframe"
        )
        progress.grid(row=4, column=0, sticky="nsew", pady=(9, 0))
        outer.rowconfigure(4, weight=1, minsize=96)
        progress.columnconfigure(0, weight=1)
        progress.rowconfigure(1, weight=1, minsize=64)
        ttk.Label(progress, textvariable=self.status_var, style="Card.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.progress_bar = ttk.Progressbar(
            progress, mode="indeterminate", style="Vivo.Horizontal.TProgressbar", length=120
        )
        self.progress_bar.grid(row=0, column=1, sticky="e", padx=(10, 0))
        self.log_text = self.tk.Text(
            progress,
            height=4,
            wrap="word",
            state="disabled",
            background="#FBFAFC",
            foreground=text_main,
            selectbackground=purple,
            selectforeground="#FFFFFF",
            relief="solid",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#E6E0E9",
            font=("Consolas", 9),
            padx=9,
            pady=7,
        )
        self.log_text.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(7, 0))
        self.log_scrollbar = ttk.Scrollbar(
            progress, orient="vertical", command=self.log_text.yview
        )
        self.log_scrollbar.grid(row=1, column=2, sticky="ns", padx=(7, 0), pady=(7, 0))
        self.log_text.configure(yscrollcommand=self.log_scrollbar.set)

        result = ttk.LabelFrame(
            outer, text="  RESULTADO  ", padding=(16, 10), style="Card.TLabelframe"
        )
        result.grid(row=5, column=0, sticky="ew", pady=(9, 0))
        self.result_frame = result
        result.columnconfigure(1, weight=1)
        ttk.Label(result, text="Protocolo Vivo", style="Success.Card.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(result, textvariable=self.protocol_var, state="readonly").grid(
            row=0, column=1, sticky="ew", padx=8
        )
        self.copy_button = ttk.Button(
            result,
            text="Copiar protocolo",
            command=self.copy_protocol,
            state="disabled",
            style="Small.TButton",
        )
        self.copy_button.grid(row=0, column=2)
        ttk.Label(result, text="Número do caso", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Entry(result, textvariable=self.case_var, state="readonly").grid(
            row=1, column=1, sticky="ew", padx=8, pady=(8, 0)
        )
        ttk.Button(result, text="Copiar caso", command=self.copy_case, style="Small.TButton").grid(
            row=1, column=2, pady=(8, 0)
        )
        ttk.Label(result, text="Reconhecimento Zabbix", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Label(
            result,
            textvariable=self.zabbix_var,
            wraplength=520,
            justify="left",
            style="Muted.Card.TLabel",
        ).grid(row=2, column=1, columnspan=2, sticky="w", padx=8, pady=(8, 0))

        ttk.Label(
            outer,
            text="Robô Vivo  •  abertura monitorada  •  credenciais protegidas",
            background=page_bg,
            foreground=text_soft,
            font=("Segoe UI", 8),
        ).grid(row=6, column=0, sticky="e", pady=(5, 0))

        entry.focus_set()

    def _post(self, kind: str, value: Any = None) -> None:
        self.events.put((kind, value))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "lookup_ok":
                    self._lookup_ok(value)
                elif kind == "lookup_error":
                    self._lookup_error(value)
                elif kind == "worker":
                    self._handle_worker_event(value)
                elif kind == "worker_exit":
                    self._handle_worker_exit(value)
                elif kind == "spawn_error":
                    if self.batch_active and self.worker_kind == "batch":
                        self._batch_fail_unfinished(
                            f"Não foi possível iniciar a automação: {value}"
                        )
                        self.worker_finished = True
                        self.worker = None
                        self.worker_kind = ""
                        self._finish_batch()
                    else:
                        self._worker_error(value)
                        if self.batch_active:
                            self.worker_finished = True
                            self.worker = None
                            self.worker_kind = ""
                            self.root.after(250, self._start_next_batch)
                elif kind == "batch_prepared":
                    self._batch_prepared(value)
                elif kind == "batch_prepare_error":
                    self._batch_prepare_error(value)
                elif kind == "zabbix_single_done":
                    self._zabbix_single_done(value)
                elif kind == "zabbix_batch_done":
                    self._zabbix_batch_done(value)
        except queue.Empty:
            pass
        self.root.after(self.POLL_MS, self._drain_events)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.status_var.set(message or ("Processando..." if busy else "Pronto."))
        if busy:
            # Durante a execução, usa o espaço do resultado ainda vazio para
            # exibir mais linhas do acompanhamento. O grid() restaura a mesma
            # posição assim que a operação termina.
            self.result_frame.grid_remove()
            self.progress_bar.start(12)
        else:
            self.progress_bar.stop()
            self.result_frame.grid()

    def _append_log(self, message: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def search(self) -> None:
        from tkinter import messagebox

        if self.batch_active or not self.worker_finished or self.zabbix_single_pending:
            messagebox.showinfo("Operação em andamento", "Aguarde a operação atual terminar.")
            return
        try:
            raw_value = self.vd_var.get()
            vd = clean_vd(raw_value)
            self.current_zabbix_host = extract_zabbix_host(raw_value)
        except ValueError as exc:
            messagebox.showwarning("VD necessário", str(exc))
            return
        self.vd_var.set(vd)
        self.search_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.link_combo.configure(state="disabled", values=())
        self.store = None
        self.links = []
        self.ticket = None
        self.ticket_started = False
        self.submission_seen = False
        self.protocol_var.set("")
        self.case_var.set("")
        self.zabbix_var.set(
            f"Aguardando protocolo para {self.current_zabbix_host}."
            if self.current_zabbix_host
            else f"Aguardando protocolo; o host será localizado automaticamente pelo {vd}."
        )
        self.copy_button.configure(state="disabled")
        self._set_busy(True, "Consultando as bases...")
        self._append_log(f"Consultando {vd} nas bases de lojas e designações.")
        threading.Thread(target=self._lookup_thread, args=(vd,), daemon=True).start()

    def _lookup_thread(self, vd: str) -> None:
        try:
            if self.adapter is None:
                self.adapter = DataAdapter(self.config)
            store, links = self.adapter.lookup(vd)
            self._post("lookup_ok", (store, links))
        except Exception as exc:
            logging.exception("Falha ao consultar %s", vd)
            self._post("lookup_error", friendly_error(exc))

    def _lookup_ok(self, value: tuple[dict[str, Any], list[dict[str, Any]]]) -> None:
        from tkinter import messagebox

        self.store, self.links = value
        self.search_button.configure(state="normal")
        self._set_busy(False, "Dados encontrados. Confira antes de continuar.")
        name = field(self.store, "nome", "nome_loja", default="Nome não informado")
        address = field(self.store, "endereco", default="Endereço não informado")
        city = field(self.store, "cidade")
        state = field(self.store, "uf")
        schedule = field(self.store, "horario_script", "horario", default="Não informado")
        start = field(self.store, "hora_inicio")
        finish = field(self.store, "hora_fim", "hora_termino")
        if start or finish:
            schedule = f"{start or '--:--'} às {finish or '--:--'}"
        self.store_var.set(
            f"{self.vd_var.get()} — {name}\n{address} — {city}/{state}\n"
            f"Horário de funcionamento: {schedule}"
        )
        if not self.links:
            self.link_detail_var.set("Nenhuma designação foi encontrada para esta loja.")
            messagebox.showwarning(
                "Circuito não encontrado",
                "A loja foi encontrada, mas não há circuito na base de designações.",
            )
            return
        values = [self._link_label(item, index) for index, item in enumerate(self.links)]
        self.link_combo.configure(state="readonly", values=values)
        self.link_combo.current(0)
        self._link_changed()
        self.start_button.configure(state="normal")
        self._append_log(
            f"Loja encontrada: {name}. {len(self.links)} circuito(s) disponível(is)."
        )

    def _lookup_error(self, message: str) -> None:
        from tkinter import messagebox

        self.search_button.configure(state="normal")
        self._set_busy(False, "Não foi possível localizar a loja.")
        self.store_var.set("Loja não localizada.")
        self._append_log(f"Falha na consulta: {message}")
        messagebox.showerror("Consulta das bases", message)

    def _link_label(self, link: dict[str, Any], index: int) -> str:
        designation = field(link, "designacao", "identificacao", default="Sem designação")
        carrier = field(link, "operadora", default="Operadora não informada")
        access = field(link, "tipo_acesso", "produto", default="Acesso não informado")
        return f"{index + 1}. {designation} | {carrier} | {access}"

    def _selected_link(self) -> Optional[dict[str, Any]]:
        if not self.links:
            return None
        index = self.link_combo.current()
        if index < 0:
            index = 0
        return self.links[index]

    def _link_changed(self, _event: Any = None) -> None:
        link = self._selected_link()
        if not link:
            return
        designation = field(link, "designacao", "identificacao", default="Não informada")
        user = field(link, "usuario", "id_servico", default="Não informado")
        carrier = field(link, "operadora", default="Não informada")
        access = field(link, "tipo_acesso", "produto", default="Não informado")
        speed = field(link, "velocidade", default="Não informada")
        self.link_detail_var.set(
            f"Designação/identificação: {designation}\n"
            f"Usuário/ID: {user}  •  Operadora: {carrier}  •  Produto: {access}  •  Velocidade: {speed}"
        )

    def _node_and_worker(self) -> tuple[str, Path]:
        return find_node_and_worker()

    def _credential_api(self) -> Any:
        return credential_api()

    def _load_credentials(self) -> Any:
        return self._credential_api().carregar()

    def _load_zabbix_credentials(self) -> Any:
        return self._credential_api().carregar_zabbix()

    def _zabbix_client(self, credentials: Any) -> Any:
        return zabbix_client_for(credentials)

    def _refresh_zabbix_button(self) -> None:
        try:
            configured = self._load_zabbix_credentials()
        except Exception as exc:
            logging.exception("Falha ao consultar a configuração do Zabbix")
            self._append_log(f"Não foi possível consultar o Zabbix: {friendly_error(exc)}")
            return
        self.zabbix_button.configure(
            text="Alterar API do Zabbix" if configured else "Configurar API do Zabbix"
        )

    def _offer_credentials_if_missing(self) -> None:
        try:
            credentials = self._load_credentials()
        except Exception as exc:
            logging.exception("Falha ao consultar o acesso automático")
            self._append_log(f"Não foi possível consultar o acesso automático: {friendly_error(exc)}")
            return
        if credentials:
            self.login_button.configure(text="Alterar login do portal da Vivo")
            return
        self.configure_credentials(first_use=True)

    def _clear_robot_profile(self) -> None:
        """Remove somente a sessão do perfil exclusivo deste robô."""

        clear_robot_profile()

    def configure_credentials(self, first_use: bool = False) -> None:
        from tkinter import messagebox

        if self.batch_active or not self.worker_finished or self.zabbix_single_pending:
            messagebox.showinfo("Operação em andamento", "Aguarde a operação atual terminar.")
            return
        try:
            api = self._credential_api()
            current = api.carregar()
        except Exception as exc:
            messagebox.showerror("Acesso automático", friendly_error(exc))
            return

        window = self.tk.Toplevel(self.root)
        window.title("Acesso automático ao Portal Vivo")
        window.resizable(False, False)
        window.transient(self.root)
        window.grab_set()

        frame = self.ttk.Frame(window, padding=18)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        self.ttk.Label(
            frame,
            text="Informe uma vez. O Windows protegerá a senha neste computador.",
            wraplength=430,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
        self.ttk.Label(frame, text="CPF").grid(row=1, column=0, sticky="w", padx=(0, 10))
        cpf_var = self.tk.StringVar(value=current.cpf if current else "")
        cpf_entry = self.ttk.Entry(frame, textvariable=cpf_var, width=34)
        cpf_entry.grid(row=1, column=1, sticky="ew")
        self.ttk.Label(frame, text="Senha").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=(10, 0)
        )
        password_var = self.tk.StringVar()
        password_entry = self.ttk.Entry(frame, textvariable=password_var, show="●", width=34)
        password_entry.grid(row=2, column=1, sticky="ew", pady=(10, 0))
        self.ttk.Label(
            frame,
            text="A senha não será gravada no BAT, nas planilhas nem nos registros do robô.",
            wraplength=430,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))

        buttons = self.ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(18, 0))
        self.ttk.Button(buttons, text="Cancelar", command=window.destroy).pack(side="left")

        def save() -> None:
            try:
                # Trocar o colaborador também deve invalidar qualquer sessão
                # anterior mantida pelo perfil exclusivo do robô.
                self._clear_robot_profile()
                api.salvar(cpf_var.get(), password_var.get())
            except Exception as exc:
                logging.exception("Falha ao salvar acesso automático")
                messagebox.showerror("Acesso automático", friendly_error(exc), parent=window)
                return
            password_var.set("")
            self.login_button.configure(text="Alterar login do portal da Vivo")
            self._append_log("Acesso automático configurado e protegido pelo Windows.")
            window.destroy()
            messagebox.showinfo(
                "Acesso automático",
                "Acesso salvo com segurança. Nas próximas aberturas o robô preencherá o login sozinho.",
                parent=self.root,
            )

        self.ttk.Button(buttons, text="Salvar acesso", command=save).pack(side="left", padx=(8, 0))
        if first_use:
            window.protocol("WM_DELETE_WINDOW", window.destroy)
        (password_entry if current else cpf_entry).focus_set()

    def configure_zabbix(self) -> None:
        from tkinter import messagebox

        if self.batch_active or not self.worker_finished or self.zabbix_single_pending:
            messagebox.showinfo("Operação em andamento", "Aguarde a operação atual terminar.")
            return
        try:
            api = self._credential_api()
            current = api.carregar_zabbix()
            if str(APP_DIR) not in sys.path:
                sys.path.insert(0, str(APP_DIR))
            from zabbix_client import ZabbixClient, normalize_api_url  # type: ignore
        except Exception as exc:
            messagebox.showerror("Integração Zabbix", friendly_error(exc))
            return

        window = self.tk.Toplevel(self.root)
        window.title("Integração com o Zabbix")
        window.resizable(False, False)
        window.transient(self.root)
        window.grab_set()
        frame = self.ttk.Frame(window, padding=18)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        self.ttk.Label(
            frame,
            text=(
                "Depois que o Protocolo Vivo for recebido, o robô poderá reconhecer "
                "o incidente do host exato e registrar o protocolo como mensagem."
            ),
            wraplength=520,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
        self.ttk.Label(frame, text="URL da API").grid(row=1, column=0, sticky="w", padx=(0, 10))
        url_var = self.tk.StringVar(
            value=current.api_url if current else "http://172.16.2.250/api_jsonrpc.php"
        )
        url_entry = self.ttk.Entry(frame, textvariable=url_var, width=54)
        url_entry.grid(row=1, column=1, sticky="ew")
        self.ttk.Label(frame, text="Token da API").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=(10, 0)
        )
        token_var = self.tk.StringVar()
        token_entry = self.ttk.Entry(frame, textvariable=token_var, show="●", width=54)
        token_entry.grid(row=2, column=1, sticky="ew", pady=(10, 0))
        if current:
            self.ttk.Label(
                frame,
                text="Deixe o token vazio para conservar o token já protegido pelo Windows.",
                wraplength=520,
                justify="left",
            ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        automatic_var = self.tk.BooleanVar(value=current.automatico if current else True)
        self.ttk.Checkbutton(
            frame,
            text="Reconhecer automaticamente somente quando houver Protocolo Vivo",
            variable=automatic_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(14, 0))
        self.ttk.Label(
            frame,
            text=(
                "O token ficará no Gerenciador de Credenciais do Windows. "
                "Se o host ou o incidente não forem únicos, nada será alterado."
            ),
            wraplength=520,
            justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        buttons = self.ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(18, 0))
        self.ttk.Button(buttons, text="Cancelar", command=window.destroy).pack(side="left")

        def selected_token() -> str:
            return token_var.get().strip() or (current.token if current else "")

        def test_connection() -> None:
            try:
                client = ZabbixClient(normalize_api_url(url_var.get()), selected_token())
                version = client.test_connection()
            except Exception as exc:
                messagebox.showerror("Teste do Zabbix", friendly_error(exc), parent=window)
                return
            messagebox.showinfo(
                "Teste do Zabbix",
                f"Conexão e token validados com sucesso.\n\nVersão: {version}",
                parent=window,
            )

        def save() -> None:
            try:
                normalized_url = normalize_api_url(url_var.get())
                api.salvar_zabbix(normalized_url, selected_token(), automatic_var.get())
            except Exception as exc:
                logging.exception("Falha ao salvar configuração do Zabbix")
                messagebox.showerror("Integração Zabbix", friendly_error(exc), parent=window)
                return
            token_var.set("")
            self.zabbix_button.configure(text="Alterar API do Zabbix")
            self._append_log("Integração Zabbix configurada e token protegido pelo Windows.")
            window.destroy()
            messagebox.showinfo(
                "Integração Zabbix",
                "Configuração salva. Use hosts completos DSP/DPA para o reconhecimento automático.",
                parent=self.root,
            )

        self.ttk.Button(buttons, text="Testar conexão", command=test_connection).pack(
            side="left", padx=(8, 0)
        )
        self.ttk.Button(buttons, text="Salvar", command=save).pack(side="left", padx=(8, 0))
        (token_entry if current else url_entry).focus_set()

    def open_batch_dialog(self) -> None:
        """Solicita os VDs e inicia um lote com até cinco abas simultâneas."""

        from tkinter import messagebox

        if self.batch_active or not self.worker_finished or self.zabbix_single_pending:
            messagebox.showinfo("Operação em andamento", "Aguarde a operação atual terminar.")
            return
        try:
            credentials = self._load_credentials()
            node, worker_path = self._node_and_worker()
        except Exception as exc:
            messagebox.showerror("Abertura em lote", friendly_error(exc))
            return
        if not credentials:
            messagebox.showwarning(
                "Configure o acesso",
                "Cadastre o CPF e a senha antes de iniciar uma abertura em lote.",
            )
            self.configure_credentials(first_use=True)
            return

        window = self.tk.Toplevel(self.root)
        window.title("Abrir chamados para vários VDs")
        window.geometry("610x455")
        window.minsize(560, 420)
        window.transient(self.root)
        window.grab_set()

        frame = self.ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        self.ttk.Label(
            frame,
            text="Informe os VDs separados por espaço, vírgula, ponto e vírgula ou uma linha por loja.",
            wraplength=560,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        self.ttk.Label(
            frame,
            text=(
                "Exemplo: VD1517, VD675, 2056 ou hosts como "
                "DSP_2188_MPLS_VIVO e DPA_1842_MPLS_VIVO. "
                "Repetidos serão ignorados (máximo: 50)."
            ),
            wraplength=560,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(4, 10))
        vd_text = self.tk.Text(frame, height=10, wrap="word")
        vd_text.grid(row=2, column=0, sticky="nsew")

        issue = self.tk.StringVar(value=self.issue_var.get())
        problem = self.ttk.LabelFrame(frame, text="Tipo de problema para todo o lote", padding=10)
        problem.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.ttk.Radiobutton(
            problem,
            text="Link inoperante",
            value="link_interrompido",
            variable=issue,
        ).pack(side="left", padx=(0, 24))
        self.ttk.Radiobutton(
            problem,
            text="Perda de pacotes",
            value="perca_pacotes",
            variable=issue,
        ).pack(side="left")

        buttons = self.ttk.Frame(frame)
        buttons.grid(row=4, column=0, sticky="e", pady=(16, 0))
        self.ttk.Button(buttons, text="Cancelar", command=window.destroy).pack(side="left")

        def begin() -> None:
            try:
                jobs = parse_vd_jobs(vd_text.get("1.0", "end"))
            except ValueError as exc:
                messagebox.showwarning("Lista de VDs", str(exc), parent=window)
                return
            selected_issue = issue.get()
            window.destroy()
            self._begin_batch_preparation(
                jobs, selected_issue, credentials, node, worker_path
            )

        self.ttk.Button(buttons, text="Revisar lote", command=begin).pack(
            side="left", padx=(8, 0)
        )
        vd_text.focus_set()

    def _begin_batch_preparation(
        self,
        jobs: list[dict[str, str]],
        issue: str,
        credentials: Any,
        node: str,
        worker_path: Path,
    ) -> None:
        self.batch_active = True
        self.batch_queue = []
        self.batch_results = []
        self.batch_current = None
        self.batch_credentials = credentials
        self.batch_node = node
        self.batch_worker_path = worker_path
        self.batch_total = len(jobs)
        self.batch_position = 0
        self.batch_issue = issue
        self.batch_items_by_request = {}
        self.batch_zabbix_started = False
        self.issue_var.set(issue)
        self.search_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.login_button.configure(state="disabled")
        self.zabbix_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.link_combo.configure(state="disabled")
        self._set_busy(True, f"Preparando lote com {len(jobs)} VD(s)...")
        self._append_log(f"Preparando abertura em lote para {len(jobs)} VD(s).")
        threading.Thread(
            target=self._prepare_batch_thread,
            args=(jobs, issue),
            daemon=True,
        ).start()

    def _prepare_batch_thread(self, jobs: list[dict[str, str]], issue: str) -> None:
        items: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        try:
            if self.adapter is None:
                self.adapter = DataAdapter(self.config)
            for position, job in enumerate(jobs, start=1):
                vd = job["vd"]
                zabbix_host = job.get("zabbix_host", "")
                try:
                    store, links = self.adapter.lookup(vd)
                    if not links:
                        raise RuntimeError("Nenhum circuito foi encontrado na base.")
                    link = links[0]
                    designation = str(field(link, "designacao", "identificacao", default=""))
                    ticket = self.adapter.build_ticket(vd, issue, designation)
                    payload = self._build_payload(
                        ticket,
                        store,
                        link,
                        vd_value=vd,
                        issue_value=issue,
                    )
                    items.append(
                        {
                            "position": position,
                            "vd": vd,
                            "zabbix_host": zabbix_host,
                            "store": store,
                            "links": links,
                            "link": link,
                            "ticket": ticket,
                            "payload": payload,
                        }
                    )
                except Exception as exc:
                    logging.exception("Falha ao preparar %s no lote", vd)
                    failures.append(
                        {
                            "position": str(position),
                            "vd": vd,
                            "zabbix_host": zabbix_host,
                            "status": "ERRO NA BASE",
                            "protocol": "",
                            "case_number": "",
                            "message": friendly_error(exc),
                            "zabbix_status": "NÃO SOLICITADO",
                            "zabbix_detail": "Chamado Vivo não preparado.",
                        }
                    )
            self._post("batch_prepared", {"items": items, "failures": failures})
        except Exception as exc:
            logging.exception("Falha geral na preparação do lote")
            self._post(
                "batch_prepare_error",
                {"message": friendly_error(exc), "jobs": jobs},
            )

    def _batch_prepared(self, value: Mapping[str, Any]) -> None:
        from tkinter import messagebox

        items = list(value.get("items") or [])
        failures = list(value.get("failures") or [])
        self.batch_results.extend(failures)
        for item in items:
            item["duplicate"] = self._recent_duplicate(item["payload"])

        lines: list[str] = []
        for item in items:
            store = item["store"]
            link = item["link"]
            duplicate = "  [POSSÍVEL DUPLICIDADE]" if item.get("duplicate") else ""
            host_text = str(item.get("zabbix_host") or "LOCALIZAR PELO VD")
            lines.append(
                f"{item['position']}. {item['vd']} — "
                f"{host_text} — "
                f"{field(store, 'nome', 'nome_loja', default='Loja sem nome')} — "
                f"{field(link, 'designacao', 'identificacao', default='sem identificação')}"
                f"{duplicate}"
            )
        for failure in failures:
            lines.append(
                f"{failure.get('position')}. {failure.get('vd')} — NÃO SERÁ PROCESSADO: "
                f"{failure.get('message')}"
            )
        lines.sort(key=lambda line: int(line.split(".", 1)[0]))
        shown = lines[:30]
        if len(lines) > len(shown):
            shown.append(f"... e mais {len(lines) - len(shown)} item(ns).")
        issue_label = "Link inoperante" if self.batch_issue == "link_interrompido" else "Perda de pacotes"
        summary = (
            f"Problema aplicado a todo o lote: {issue_label}\n\n"
            + "\n".join(shown)
            + "\n\nO robô usará a mesma sessão do navegador, com até cinco abas "
            "ativas ao mesmo tempo. Cada confirmação continuará identificada pelo VD, "
            "e um erro em uma loja não interromperá as demais."
        )

        if not items:
            self._append_log("Nenhum VD válido ficou disponível para processamento.")
            self._finish_batch()
            return
        if not messagebox.askyesno(
            "Confirmar abertura em lote",
            summary + "\n\nAutoriza iniciar o lote?",
            icon="warning",
        ):
            for item in items:
                self.batch_results.append(
                    {
                        "position": str(item["position"]),
                        "vd": item["vd"],
                        "zabbix_host": str(item.get("zabbix_host") or ""),
                        "zabbix_status": "NÃO SOLICITADO",
                        "zabbix_detail": "Lote cancelado antes da abertura.",
                        "status": "CANCELADO",
                        "protocol": "",
                        "case_number": "",
                        "message": "Lote cancelado na revisão inicial.",
                    }
                )
            self._append_log("Abertura em lote cancelada na revisão inicial.")
            self._finish_batch()
            return

        self._append_log(
            f"Lote autorizado: {len(items)} chamado(s) válido(s); "
            f"{len(failures)} erro(s) de preparação."
        )
        self._start_parallel_batch(items)

    def _batch_prepare_error(self, value: Mapping[str, Any]) -> None:
        message = str(value.get("message") or "Não foi possível preparar o lote.")
        for position, job in enumerate(value.get("jobs") or [], start=1):
            self.batch_results.append(
                {
                    "position": str(position),
                    "vd": str(job.get("vd") or ""),
                    "zabbix_host": str(job.get("zabbix_host") or ""),
                    "zabbix_status": "NÃO SOLICITADO",
                    "zabbix_detail": "Chamado Vivo não preparado.",
                    "status": "ERRO NA PREPARAÇÃO",
                    "protocol": "",
                    "case_number": "",
                    "message": message,
                }
            )
        self._append_log(f"ERRO ao preparar lote: {message}")
        self._finish_batch()

    def _start_parallel_batch(self, items: list[dict[str, Any]]) -> None:
        """Inicia um único Chrome e mantém até cinco abas ligadas ao seu VD."""

        if not self.batch_active:
            return
        self.batch_queue = []
        self.batch_current = None
        self.batch_items_by_request = {}
        for item in items:
            item["recorded"] = False
            item["submitted"] = False
            request_id = str(item.get("payload", {}).get("request_id") or "")
            if not request_id:
                request_id = str(uuid.uuid4())
                item["payload"]["request_id"] = request_id
            self.batch_items_by_request[request_id] = item

        self.ticket_started = True
        self.submission_seen = False
        self.terminal_event_seen = False
        self.worker_finished = False
        self.worker_kind = "batch"
        self._set_busy(
            True,
            f"Processando {len(items)} chamado(s) em até {self.batch_concurrency} abas...",
        )
        self._append_log(
            f"Navegador compartilhado iniciado: até {self.batch_concurrency} abas ativas."
        )
        request = {
            "concurrency": self.batch_concurrency,
            "tickets": [item["payload"] for item in items],
        }
        serialized = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        assert self.batch_worker_path is not None
        self._spawn_worker(
            [self.batch_node, str(self.batch_worker_path), "--open-batch", serialized],
            environment={
                "ROBO_VIVO_CPF": self.batch_credentials.cpf,
                "ROBO_VIVO_PASSWORD": self.batch_credentials.senha,
            },
        )

    def _batch_item_for_event(
        self, event: Mapping[str, Any]
    ) -> Optional[dict[str, Any]]:
        request_id = str(event.get("request_id") or event.get("requestId") or event.get("id") or "")
        return self.batch_items_by_request.get(request_id)

    def _display_batch_item(self, item: Mapping[str, Any]) -> None:
        """Exibe no painel principal o VD referente ao evento atual."""

        self.batch_current = dict(item) if not isinstance(item, dict) else item
        self.batch_position = int(item.get("position") or 0)
        self.vd_var.set(str(item.get("vd") or ""))
        self.store = item.get("store") or {}
        self.links = list(item.get("links") or [])
        self.ticket = item.get("ticket") or {}
        self.issue_var.set(self.batch_issue)
        values = [self._link_label(link, index) for index, link in enumerate(self.links)]
        self.link_combo.configure(state="disabled", values=values)
        if values:
            self.link_combo.current(0)
            self._link_changed()
        name = field(self.store, "nome", "nome_loja", default="Nome não informado")
        address = field(self.store, "endereco", default="Endereço não informado")
        city = field(self.store, "cidade")
        state = field(self.store, "uf")
        schedule = field(self.store, "horario_script", "horario", default="Não informado")
        start = field(self.store, "hora_inicio")
        finish = field(self.store, "hora_fim", "hora_termino")
        if start or finish:
            schedule = f"{start or '--:--'} às {finish or '--:--'}"
        self.store_var.set(
            f"{item.get('vd')} — {name}\n{address} — {city}/{state}\n"
            f"Horário de funcionamento: {schedule}"
        )
        host = str(item.get("zabbix_host") or "")
        self.zabbix_var.set(
            f"Aguardando protocolo para {host}."
            if host
            else "Host completo não informado para este VD."
        )

    def _batch_record_item(
        self,
        item: Optional[dict[str, Any]],
        status: str,
        protocol: str = "",
        case_number: str = "",
        lp: str = "",
        message: str = "",
    ) -> None:
        if not item or item.get("recorded"):
            return
        item["recorded"] = True
        link = item.get("link") or {}
        portal_lp = lp or str(field(link, "designacao", "identificacao", default=""))
        self.batch_results.append(
            {
                "position": str(item.get("position", "")),
                "vd": str(item.get("vd", "")),
                "zabbix_host": str(item.get("zabbix_host", "")),
                "zabbix_status": "AGUARDANDO" if protocol else "NÃO SOLICITADO",
                "zabbix_detail": "",
                "status": status,
                "protocol": protocol,
                "case_number": case_number,
                "lp": portal_lp,
                "message": message,
            }
        )

    def _batch_fail_unfinished(self, message: str) -> None:
        for item in self.batch_items_by_request.values():
            if item.get("recorded"):
                continue
            submitted = bool(item.get("submitted"))
            status = "ENVIO INCERTO" if submitted else "ERRO"
            if submitted:
                self._write_history(status, "", "", item=item)
            self._batch_record_item(item, status, message=message)

    def _zabbix_update_result(
        self, result: dict[str, str], credentials: Any
    ) -> dict[str, str]:
        return zabbix_update_result(
            self.config, result, credentials, client_factory=self._zabbix_client
        )

    def _start_batch_zabbix(self) -> None:
        if self.batch_zabbix_started:
            return
        self.batch_zabbix_started = True
        try:
            credentials = self._load_zabbix_credentials()
        except Exception as exc:
            logging.exception("Falha ao carregar token do Zabbix")
            credentials = None
            self._append_log(f"Zabbix não configurado: {friendly_error(exc)}")
        self.worker_kind = "zabbix"
        self._set_busy(True, "Reconhecendo incidentes no Zabbix...")
        self._append_log("Iniciando reconhecimento seguro dos protocolos no Zabbix.")

        def run() -> None:
            updated: list[dict[str, str]] = []
            for result in self.batch_results:
                updated.append(self._zabbix_update_result(dict(result), credentials))
            self._post("zabbix_batch_done", updated)

        threading.Thread(target=run, daemon=True).start()

    def _zabbix_batch_done(self, value: Any) -> None:
        self.batch_results = [dict(item) for item in (value or [])]
        recognized = sum(
            1 for item in self.batch_results if item.get("zabbix_status") == "RECONHECIDO"
        )
        pending = sum(
            1 for item in self.batch_results if item.get("zabbix_status") == "PENDENTE"
        )
        self._append_log(
            f"Zabbix: {recognized} reconhecido(s), {pending} pendente(s)."
        )
        self.worker_kind = ""
        self._finish_batch()

    def _start_single_zabbix(self, protocol: str, case_number: str) -> None:
        self.zabbix_single_pending = True
        try:
            credentials = self._load_zabbix_credentials()
        except Exception as exc:
            logging.exception("Falha ao carregar token do Zabbix")
            credentials = None
        result = {
            "position": "1",
            "vd": self.vd_var.get(),
            "zabbix_host": self.current_zabbix_host,
            "status": "ABERTO",
            "protocol": protocol,
            "case_number": case_number,
            "message": "Chamado aberto com sucesso.",
        }
        self.zabbix_var.set("Reconhecimento em andamento...")
        self._set_busy(True, "Protocolo recebido. Reconhecendo no Zabbix...")

        def run() -> None:
            self._post("zabbix_single_done", self._zabbix_update_result(result, credentials))

        threading.Thread(target=run, daemon=True).start()

    def _zabbix_single_done(self, value: Mapping[str, Any]) -> None:
        from tkinter import messagebox

        status = str(value.get("zabbix_status") or "PENDENTE")
        detail = str(value.get("zabbix_detail") or "")
        protocol = str(value.get("protocol") or "")
        case_number = str(value.get("case_number") or "")
        self.zabbix_single_pending = False
        self.zabbix_var.set(f"{status}: {detail}".strip())
        self._set_busy(False, "Chamado aberto; resultado do Zabbix registrado.")
        self._append_log(f"Zabbix {status}: {detail}")
        text = (
            f"Chamado aberto com sucesso.\n\nProtocolo Vivo: {protocol}\n"
            f"Número do caso: {case_number or 'não informado'}\n\n"
            f"Zabbix: {status}\n{detail}"
        )
        if status == "RECONHECIDO":
            messagebox.showinfo("Chamado aberto e reconhecido", text)
        else:
            messagebox.showwarning("Chamado aberto; Zabbix pendente", text)

    def _start_next_batch(self) -> None:
        if not self.batch_active:
            return
        if not self.batch_queue:
            self._finish_batch()
            return

        item = self.batch_queue.pop(0)
        item["recorded"] = False
        self.batch_current = item
        self.batch_position = int(item["position"])
        self.vd_var.set(item["vd"])
        self.store = item["store"]
        self.links = item["links"]
        self.ticket = item["ticket"]
        self.issue_var.set(self.batch_issue)
        values = [self._link_label(link, index) for index, link in enumerate(self.links)]
        self.link_combo.configure(state="disabled", values=values)
        self.link_combo.current(0)
        self._link_changed()

        name = field(self.store, "nome", "nome_loja", default="Nome não informado")
        address = field(self.store, "endereco", default="Endereço não informado")
        city = field(self.store, "cidade")
        state = field(self.store, "uf")
        schedule = field(self.store, "horario_script", "horario", default="Não informado")
        start = field(self.store, "hora_inicio")
        finish = field(self.store, "hora_fim", "hora_termino")
        if start or finish:
            schedule = f"{start or '--:--'} às {finish or '--:--'}"
        self.store_var.set(
            f"{item['vd']} — {name}\n{address} — {city}/{state}\n"
            f"Horário de funcionamento: {schedule}"
        )
        self.protocol_var.set("")
        self.case_var.set("")
        self.copy_button.configure(state="disabled")
        self.ticket_started = True
        self.submission_seen = False
        self.terminal_event_seen = False
        self.worker_finished = False
        self.worker_kind = "ticket"
        self._set_busy(
            True,
            f"Processando {self.batch_position}/{self.batch_total}: {item['vd']}...",
        )
        self._append_log(
            f"Lote {self.batch_position}/{self.batch_total}: iniciando {item['vd']}."
        )
        serialized = json.dumps(item["payload"], ensure_ascii=False, separators=(",", ":"))
        assert self.batch_worker_path is not None
        self._spawn_worker(
            [self.batch_node, str(self.batch_worker_path), "--open-ticket", serialized],
            environment={
                "ROBO_VIVO_CPF": self.batch_credentials.cpf,
                "ROBO_VIVO_PASSWORD": self.batch_credentials.senha,
            },
        )

    def _batch_record(
        self,
        status: str,
        protocol: str = "",
        case_number: str = "",
        message: str = "",
    ) -> None:
        item = self.batch_current
        if not item or item.get("recorded"):
            return
        item["recorded"] = True
        self.batch_results.append(
            {
                "position": str(item.get("position", "")),
                "vd": str(item.get("vd", "")),
                "zabbix_host": str(item.get("zabbix_host", "")),
                "zabbix_status": "AGUARDANDO" if protocol else "NÃO SOLICITADO",
                "zabbix_detail": "",
                "status": status,
                "protocol": protocol,
                "case_number": case_number,
                "message": message,
            }
        )

    def _batch_stop_remaining(self, message: str) -> None:
        for item in self.batch_queue:
            self.batch_results.append(
                {
                    "position": str(item.get("position", "")),
                    "vd": str(item.get("vd", "")),
                    "zabbix_host": str(item.get("zabbix_host", "")),
                    "zabbix_status": "NÃO SOLICITADO",
                    "zabbix_detail": "",
                    "status": "NÃO PROCESSADO",
                    "protocol": "",
                    "case_number": "",
                    "message": message,
                }
            )
        self.batch_queue = []

    def _finish_batch(self) -> None:
        if not self.batch_active:
            return
        self.batch_results.sort(key=lambda item: int(item.get("position") or 0))
        total = len(self.batch_results)
        successes = sum(
            1 for item in self.batch_results if item.get("status") in {"ABERTO", "ENVIADO"}
        )
        pending = sum(
            1
            for item in self.batch_results
            if item.get("status") in {"PROTOCOLO PENDENTE", "ENVIO INCERTO"}
        )
        failures = total - successes - pending
        zabbix_recognized = sum(
            1 for item in self.batch_results if item.get("zabbix_status") == "RECONHECIDO"
        )
        zabbix_pending = sum(
            1 for item in self.batch_results if item.get("zabbix_status") == "PENDENTE"
        )
        self.batch_active = False
        self.batch_queue = []
        self.batch_current = None
        self.batch_items_by_request = {}
        self.batch_credentials = None
        self.batch_worker_path = None
        self.worker_finished = True
        self.worker = None
        self.worker_kind = ""
        self.progress_bar.stop()
        self.login_button.configure(state="normal")
        self.zabbix_button.configure(state="normal")
        self.search_button.configure(state="normal")
        self.batch_button.configure(state="normal")
        self.start_button.configure(state="disabled")
        if self.links:
            self.link_combo.configure(state="readonly")
        self.status_var.set(
            f"Lote: {successes} aberto(s), {pending} pendente(s), {failures} falha(s). "
            f"Zabbix: {zabbix_recognized} reconhecido(s), {zabbix_pending} pendente(s)."
        )
        self._append_log(self.status_var.get())
        self._show_batch_results()
        self.batch_zabbix_started = False

    def _show_batch_results(self) -> None:
        results = [dict(item) for item in self.batch_results]
        window = self.tk.Toplevel(self.root)
        window.title("Resultado da abertura em lote")
        window.geometry("1420x560")
        window.minsize(1100, 420)
        window.transient(self.root)

        frame = self.ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        self.ttk.Label(
            frame,
            text="Resultado individual de cada VD",
            style="Title.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.ttk.Label(
            frame,
            text=(
                "Cada linha possui seu próprio botão para copiar somente aquele protocolo. "
                "O resultado do reconhecimento no Zabbix aparece separado do chamado Vivo. "
                "Marque linhas para copiar casos, detalhes ou frases específicas; sem marcação, todas serão usadas."
            ),
            wraplength=900,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))

        def copy_text(text: str, success_message: str) -> None:
            if not text.strip():
                self.status_var.set("Não há informação disponível para copiar.")
                return
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status_var.set(success_message)

        table = self.ttk.Frame(frame)
        table.grid(row=2, column=0, columnspan=2, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        canvas = self.tk.Canvas(table, highlightthickness=0, borderwidth=0)
        y_scroll = self.ttk.Scrollbar(table, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=y_scroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        rows_frame = self.ttk.Frame(canvas, padding=(2, 2, 8, 2))
        canvas_window = canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        rows_frame.columnconfigure(7, weight=1)
        selected_rows: list[tuple[dict[str, str], Any]] = []

        def refresh_scroll_region(_event: Any = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def fit_table(event: Any) -> None:
            canvas.itemconfigure(canvas_window, width=event.width)

        rows_frame.bind("<Configure>", refresh_scroll_region)
        canvas.bind("<Configure>", fit_table)

        headers = (
            "", "VD", "Host Zabbix", "Status Vivo", "Protocolo Vivo",
            "Número do caso", "Zabbix", "Detalhe", "Ação",
        )
        for column, heading in enumerate(headers):
            self.ttk.Label(
                rows_frame,
                text=heading,
                style="Section.TLabel" if heading else "TLabel",
                anchor="w",
            ).grid(row=0, column=column, sticky="ew", padx=5, pady=(3, 7))

        for row_number, item in enumerate(results, start=1):
            selected = self.tk.BooleanVar(value=False)
            selected_rows.append((item, selected))
            self.ttk.Checkbutton(rows_frame, variable=selected).grid(
                row=row_number, column=0, padx=5, pady=5
            )
            self.ttk.Label(rows_frame, text=item.get("vd", ""), width=10).grid(
                row=row_number, column=1, sticky="w", padx=5, pady=5
            )
            self.ttk.Label(rows_frame, text=item.get("zabbix_host", ""), width=24).grid(
                row=row_number, column=2, sticky="w", padx=5, pady=5
            )
            self.ttk.Label(rows_frame, text=item.get("status", ""), width=18).grid(
                row=row_number, column=3, sticky="w", padx=5, pady=5
            )
            self.ttk.Label(rows_frame, text=item.get("protocol", ""), width=15).grid(
                row=row_number, column=4, sticky="w", padx=5, pady=5
            )
            self.ttk.Label(rows_frame, text=item.get("case_number", ""), width=16).grid(
                row=row_number, column=5, sticky="w", padx=5, pady=5
            )
            self.ttk.Label(rows_frame, text=item.get("zabbix_status", ""), width=16).grid(
                row=row_number, column=6, sticky="w", padx=5, pady=5
            )
            details = " — ".join(
                value for value in (
                    str(item.get("message") or "").strip(),
                    str(item.get("zabbix_detail") or "").strip(),
                ) if value
            )
            self.ttk.Label(
                rows_frame,
                text=details,
                wraplength=360,
                justify="left",
            ).grid(row=row_number, column=7, sticky="ew", padx=5, pady=5)
            protocol = str(item.get("protocol") or "").strip()
            vd = str(item.get("vd") or "VD")
            status = str(item.get("status") or "").strip()
            if protocol:
                action_text = "Copiar protocolo"
                action_value = protocol
                action_message = f"Protocolo de {vd} copiado."
                action_state = "normal"
            elif status == "PROTOCOLO PENDENTE":
                action_text = "Copiar frase"
                action_value = protocol_support_phrase(
                    vd,
                    item.get("lp"),
                    item.get("case_number"),
                )
                action_message = f"Frase de apoio para {vd} copiada."
                action_state = "normal"
            else:
                action_text = "Copiar protocolo"
                action_value = ""
                action_message = "Não há informação disponível para copiar."
                action_state = "disabled"
            self.ttk.Button(
                rows_frame,
                text=action_text,
                state=action_state,
                command=lambda value=action_value, success=action_message: copy_text(
                    value, success
                ),
            ).grid(row=row_number, column=8, sticky="e", padx=5, pady=5)

        def toggle_all() -> None:
            desired = bool(select_all.get())
            for _item, variable in selected_rows:
                variable.set(desired)

        select_all = self.tk.BooleanVar(value=False)
        self.ttk.Checkbutton(
            rows_frame,
            text="Todos",
            variable=select_all,
            command=toggle_all,
        ).grid(row=0, column=0, sticky="w", padx=2, pady=(3, 7))

        def on_mousewheel(event: Any) -> None:
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", on_mousewheel))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))

        def close_window() -> None:
            canvas.unbind_all("<MouseWheel>")
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", close_window)

        def chosen_results() -> list[dict[str, str]]:
            chosen = [item for item, variable in selected_rows if variable.get()]
            return chosen or results

        def copy_column(key: str, label: str) -> None:
            values = [
                str(item.get(key, "")).replace("\t", " ").replace("\n", " ").strip()
                for item in chosen_results()
            ]
            copy_text(
                "\n".join(value for value in values if value),
                f"{label} copiado(s).",
            )

        def copy_complete_sentences() -> None:
            lines: list[str] = []
            for item in chosen_results():
                parts = [
                    str(item.get("vd") or "VD não informado"),
                    f"Status: {item.get('status') or 'não informado'}",
                ]
                if item.get("protocol"):
                    parts.append(f"Protocolo Vivo: {item['protocol']}")
                if item.get("case_number"):
                    parts.append(f"Número do caso: {item['case_number']}")
                if item.get("zabbix_host"):
                    parts.append(f"Host: {item['zabbix_host']}")
                if item.get("zabbix_status"):
                    parts.append(f"Zabbix: {item['zabbix_status']}")
                if item.get("message"):
                    detail = str(item["message"]).replace("\t", " ").replace("\n", " ")
                    parts.append(f"Detalhe: {detail}")
                if item.get("zabbix_detail"):
                    detail = str(item["zabbix_detail"]).replace("\t", " ").replace("\n", " ")
                    parts.append(f"Detalhe Zabbix: {detail}")
                lines.append(" — ".join(parts))
            copy_text("\n".join(lines), "Frase(s) completa(s) copiada(s).")

        def copy_details() -> None:
            lines = []
            for item in chosen_results():
                detail = " — ".join(
                    value for value in (
                        str(item.get("message") or "").replace("\t", " ").replace("\n", " ").strip(),
                        str(item.get("zabbix_detail") or "").replace("\t", " ").replace("\n", " ").strip(),
                    ) if value
                )
                if detail:
                    lines.append(detail)
            copy_text("\n".join(lines), "Detalhe(s) copiado(s).")

        buttons = self.ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.ttk.Button(
            buttons,
            text="Copiar casos",
            command=lambda: copy_column("case_number", "Número(s) do caso"),
        ).pack(side="left")
        self.ttk.Button(
            buttons,
            text="Copiar detalhes",
            command=copy_details,
        ).pack(side="left", padx=(8, 0))
        self.ttk.Button(
            buttons,
            text="Copiar frase completa",
            command=copy_complete_sentences,
        ).pack(side="left", padx=(8, 0))
        self.ttk.Button(buttons, text="Fechar", command=close_window).pack(
            side="left", padx=(8, 0)
        )

    def start_ticket(self) -> None:
        from tkinter import messagebox

        if (
            self.batch_active
            or self.ticket_started
            or not self.worker_finished
            or self.zabbix_single_pending
        ):
            messagebox.showinfo("Operação em andamento", "Este chamado já está sendo processado.")
            return
        if not self.store or not self._selected_link():
            messagebox.showwarning("Dados incompletos", "Localize e selecione um circuito primeiro.")
            return
        try:
            credentials = self._load_credentials()
        except Exception as exc:
            messagebox.showerror("Acesso automático", friendly_error(exc))
            return
        if not credentials:
            messagebox.showwarning(
                "Configure o acesso",
                "Cadastre o CPF e a senha uma única vez para o robô entrar automaticamente.",
            )
            self.configure_credentials(first_use=True)
            return
        vd = self.vd_var.get()
        link = self._selected_link() or {}
        designation = str(field(link, "designacao", "identificacao", default=""))
        try:
            assert self.adapter is not None
            ticket = self.adapter.build_ticket(vd, self.issue_var.get(), designation)
            payload = self._build_payload(ticket, self.store, link)
            node, worker_path = self._node_and_worker()
        except Exception as exc:
            logging.exception("Falha ao preparar chamado")
            messagebox.showerror("Preparação do chamado", friendly_error(exc))
            return

        confirmation = self._base_confirmation(payload)
        if not messagebox.askyesno(
            "Confirmar dados da base",
            confirmation + "\n\nOs dados acima estão corretos?",
            icon="question",
        ):
            self._append_log("Abertura cancelada na confirmação dos dados da base.")
            return
        if self._recent_duplicate(payload) and not messagebox.askyesno(
            "Possível chamado duplicado",
            "Há um chamado recente registrado para este VD e circuito.\n\n"
            "Deseja iniciar uma nova abertura mesmo assim?",
            icon="warning",
        ):
            self._append_log("Abertura cancelada para evitar possível duplicidade.")
            return

        self.ticket = ticket
        self.ticket_started = True
        self.submission_seen = False
        self.terminal_event_seen = False
        self.worker_finished = False
        self.worker_kind = "ticket"
        self.start_button.configure(state="disabled")
        self.search_button.configure(state="disabled")
        self.login_button.configure(state="disabled")
        self.zabbix_button.configure(state="disabled")
        self.link_combo.configure(state="disabled")
        self._set_busy(True, "Iniciando acesso ao portal Vivo...")
        self._append_log("Automação iniciada. Nenhum reenvio automático será realizado.")
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self._spawn_worker(
            [node, str(worker_path), "--open-ticket", serialized],
            environment={
                "ROBO_VIVO_CPF": credentials.cpf,
                "ROBO_VIVO_PASSWORD": credentials.senha,
            },
        )

    def _build_payload(
        self,
        ticket: dict[str, Any],
        store: dict[str, Any],
        link: dict[str, Any],
        vd_value: Optional[str] = None,
        issue_value: Optional[str] = None,
    ) -> dict[str, Any]:
        return build_ticket_payload(
            self.config,
            ticket,
            store,
            link,
            vd_value or self.vd_var.get(),
            issue_value or self.issue_var.get(),
        )

    def _base_confirmation(self, payload: Mapping[str, Any]) -> str:
        store = payload.get("loja", {})
        link = payload.get("circuito", {})
        issue = (
            "Link inoperante"
            if payload.get("problema") == "link_interrompido"
            else "Perda de pacotes"
        )
        host_line = self.current_zabbix_host or "Será localizado pelo VD, exigindo um único incidente ativo"
        return (
            f"Loja: {payload.get('vd')} — {field(store, 'nome', 'nome_loja')}\n"
            f"Host Zabbix: {host_line}\n"
            f"Endereço: {field(store, 'endereco')} — {field(store, 'cidade')}/{field(store, 'uf')}\n"
            f"Designação: {field(link, 'designacao', 'identificacao')}\n"
            f"Horário no portal: {payload.get('hora_inicio')} às {payload.get('hora_fim')}\n"
            f"Problema: {issue}"
        )

    def _spawn_worker(
        self, command: list[str], environment: Optional[Mapping[str, str]] = None
    ) -> None:
        def launch() -> None:
            try:
                self.worker = WorkerClient(
                    command,
                    lambda event: self._post("worker", event),
                    lambda code: self._post("worker_exit", code),
                    environment=environment,
                )
                self.worker.start()
            except Exception as exc:
                logging.exception("Falha ao iniciar automação")
                self._post("spawn_error", friendly_error(exc))

        threading.Thread(target=launch, daemon=True).start()

    def _event_name(self, event: Mapping[str, Any]) -> str:
        return normalized_key(event.get("type") or event.get("event") or event.get("status") or "")

    def _handle_worker_event(self, event: dict[str, Any]) -> None:
        from tkinter import messagebox

        event_name = self._event_name(event)
        message = str(event.get("message") or event.get("mensagem") or "").strip()
        parallel_batch = self.batch_active and self.worker_kind == "batch"
        batch_item = self._batch_item_for_event(event) if parallel_batch else None
        if message:
            prefix = f"[{batch_item.get('vd')}] " if batch_item else ""
            self.status_var.set(prefix + message)
            self._append_log(prefix + message)

        if event_name in {
            "status", "progress", "progresso", "ready", "batch_started", "batch_complete"
        }:
            return
        if event_name in {"auth_required", "login_required", "autenticacao_necessaria"}:
            if parallel_batch:
                detail = message or "A Vivo solicitou uma validação adicional."
                self._batch_record_item(batch_item, "VALIDAÇÃO NECESSÁRIA", message=detail)
                return
            self.terminal_event_seen = True
            if not self.submission_seen:
                self.ticket_started = False
            if self.batch_active:
                detail = message or "A Vivo solicitou uma validação adicional."
                self._batch_record("VALIDAÇÃO NECESSÁRIA", message=detail)
                self._batch_stop_remaining(
                    "Lote interrompido porque a Vivo solicitou uma validação de segurança."
                )
                self._append_log(
                    "Lote interrompido por validação de segurança; os VDs restantes não foram processados."
                )
                return
            self._set_busy(False, "A Vivo solicitou uma validação adicional.")
            messagebox.showwarning(
                "Validação da Vivo necessária",
                (message or "A Vivo solicitou uma validação de segurança que não pode ser automatizada.")
                + "\n\nNenhum chamado foi enviado.",
            )
            return
        if event_name in {"submitted", "ticket_submitted", "chamado_enviado", "protocol_pending"}:
            if parallel_batch:
                if batch_item:
                    batch_item["submitted"] = True
                return
            self.submission_seen = True
            if not message:
                self._append_log("Chamado enviado. Aguardando o protocolo, sem reenviar.")
            return
        if event_name in {"portal_candidate", "candidate", "candidato_portal"}:
            if parallel_batch and batch_item:
                self._display_batch_item(batch_item)
            candidate = event.get("candidate") or event.get("candidato") or event.get("data") or {}
            expected = event.get("expected") or event.get("esperado") or {}
            text = self._candidate_text(candidate, expected)
            batch_notice = ""
            title = "Confirmar circuito no portal"
            if self.batch_active:
                target_vd = str(batch_item.get("vd")) if batch_item else self.vd_var.get()
                target_position = int(batch_item.get("position") or 0) if batch_item else self.batch_position
                title = f"Confirmar circuito — {target_vd}"
                batch_notice = (
                    f"CONFIRMAÇÃO OBRIGATÓRIA DO LOTE "
                    f"({target_position}/{self.batch_total})\n\n"
                )
            confirmed = messagebox.askyesno(
                title,
                batch_notice + text + "\n\nÉ este o circuito correto?",
                icon="question",
            )
            self._send_worker_response("portal_candidate", event, confirmed)
            self._append_log(
                "Circuito do portal confirmado." if confirmed else "Candidato recusado; procurando outro."
            )
            return
        if event_name in {"final_preview", "preview", "confirm_submit", "revisao_final"}:
            if parallel_batch and batch_item:
                self._display_batch_item(batch_item)
            preview = event.get("preview") or event.get("data") or event
            if self.batch_active:
                confirmed = True
                self._append_log(
                    "Envio autorizado pela confirmação inicial do lote."
                )
            else:
                confirmed = messagebox.askyesno(
                    "Revisão final antes do envio",
                    self._preview_text(preview)
                    + "\n\nAutoriza clicar em Enviar? Esta confirmação não será repetida automaticamente.",
                    icon="warning",
                )
            self._send_worker_response("final_preview", event, confirmed)
            if confirmed:
                self._append_log("Envio autorizado pelo usuário.")
            else:
                self._append_log("Envio cancelado pelo usuário.")
            return
        if event_name in {"success", "result", "completed", "concluido", "login_ready", "auth_ok"}:
            status = normalized_key(event.get("status", ""))
            if event_name == "result" and status in {"error", "erro", "failed"}:
                if parallel_batch:
                    self._handle_batch_error(
                        event,
                        batch_item,
                        message or "A automação informou uma falha.",
                    )
                else:
                    self._worker_error(message or "A automação informou uma falha.")
                return
            if self.worker_kind == "login":
                self.terminal_event_seen = True
                self._set_busy(False, message or "Login Vivo concluído.")
                messagebox.showinfo(
                    "Login Vivo",
                    message or "A autenticação foi concluída e a sessão ficou salva.",
                )
                return
            if parallel_batch:
                self._complete_batch_item(event, batch_item)
                return
            self._complete_ticket(event)
            return
        if event_name in {"cancelled", "canceled", "cancelado"}:
            if parallel_batch:
                self._batch_record_item(
                    batch_item, "CANCELADO", message=message or "Operação cancelada."
                )
                return
            self.terminal_event_seen = True
            if self.batch_active:
                self._batch_record("CANCELADO", message=message or "Operação cancelada.")
                self._append_log(message or "Operação cancelada.")
                return
            self._set_busy(False, message or "Operação cancelada.")
            return
        if event_name in {"pending", "pendente"}:
            if parallel_batch:
                self._handle_batch_pending(event, batch_item)
                return
            self._handle_pending(event)
            return
        if event_name in {"error", "erro", "failed", "failure"}:
            if parallel_batch:
                self._handle_batch_error(
                    event,
                    batch_item,
                    message or str(event.get("details") or "Falha não detalhada."),
                )
                return
            self._worker_error(message or str(event.get("details") or "Falha não detalhada."))

    def _complete_batch_item(
        self, event: Mapping[str, Any], item: Optional[dict[str, Any]]
    ) -> None:
        nested = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        protocol = str(
            event.get("protocol")
            or event.get("protocolo")
            or field(nested, "protocol", "protocolo", "protocolo_vivo")
            or ""
        ).strip()
        case_number = str(
            event.get("case_number")
            or event.get("numero_caso")
            or event.get("case")
            or field(nested, "case_number", "numero_caso", "case")
            or ""
        ).strip()
        lp = str(
            event.get("lp")
            or event.get("identification")
            or event.get("identificacao")
            or field(nested, "lp", "identification", "identificacao")
            or ""
        ).strip()
        if item:
            item["submitted"] = True
            self._display_batch_item(item)
        self.protocol_var.set(protocol)
        self.case_var.set(case_number)
        if protocol:
            self.copy_button.configure(state="normal")
        status = "ABERTO" if protocol else "PROTOCOLO PENDENTE"
        self._write_history(status, protocol, case_number, item=item)
        self._batch_record_item(
            item,
            status,
            protocol=protocol,
            case_number=case_number,
            lp=lp,
            message="Chamado aberto com sucesso." if protocol else "Chamado enviado; protocolo pendente.",
        )

    def _handle_batch_pending(
        self, event: Mapping[str, Any], item: Optional[dict[str, Any]]
    ) -> None:
        reason = normalized_key(event.get("reason", ""))
        message = str(event.get("message") or "Operação encerrada sem protocolo.")
        if reason in {"candidate_rejected", "cancelled_before_submit", "submission_disabled"}:
            self._batch_record_item(item, "NÃO ENVIADO", message=message)
            return
        protocol = str(event.get("protocol") or event.get("protocolo_vivo") or "").strip()
        case_number = str(
            event.get("case_number") or event.get("numero_caso") or event.get("case") or ""
        ).strip()
        lp = str(
            event.get("lp")
            or event.get("identification")
            or event.get("identificacao")
            or ""
        ).strip()
        if item:
            item["submitted"] = True
            self._display_batch_item(item)
        self.protocol_var.set(protocol)
        self.case_var.set(case_number)
        if protocol:
            self.copy_button.configure(state="normal")
        status = "ENVIO INCERTO" if reason == "submission_state_uncertain" else "PROTOCOLO PENDENTE"
        self._write_history(status, protocol, case_number, item=item)
        details = str(event.get("details") or "").strip()
        self._batch_record_item(
            item,
            status,
            protocol=protocol,
            case_number=case_number,
            lp=lp,
            message=details or message,
        )

    def _handle_batch_error(
        self,
        event: Mapping[str, Any],
        item: Optional[dict[str, Any]],
        message: str,
    ) -> None:
        submitted = bool(item and item.get("submitted"))
        status = "ENVIO INCERTO" if submitted else "ERRO"
        if submitted:
            self._write_history(status, "", "", item=item)
        self._batch_record_item(item, status, message=message)

    def _candidate_text(self, candidate: Any, expected: Any = None) -> str:
        portal = (
            "DADOS ENCONTRADOS NO PORTAL\n"
            f"Empresa: {field(candidate, 'empresa', 'company', default='Não informada')}\n"
            f"Produto: {field(candidate, 'produto', 'produto_servico', 'product', default='Não informado')}\n"
            f"Endereço no portal: {field(candidate, 'endereco', 'address', default='Não informado pelo portal')}\n"
            f"Cidade: {field(candidate, 'cidade', 'city', default='Não informada')}\n"
            f"Identificação: {field(candidate, 'identificacao', 'identification', default='Não informada')}\n"
            f"ID do serviço: {field(candidate, 'id_servico', 'service_id', 'id', default='Não informado')}\n"
            f"Velocidade: {field(candidate, 'velocidade', 'speed', default='Não informada')}\n"
            f"Nº do contrato: {field(candidate, 'contrato', 'contract', default='Não informado')}"
        )
        if not expected:
            return portal
        return (
            portal
            + "\n\nDADOS ESPERADOS PELA BASE\n"
            + f"Loja: {field(expected, 'vd', default=self.vd_var.get())} — "
            + f"{field(expected, 'store_name', 'nome_loja', default='Não informada')}\n"
            + f"Endereço na base: {field(expected, 'address', 'endereco', default='Não informado')}\n"
            + f"Cidade: {field(expected, 'city', 'cidade', default='Não informada')}\n"
            + f"Identificação: {field(expected, 'identification', 'identificacao', default='Não informada')}\n"
            + f"ID do serviço: {field(expected, 'service_id', 'id_servico', default='Não disponível na base')}"
        )

    def _preview_text(self, preview: Any) -> str:
        description = field(preview, "descricao", "description")
        if len(str(description)) > 850:
            description = str(description)[:850] + "…"
        return (
            f"Loja: {field(preview, 'store_name', 'nome_loja', 'vd', default=self.vd_var.get())}\n"
            f"Endereço: {field(preview, 'endereco', 'address', default=field(self.store, 'endereco'))}\n"
            f"Identificação: {field(preview, 'identificacao', 'identification', default='')}\n"
            f"Horário: {field(preview, 'hora_inicio', 'start_time', default='')} às "
            f"{field(preview, 'hora_fim', 'hora_termino', 'end_time', default='')}\n\n"
            f"Descrição:\n{description}"
        )

    def _handle_pending(self, event: Mapping[str, Any]) -> None:
        """Trata término sem protocolo sem jamais iniciar uma segunda submissão."""

        from tkinter import messagebox

        reason = normalized_key(event.get("reason", ""))
        message = str(event.get("message") or "Operação encerrada sem protocolo.")
        self.terminal_event_seen = True
        if reason in {"candidate_rejected", "cancelled_before_submit", "submission_disabled"}:
            if self.batch_active:
                self._batch_record("NÃO ENVIADO", message=message)
                self._append_log(message)
                return
            self._set_busy(False, "Operação encerrada antes do envio.")
            self._append_log(message)
            messagebox.showinfo("Chamado não enviado", message)
            return

        # protocol_timeout e submission_state_uncertain significam que repetir o
        # clique poderia criar chamado duplicado.
        self.submission_seen = True
        protocol = str(event.get("protocol") or event.get("protocolo_vivo") or "").strip()
        case_number = str(
            event.get("case_number") or event.get("numero_caso") or event.get("case") or ""
        ).strip()
        self.protocol_var.set(protocol)
        self.case_var.set(case_number)
        if protocol:
            self.copy_button.configure(state="normal")
        history_status = "ENVIO INCERTO" if reason == "submission_state_uncertain" else "PROTOCOLO PENDENTE"
        self._write_history(history_status, protocol, case_number)
        if self.batch_active:
            details = str(event.get("details") or "").strip()
            self._batch_record(
                history_status,
                protocol=protocol,
                case_number=case_number,
                message=details or message,
            )
            self._append_log(message)
            return
        self._set_busy(False, "Chamado enviado; confirmação pendente.")
        self._append_log(message)
        details = str(event.get("details") or "").strip()
        if reason == "protocol_timeout":
            nested = event.get("result") if isinstance(event.get("result"), Mapping) else {}
            lp = str(
                event.get("lp")
                or event.get("identification")
                or event.get("identificacao")
                or field(nested, "lp", "identification", "identificacao")
                or ""
            ).strip()
            if not lp:
                ticket_link = field(self.ticket or {}, "link", "circuito", default={})
                lp = str(
                    field(
                        ticket_link or self._selected_link() or {},
                        "designacao",
                        "identificacao",
                        default="",
                    )
                ).strip()
            vd = str(event.get("vd") or self.vd_var.get()).strip()
            self.zabbix_var.set("Não solicitado: Protocolo Vivo pendente após 5 minutos.")
            self._show_protocol_support_dialog(vd, lp, case_number)
            return
        suffix = f"\n\nDetalhe: {details}" if details else ""
        messagebox.showwarning(
            "Não reenviar o chamado",
            message
            + suffix
            + (f"\n\nNúmero do caso: {case_number}" if case_number else "")
            + "\n\nO robô não fará uma nova tentativa. Consulte o portal antes de abrir outro chamado.",
        )

    def _send_worker_response(
        self, action: str, event: Mapping[str, Any], confirmed: bool
    ) -> None:
        try:
            if not self.worker:
                raise RuntimeError("Processo de automação indisponível.")
            self.worker.send(
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
            if self.batch_active and self.worker_kind == "batch":
                self._handle_batch_error(event, self._batch_item_for_event(event), message)
            else:
                self._worker_error(message)

    def _complete_ticket(self, event: Mapping[str, Any]) -> None:
        from tkinter import messagebox

        nested = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        protocol = str(
            event.get("protocol")
            or event.get("protocolo")
            or field(nested, "protocol", "protocolo", "protocolo_vivo")
            or ""
        ).strip()
        case_number = str(
            event.get("case_number")
            or event.get("numero_caso")
            or event.get("case")
            or field(nested, "case_number", "numero_caso", "case")
            or ""
        ).strip()
        lp = str(
            event.get("lp")
            or event.get("identification")
            or event.get("identificacao")
            or field(nested, "lp", "identification", "identificacao")
            or ""
        ).strip()
        self.protocol_var.set(protocol)
        self.case_var.set(case_number)
        self.terminal_event_seen = True
        self.submission_seen = True
        self._set_busy(False, "Chamado aberto com sucesso." if protocol else "Chamado enviado.")
        if protocol:
            self.copy_button.configure(state="normal")
            self._append_log(f"Protocolo Vivo recebido: {protocol}")
        else:
            self._append_log(
                "O chamado foi enviado, mas o protocolo ainda não apareceu. Não haverá reenvio."
            )
        self._write_history("ABERTO" if protocol else "PROTOCOLO PENDENTE", protocol, case_number)
        if self.batch_active:
            self._batch_record(
                "ABERTO" if protocol else "PROTOCOLO PENDENTE",
                protocol=protocol,
                case_number=case_number,
                message="Chamado aberto com sucesso." if protocol else "Chamado enviado; protocolo pendente.",
            )
            return
        if protocol:
            self._start_single_zabbix(protocol, case_number)
            return
        else:
            self.zabbix_var.set("Não solicitado: Protocolo Vivo ainda pendente.")
            if not lp:
                ticket_link = field(self.ticket or {}, "link", "circuito", default={})
                lp = str(
                    field(
                        ticket_link or self._selected_link() or {},
                        "designacao",
                        "identificacao",
                        default="",
                    )
                ).strip()
            self._show_protocol_support_dialog(self.vd_var.get(), lp, case_number)

    def _worker_error(self, message: str) -> None:
        from tkinter import messagebox

        self.terminal_event_seen = True
        if self.batch_active:
            self._append_log(f"ERRO em {self.vd_var.get()}: {message}")
            logging.error("Erro do worker no lote: %s", message)
            if not self.submission_seen:
                self.ticket_started = False
            status = "ENVIO INCERTO" if self.submission_seen else "ERRO"
            if self.submission_seen:
                self._write_history(status, "", "")
            self._batch_record(status, message=message)
            return
        self._set_busy(False, "A automação encontrou um erro.")
        self._append_log(f"ERRO: {message}")
        logging.error("Erro do worker: %s", message)
        if not self.submission_seen:
            self.ticket_started = False
        note = "\n\nNenhuma nova tentativa foi feita automaticamente."
        if self.submission_seen:
            note += " O envio pode ter ocorrido; consulte o portal antes de tentar novamente."
        messagebox.showerror("Abertura do chamado", message + note)

    def _handle_worker_exit(self, code: int) -> None:
        previous_kind = self.worker_kind
        if self.batch_active and previous_kind == "batch":
            self.worker_finished = True
            unfinished = [
                item for item in self.batch_items_by_request.values() if not item.get("recorded")
            ]
            if unfinished:
                detail = (
                    f"A automação foi encerrada inesperadamente (código {code})."
                    if code != 0
                    else "A automação terminou sem informar o resultado deste VD."
                )
                self._batch_fail_unfinished(detail)
            self.worker = None
            self.worker_kind = ""
            self._start_batch_zabbix()
            return
        if self.batch_active and previous_kind == "ticket":
            self.worker_finished = True
            if code != 0 and not self.terminal_event_seen:
                self._worker_error(f"A automação foi encerrada inesperadamente (código {code}).")
            elif not self.terminal_event_seen:
                if self.submission_seen:
                    self._batch_record(
                        "ENVIO INCERTO",
                        message="A automação terminou depois do envio, mas sem resultado final.",
                    )
                else:
                    self._worker_error("A automação terminou sem informar o resultado do chamado.")
            self.worker = None
            self.worker_kind = ""
            # Dá tempo para o Chrome liberar completamente o perfil persistente
            # antes de o próximo processo do lote tentar reutilizá-lo.
            self.root.after(1500, self._start_next_batch)
            return
        self.worker_finished = True
        self.progress_bar.stop()
        self.login_button.configure(state="normal")
        self.zabbix_button.configure(state="normal")
        self.search_button.configure(state="normal")
        if previous_kind == "ticket":
            if self.submission_seen:
                # Depois do envio, uma nova busca continua obrigatória para
                # impedir um segundo clique no mesmo chamado.
                self.start_button.configure(state="disabled")
            elif self.store and self.links:
                # Falhas anteriores ao envio são seguras para correção/tentativa
                # manual, inclusive o antigo erro que deixava o botão travado.
                self.ticket_started = False
                self.start_button.configure(state="normal")
                self.link_combo.configure(state="readonly")
        elif self.store and self.links:
            self.start_button.configure(state="normal")
            self.link_combo.configure(state="readonly")
        if code != 0 and not self.terminal_event_seen:
            self._worker_error(f"A automação foi encerrada inesperadamente (código {code}).")
        elif previous_kind == "login" and not self.terminal_event_seen:
            self._set_busy(False, "A janela de login foi encerrada.")
            self._append_log("A janela de login foi encerrada.")
        self.worker = None
        self.worker_kind = ""

    def _history_path(self) -> Path:
        return history_path(self.config)

    def _write_zabbix_audit(self, result: Mapping[str, Any]) -> None:
        write_zabbix_audit(self.config, result)

    def _write_history(
        self,
        status: str,
        protocol: str,
        case_number: str,
        item: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if item:
            link = item.get("link") or {}
            store = item.get("store") or {}
            vd_value = str(item.get("vd") or "")
            issue_value = self.batch_issue or self.issue_var.get()
        else:
            link = self._selected_link() or {}
            store = self.store or {}
            vd_value = self.vd_var.get()
            issue_value = self.issue_var.get()
        write_history(
            self.config, status, protocol, case_number, vd_value, store, link, issue_value
        )

    def _recent_duplicate(self, payload: Mapping[str, Any]) -> bool:
        return recent_duplicate(self.config, payload, fallback_vd=self.vd_var.get())

    def _show_protocol_support_dialog(
        self, vd: str, lp: str, case_number: str
    ) -> None:
        """Exibe e copia a mensagem de apoio quando o protocolo fica pendente."""

        phrase = protocol_support_phrase(vd, lp, case_number)
        window = self.tk.Toplevel(self.root)
        window.title("Protocolo Vivo pendente")
        window.geometry("690x360")
        window.minsize(600, 320)
        window.transient(self.root)

        frame = self.ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        self.ttk.Label(
            frame,
            text=(
                "O chamado foi enviado, mas o Protocolo Vivo não apareceu após 5 minutos. "
                "Não envie o chamado novamente. Copie a frase abaixo para solicitar apoio ao time."
            ),
            wraplength=640,
            justify="left",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 12))

        message_text = self.tk.Text(
            frame,
            height=9,
            wrap="word",
            background="#FBFAFC",
            foreground="#2F2933",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=9,
            font=("Segoe UI", 10),
        )
        message_text.grid(row=1, column=0, sticky="nsew")
        message_text.insert("1.0", phrase)
        message_text.configure(state="disabled")

        buttons = self.ttk.Frame(frame)
        buttons.grid(row=2, column=0, sticky="e", pady=(14, 0))

        def copy_phrase() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(phrase)
            self.status_var.set("Frase de apoio copiada.")

        copy_button = self.ttk.Button(
            buttons,
            text="Copiar frase",
            command=copy_phrase,
            style="Primary.TButton",
        )
        copy_button.pack(side="left")
        self.ttk.Button(buttons, text="Fechar", command=window.destroy).pack(
            side="left", padx=(8, 0)
        )
        copy_button.focus_set()

    def copy_protocol(self) -> None:
        value = self.protocol_var.get().strip()
        if value:
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self.status_var.set("Protocolo copiado.")

    def copy_case(self) -> None:
        value = self.case_var.get().strip()
        if value:
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self.status_var.set("Número do caso copiado.")

    def close(self) -> None:
        from tkinter import messagebox

        if (
            self.batch_active or not self.worker_finished or self.zabbix_single_pending
        ) and not messagebox.askyesno(
            "Fechar o robô",
            "Há uma operação em andamento. Fechar agora interromperá a automação.\n\n"
            "Deseja realmente fechar?",
            icon="warning",
        ):
            return
        if self.worker:
            self.worker.terminate()
        self.root.destroy()


def friendly_error(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return text
    return exc.__class__.__name__


def configure_logging(config: configparser.ConfigParser) -> Path:
    try:
        history = resolve_config_path(config, "arquivos", "historico")
        log_dir = history.parent
    except Exception:
        log_dir = PACKAGE_DIR / "historico"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "erros.log"
    logging.basicConfig(
        filename=path,
        level=logging.INFO,
        encoding="utf-8",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return path


def self_test() -> int:
    result: dict[str, Any] = {"ok": False, "package": str(PACKAGE_DIR), "checks": {}}
    try:
        config = load_config()
        result["checks"]["config"] = True
        result["checks"]["lojas"] = resolve_config_path(config, "bases", "lojas").is_file()
        result["checks"]["links"] = resolve_config_path(config, "bases", "links").is_file()
        result["checks"]["modelo_link"] = resolve_config_path(
            config, "arquivos", "modelo_link_interrompido"
        ).is_file()
        result["checks"]["modelo_perda"] = resolve_config_path(
            config, "arquivos", "modelo_perca_pacotes"
        ).is_file()
        if str(APP_DIR) not in sys.path:
            sys.path.insert(0, str(APP_DIR))
        import data_service  # type: ignore  # noqa: F401
        import credential_store  # type: ignore  # noqa: F401
        import zabbix_client  # type: ignore  # noqa: F401

        result["checks"]["data_service"] = True
        result["checks"]["credential_store"] = all(
            callable(getattr(credential_store, name, None))
            for name in (
                "salvar", "carregar", "remover",
                "salvar_zabbix", "carregar_zabbix", "remover_zabbix",
            )
        )
        result["checks"]["zabbix_client"] = all(
            callable(getattr(zabbix_client, name, None))
            for name in ("ZabbixClient", "normalize_api_url", "normalize_host")
        )
        result["checks"]["visual_assets"] = all(
            asset_path(name).is_file()
            for name in ("app_icon.ico", "app_icon.png", "vivo_wordmark.png")
        )
        adapter = DataAdapter(config)
        result["checks"]["data_adapter"] = all(
            callable(getattr(adapter.service, name, None))
            for name in ("localizar_loja", "localizar_links", "preparar_chamado")
        )
        worker = APP_DIR / "vivo_worker.mjs"
        if not worker.is_file():
            worker = PACKAGE_DIR / "automation" / "vivo_worker.mjs"
        result["checks"]["worker"] = worker.is_file()
        import web_server  # type: ignore

        result["checks"]["web_interface"] = callable(getattr(web_server, "serve", None)) and all(
            (web_server.WEB_DIR / name).is_file() for name in ("index.html", "app.css", "app.js")
        )
        result["checks"]["vd_normalization"] = (
            clean_vd("001517") == "VD1517" and clean_vd("3") == "VD003"
        )
        store, links = adapter.lookup("VD1517")
        result["checks"]["sample_store"] = (
            field(store, "nome") == "DP MESQUITA"
            and "PARANA" in str(field(store, "endereco_completo", "endereco"))
            and bool(links)
        )
        sample_ticket = adapter.build_ticket(
            "VD1517", "link_interrompido", str(field(links[0], "designacao"))
        )
        result["checks"]["sample_description"] = all(
            item in str(field(sample_ticket, "descricao"))
            for item in (
                "Favor verificar a filial: DP MESQUITA",
                "Filial: VD1517 - DP MESQUITA",
                "Horário Funcionamento: 07 ÀS 22",
            )
        )
        store_24h, _links_24h = adapter.lookup("VD003")
        result["checks"]["sample_24h"] = (
            bool(field(store_24h, "vinte_quatro_horas"))
            and field(store_24h, "hora_inicio") == "00:00"
            and field(store_24h, "hora_fim") == "23:59"
        )
        result["ok"] = all(result["checks"].values())
    except Exception as exc:
        result["error"] = f"{exc.__class__.__name__}: {exc}"
        result["traceback"] = traceback.format_exc(limit=3)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def show_startup_error(title: str, message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        hidden = tk.Tk()
        hidden.withdraw()
        messagebox.showerror(title, message, parent=hidden)
        hidden.destroy()
    except Exception:
        print(f"{title}: {message}", file=sys.stderr)


def run_web(simulation: bool = False, open_browser: bool = True) -> int:
    """Inicia a interface web local; reabre a aba se ela já estiver rodando."""

    # Importações explícitas para o PyInstaller incluir os módulos da
    # biblioteca padrão usados por app/web_server.py (carregado do disco).
    import http.server  # noqa: F401
    import mimetypes  # noqa: F401
    import secrets  # noqa: F401
    import socketserver  # noqa: F401
    import webbrowser

    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    import web_server  # type: ignore

    mutex: Optional[SingleInstanceMutex] = None
    try:
        try:
            mutex = SingleInstanceMutex()
        except AlreadyRunning:
            url = web_server.read_session_url()
            if url:
                if open_browser:
                    webbrowser.open(url)
                return 0
            show_startup_error(
                "Robô já aberto",
                "O Robô de Chamados Vivo já está aberto na janela tradicional. "
                "Feche-o antes de abrir a interface web.",
            )
            return 2
        config = load_config()
        configure_logging(config)
        return web_server.serve(
            sys.modules[__name__], config, simulation=simulation, open_browser=open_browser
        )
    except Exception as exc:
        try:
            logging.exception("Falha fatal na interface web")
        except Exception:
            pass
        show_startup_error("Robô de Chamados Vivo", friendly_error(exc))
        return 1
    finally:
        if mutex:
            mutex.close()


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--self-test", action="store_true", help="valida o pacote sem abrir a janela")
    parser.add_argument("--web", action="store_true", help="abre a interface web local no navegador")
    parser.add_argument(
        "--simulacao",
        action="store_true",
        help="interface web em modo simulação: nada é enviado à Vivo nem ao Zabbix",
    )
    parser.add_argument(
        "--sem-navegador", action="store_true", help="não abre o navegador automaticamente (web)"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.self_test:
        return self_test()
    if args.web or args.simulacao:
        return run_web(simulation=args.simulacao, open_browser=not args.sem_navegador)

    mutex: Optional[SingleInstanceMutex] = None
    try:
        mutex = SingleInstanceMutex()
        config = load_config()
        configure_logging(config)
        import tkinter as tk

        root = tk.Tk()
        app = VivoApp(root, config)
        # Mantém referências vivas durante todo o mainloop.
        root._vivo_app = app  # type: ignore[attr-defined]
        root.mainloop()
        return 0
    except AlreadyRunning as exc:
        show_startup_error("Robô já aberto", str(exc))
        return 2
    except Exception as exc:
        try:
            logging.exception("Falha fatal na interface")
        except Exception:
            pass
        show_startup_error("Robô de Chamados Vivo", friendly_error(exc))
        return 1
    finally:
        if mutex:
            mutex.close()


if __name__ == "__main__":
    raise SystemExit(main())
