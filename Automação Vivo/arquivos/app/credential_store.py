"""Armazenamento local seguro do acesso ao Portal Vivo.

A senha fica no Gerenciador de Credenciais do Windows, criptografada para o
usuário atual. Este módulo nunca grava a senha no BAT, no INI ou nos logs.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

if os.name == "nt":
    from ctypes import wintypes


TARGET_NAME = "DPSP_RoboVivo_PortalVivo"
ZABBIX_TARGET_NAME = "DPSP_RoboVivo_ZabbixAPI"
DEFAULT_ZABBIX_URL = "http://172.16.2.250/api_jsonrpc.php"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
APP_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True, slots=True)
class CredenciaisVivo:
    cpf: str
    senha: str


@dataclass(frozen=True, slots=True)
class CredenciaisZabbix:
    api_url: str
    token: str
    automatico: bool = True


if os.name == "nt":

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
        _fields_ = [
            ("Keyword", wintypes.LPWSTR),
            ("Flags", wintypes.DWORD),
            ("ValueSize", wintypes.DWORD),
            ("Value", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.POINTER(CREDENTIAL_ATTRIBUTEW)),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    PCREDENTIALW = ctypes.POINTER(CREDENTIALW)


def normalizar_cpf(valor: str) -> str:
    cpf = re.sub(r"\D", "", valor or "")
    if len(cpf) != 11:
        raise ValueError("Informe o CPF com 11 números.")
    return cpf


def env_paths() -> list[Path]:
    """Locais verificados para o arquivo ``.env`` (do mais próximo ao diretório raiz)."""

    explicit = os.environ.get("ROBO_VIVO_ENV_FILE", "").strip()
    if explicit:
        return [Path(explicit)]
    candidates: list[Path] = []
    for base in (APP_DIR.parent, APP_DIR.parent.parent, APP_DIR.parent.parent.parent):
        candidate = base / ".env"
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";")):
        return None
    if stripped.lower().startswith("export "):
        stripped = stripped[7:].lstrip()
    if "=" not in stripped:
        return None
    key, _, raw_value = stripped.partition("=")
    key = key.strip()
    if not key:
        return None
    value = raw_value.strip()
    if value.startswith(('"', "'")):
        quote = value[0]
        end_idx = -1
        escaped = False
        for idx in range(1, len(value)):
            ch = value[idx]
            if escaped:
                escaped = False
                continue
            if quote == '"' and ch == "\\":
                escaped = True
                continue
            if ch == quote:
                end_idx = idx
                break
        if end_idx > 0:
            inner = value[1:end_idx]
            if quote == '"':
                inner = inner.replace(r"\"", '"').replace(r"\\", "\\")
            return key, inner
    comment_match = re.search(r"\s+#", value)
    if comment_match:
        value = value[: comment_match.start()].rstrip()
    return key, value


def _load_env_values() -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in reversed(env_paths()):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            content = path.read_text(encoding="cp1252")
        for line in content.splitlines():
            parsed = _parse_dotenv_line(line)
            if parsed:
                merged[parsed[0].upper()] = parsed[1]
    for env_key in (
        "VIVO_CPF",
        "VIVO_SENHA",
        "VIVO_PASSWORD",
        "ZABBIX_API_URL",
        "ZABBIX_URL",
        "ZABBIX_API_TOKEN",
        "ZABBIX_TOKEN",
        "ZABBIX_AUTOMATICO",
        "ZABBIX_AUTO",
    ):
        if env_key in os.environ and os.environ[env_key].strip():
            merged[env_key] = os.environ[env_key].strip()
    return merged


def _first_env(values: dict[str, str], *keys: str) -> str:
    for key in keys:
        val = values.get(key.upper(), "").strip()
        if val:
            return val
    return ""


def _parse_env_bool(raw: str, default: bool = True) -> bool:
    if not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "sim", "s", "yes", "y", "true", "verdadeiro"}:
        return True
    if normalized in {"0", "nao", "não", "n", "no", "false", "falso"}:
        return False
    return default


def _carregar_vivo_env() -> CredenciaisVivo | None:
    values = _load_env_values()
    cpf_raw = _first_env(values, "VIVO_CPF")
    senha = _first_env(values, "VIVO_SENHA", "VIVO_PASSWORD")
    if not cpf_raw and not senha:
        return None
    if not cpf_raw or not senha:
        raise ValueError("Preencha tanto VIVO_CPF quanto VIVO_SENHA no arquivo .env.")
    return CredenciaisVivo(cpf=normalizar_cpf(cpf_raw), senha=senha)


def _carregar_zabbix_env() -> CredenciaisZabbix | None:
    values = _load_env_values()
    token = _first_env(values, "ZABBIX_API_TOKEN", "ZABBIX_TOKEN")
    if not token:
        return None
    api_url = _first_env(values, "ZABBIX_API_URL", "ZABBIX_URL") or DEFAULT_ZABBIX_URL
    auto_raw = _first_env(values, "ZABBIX_AUTOMATICO", "ZABBIX_AUTO")
    return CredenciaisZabbix(
        api_url=api_url,
        token=token,
        automatico=_parse_env_bool(auto_raw, default=True),
    )


def _api() -> ctypes.WinDLL:
    if os.name != "nt":
        raise OSError("O armazenamento seguro de credenciais requer Windows.")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredWriteW.argtypes = (ctypes.POINTER(CREDENTIALW), wintypes.DWORD)
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(PCREDENTIALW),
    )
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD)
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = (ctypes.c_void_p,)
    api.CredFree.restype = None
    return api


def salvar(cpf: str, senha: str) -> None:
    cpf_normalizado = normalizar_cpf(cpf)
    if not senha:
        raise ValueError("Informe a senha do Portal Vivo.")
    segredo = senha.encode("utf-16-le")
    if len(segredo) > 512:
        raise ValueError("A senha ultrapassa o limite aceito pelo Windows.")

    buffer = ctypes.create_string_buffer(segredo)
    credencial = CREDENTIALW()
    credencial.Type = CRED_TYPE_GENERIC
    credencial.TargetName = TARGET_NAME
    credencial.Comment = "Acesso automático do Robô de Chamados Vivo"
    credencial.CredentialBlobSize = len(segredo)
    credencial.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    credencial.Persist = CRED_PERSIST_LOCAL_MACHINE
    credencial.UserName = cpf_normalizado

    api = _api()
    if not api.CredWriteW(ctypes.byref(credencial), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def carregar() -> CredenciaisVivo | None:
    env_cred = _carregar_vivo_env()
    if env_cred is not None:
        return env_cred
    if os.name != "nt":
        return None
    api = _api()
    ponteiro = PCREDENTIALW()
    if not api.CredReadW(TARGET_NAME, CRED_TYPE_GENERIC, 0, ctypes.byref(ponteiro)):
        erro = ctypes.get_last_error()
        if erro == ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(erro)
    try:
        credencial = ponteiro.contents
        cpf = normalizar_cpf(credencial.UserName or "")
        segredo = ctypes.string_at(
            credencial.CredentialBlob, credencial.CredentialBlobSize
        ).decode("utf-16-le")
        return CredenciaisVivo(cpf=cpf, senha=segredo)
    finally:
        api.CredFree(ponteiro)


def remover() -> None:
    api = _api()
    if not api.CredDeleteW(TARGET_NAME, CRED_TYPE_GENERIC, 0):
        erro = ctypes.get_last_error()
        if erro != ERROR_NOT_FOUND:
            raise ctypes.WinError(erro)
    if _carregar_vivo_env() is not None:
        raise ValueError(
            "O acesso ao Portal Vivo está definido no arquivo .env; limpe VIVO_CPF e VIVO_SENHA no .env para removê-lo."
        )


def salvar_zabbix(api_url: str, token: str, automatico: bool = True) -> None:
    url = (api_url or "").strip()
    segredo_texto = (token or "").strip()
    if not url:
        raise ValueError("Informe a URL da API do Zabbix.")
    if not segredo_texto:
        raise ValueError("Informe o token da API do Zabbix.")
    segredo = segredo_texto.encode("utf-16-le")
    if len(segredo) > 512:
        raise ValueError("O token ultrapassa o limite aceito pelo Windows.")
    metadados = json.dumps(
        {"api_url": url, "automatico": bool(automatico)},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    buffer = ctypes.create_string_buffer(segredo)
    credencial = CREDENTIALW()
    credencial.Type = CRED_TYPE_GENERIC
    credencial.TargetName = ZABBIX_TARGET_NAME
    credencial.Comment = "Token da API usado pelo Robô de Chamados Vivo"
    credencial.CredentialBlobSize = len(segredo)
    credencial.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    credencial.Persist = CRED_PERSIST_LOCAL_MACHINE
    credencial.UserName = metadados
    api = _api()
    if not api.CredWriteW(ctypes.byref(credencial), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def carregar_zabbix() -> CredenciaisZabbix | None:
    env_cred = _carregar_zabbix_env()
    if env_cred is not None:
        return env_cred
    if os.name != "nt":
        return None
    api = _api()
    ponteiro = PCREDENTIALW()
    if not api.CredReadW(ZABBIX_TARGET_NAME, CRED_TYPE_GENERIC, 0, ctypes.byref(ponteiro)):
        erro = ctypes.get_last_error()
        if erro == ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(erro)
    try:
        credencial = ponteiro.contents
        try:
            metadados = json.loads(credencial.UserName or "{}")
        except json.JSONDecodeError:
            metadados = {"api_url": credencial.UserName or "", "automatico": True}
        token = ctypes.string_at(
            credencial.CredentialBlob, credencial.CredentialBlobSize
        ).decode("utf-16-le")
        return CredenciaisZabbix(
            api_url=str(metadados.get("api_url") or "").strip(),
            token=token,
            automatico=bool(metadados.get("automatico", True)),
        )
    finally:
        api.CredFree(ponteiro)


def remover_zabbix() -> None:
    api = _api()
    if not api.CredDeleteW(ZABBIX_TARGET_NAME, CRED_TYPE_GENERIC, 0):
        erro = ctypes.get_last_error()
        if erro != ERROR_NOT_FOUND:
            raise ctypes.WinError(erro)
    if _carregar_zabbix_env() is not None:
        raise ValueError(
            "O token do Zabbix está definido no arquivo .env; limpe ZABBIX_API_TOKEN no .env para removê-lo."
        )


__all__ = [
    "CredenciaisVivo",
    "CredenciaisZabbix",
    "TARGET_NAME",
    "ZABBIX_TARGET_NAME",
    "carregar",
    "env_paths",
    "normalizar_cpf",
    "remover",
    "salvar",
    "carregar_zabbix",
    "remover_zabbix",
    "salvar_zabbix",
]
