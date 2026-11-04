"""Cliente mínimo e seguro para reconhecer incidentes no Zabbix.

O módulo usa apenas a API JSON-RPC oficial. Ele nunca navega pela interface,
não grava o token em logs e se recusa a atualizar quando host ou incidente não
são encontrados de forma única.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional


class ZabbixError(RuntimeError):
    """Falha de conexão, autenticação ou validação de segurança."""


@dataclass(frozen=True, slots=True)
class AcknowledgementResult:
    host: str
    hostid: str
    eventid: str
    problem: str
    protocol: str


def normalize_api_url(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("Informe a URL da API do Zabbix.")
    if "://" not in text:
        text = "http://" + text
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A URL da API precisa começar com http:// ou https://.")
    if parsed.username or parsed.password:
        raise ValueError("Não coloque usuário ou senha na URL da API.")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/api_jsonrpc.php"
    elif path.lower().endswith("/zabbix.php"):
        path = path[: -len("zabbix.php")] + "api_jsonrpc.php"
    elif not path.lower().endswith("/api_jsonrpc.php"):
        path += "/api_jsonrpc.php"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def normalize_host(value: str) -> str:
    host = (value or "").strip().upper()
    if not re.fullmatch(r"(?:DSP|DPA)_\d{1,7}_MPLS_VIVO", host):
        raise ValueError(
            "O host precisa estar no formato DSP_1234_MPLS_VIVO ou DPA_1234_MPLS_VIVO."
        )
    return host


def host_candidates(value: str) -> list[str]:
    """Retorna nomes técnicos exatos possíveis para um host ou VD.

    Um host completo nunca é ampliado. Para um VD, são considerados somente
    os padrões DPA/DSP e a variação com quatro dígitos usada pelo Zabbix.
    A escolha final exige exatamente um incidente ativo entre os candidatos.
    """

    text = (value or "").strip().upper()
    if re.fullmatch(r"(?:DSP|DPA)_\d{1,7}_MPLS_VIVO", text):
        return [normalize_host(text)]
    match = re.fullmatch(r"(?:VD)?\s*(\d{1,7})", text)
    if not match:
        raise ValueError(
            "Informe o VD ou o host no formato DSP_1234_MPLS_VIVO ou DPA_1234_MPLS_VIVO."
        )
    raw_digits = match.group(1)
    number = str(int(raw_digits))
    if number == "0":
        raise ValueError("O VD informado é inválido.")
    number_forms: list[str] = []
    for candidate in (raw_digits, number, number.zfill(4)):
        if candidate not in number_forms:
            number_forms.append(candidate)
    return [
        f"{prefix}_{digits}_MPLS_VIVO"
        for digits in number_forms
        for prefix in ("DPA", "DSP")
    ]


def normalize_protocol(value: str) -> str:
    protocol = re.sub(r"\D", "", value or "")
    if not protocol:
        raise ValueError("O Protocolo Vivo não foi informado.")
    return protocol


class ZabbixClient:
    def __init__(self, api_url: str, token: str, timeout: float = 15.0) -> None:
        self.api_url = normalize_api_url(api_url)
        self._token = (token or "").strip()
        if not self._token:
            raise ValueError("Informe o token da API do Zabbix.")
        self.timeout = max(3.0, min(float(timeout), 60.0))
        # A aplicação está dentro da rede corporativa. Ignorar proxies do
        # usuário evita que um IP privado seja enviado a um proxy externo.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._request_id = 0

    def _post(self, payload: Mapping[str, Any], bearer: bool = True) -> dict[str, Any]:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json-rpc"}
        if bearer:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(self.api_url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_000)
        except urllib.error.HTTPError as exc:
            raise ZabbixError(f"A API do Zabbix respondeu HTTP {exc.code}.") from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise ZabbixError(f"Não foi possível conectar à API do Zabbix: {reason}") from None
        except TimeoutError:
            raise ZabbixError("A API do Zabbix não respondeu dentro do prazo.") from None
        try:
            result = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ZabbixError("A API do Zabbix retornou uma resposta inválida.") from None
        if not isinstance(result, dict):
            raise ZabbixError("A API do Zabbix retornou um objeto inesperado.")
        return result

    def _call(self, method: str, params: Any, authenticated: bool = True) -> Any:
        self._request_id += 1
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._request_id,
        }
        try:
            result = self._post(payload, bearer=authenticated)
        except ZabbixError as exc:
            if authenticated and re.search(r"HTTP (401|403)\b", str(exc)):
                legacy_payload = dict(payload)
                legacy_payload["auth"] = self._token
                result = self._post(legacy_payload, bearer=False)
            else:
                raise
        error = result.get("error")
        if error and authenticated:
            # Compatibilidade com instalações antigas ou Apache que não
            # encaminha Authorization. O campo auth é legado, mas ainda é
            # aceito nas versões em que o cabeçalho pode falhar.
            message = str(error.get("message") or "") + " " + str(error.get("data") or "")
            if re.search(r"not authori[sz]ed|re-login|session terminated", message, re.I):
                legacy_payload = dict(payload)
                legacy_payload["auth"] = self._token
                result = self._post(legacy_payload, bearer=False)
                error = result.get("error")
        if error:
            message = str(error.get("message") or "Falha na API do Zabbix.")
            data = str(error.get("data") or "").strip()
            raise ZabbixError(message + (f" {data}" if data else ""))
        if "result" not in result:
            raise ZabbixError("A API do Zabbix não retornou o campo result.")
        return result["result"]

    def test_connection(self) -> str:
        version = str(self._call("apiinfo.version", {}, authenticated=False))
        # Também valida o token e a permissão mínima de leitura.
        self._call("host.get", {"output": ["hostid"], "limit": 1})
        return version

    def acknowledge_host_problem(self, host: str, protocol: str) -> AcknowledgementResult:
        candidates = host_candidates(host)
        target_protocol = normalize_protocol(protocol)
        hosts = self._call(
            "host.get",
            {
                "output": ["hostid", "host", "name"],
                "filter": {"host": candidates},
            },
        )
        if not isinstance(hosts, list) or not hosts:
            raise ZabbixError(
                f"Nenhum host DPA/DSP correspondente a {host} foi encontrado no Zabbix."
            )
        allowed = set(candidates)
        matching_problems: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for found_host in hosts:
            found_name = str(found_host.get("host") or "").upper()
            hostid = str(found_host.get("hostid") or "")
            if found_name not in allowed or not hostid:
                raise ZabbixError("O Zabbix retornou um host diferente do solicitado.")
            problems = self._call(
                "problem.get",
                {
                    "output": ["eventid", "name", "acknowledged", "clock", "severity"],
                    "hostids": [hostid],
                    "acknowledged": False,
                },
            )
            if not isinstance(problems, list):
                raise ZabbixError("O Zabbix retornou uma lista de incidentes inválida.")
            matching_problems.extend((found_host, problem) for problem in problems)
        if len(matching_problems) != 1:
            count = len(matching_problems)
            raise ZabbixError(
                f"Os hosts DPA/DSP de {host} possuem {count} incidente(s) ativo(s) e não reconhecido(s); "
                "nenhum foi alterado por segurança."
            )
        found_host, problem = matching_problems[0]
        target_host = str(found_host.get("host") or "").upper()
        hostid = str(found_host.get("hostid") or "")
        eventid = str(problem.get("eventid") or "")
        problem_name = str(problem.get("name") or "")
        if not eventid:
            raise ZabbixError("O incidente encontrado não possui eventid.")
        updated = self._call(
            "event.acknowledge",
            {
                "eventids": [eventid],
                "action": 6,  # 2 = reconhecer; 4 = adicionar mensagem
                "message": target_protocol,
            },
        )
        updated_ids = [str(value) for value in (updated or {}).get("eventids", [])]
        if eventid not in updated_ids:
            raise ZabbixError("A API não confirmou a atualização do incidente.")

        verified_ok = False
        verification_error: Optional[Exception] = None
        for history_parameter in ("selectAcknowledges", "select_acknowledges"):
            try:
                verified = self._call(
                    "event.get",
                    {
                        "output": ["eventid", "acknowledged", "name"],
                        "eventids": [eventid],
                        history_parameter: ["message", "action", "clock"],
                    },
                )
            except ZabbixError as exc:
                verification_error = exc
                continue
            if not isinstance(verified, list) or len(verified) != 1:
                continue
            event = verified[0]
            messages = [
                re.sub(r"\D", "", str(item.get("message") or ""))
                for item in event.get("acknowledges", [])
                if isinstance(item, Mapping)
            ]
            if (
                str(event.get("acknowledged")) in {"1", "True", "true"}
                and target_protocol in messages
            ):
                verified_ok = True
                break
        if not verified_ok:
            suffix = f" Detalhe: {verification_error}" if verification_error else ""
            raise ZabbixError(
                "A API respondeu, mas não foi possível confirmar o reconhecimento com o protocolo."
                + suffix
            )
        return AcknowledgementResult(
            host=target_host,
            hostid=hostid,
            eventid=eventid,
            problem=problem_name,
            protocol=target_protocol,
        )


__all__ = [
    "AcknowledgementResult",
    "ZabbixClient",
    "ZabbixError",
    "host_candidates",
    "normalize_api_url",
    "normalize_host",
    "normalize_protocol",
]
