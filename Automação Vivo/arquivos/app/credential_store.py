"""Armazenamento local seguro do acesso ao Portal Vivo.

A senha fica no Gerenciador de Credenciais do Windows, criptografada para o
usuário atual. Este módulo nunca grava a senha no BAT, no INI ou nos logs.
"""

from __future__ import annotations

import ctypes
import os
import json
import re
from ctypes import wintypes
from dataclasses import dataclass


TARGET_NAME = "DPSP_RoboVivo_PortalVivo"
ZABBIX_TARGET_NAME = "DPSP_RoboVivo_ZabbixAPI"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


@dataclass(frozen=True, slots=True)
class CredenciaisVivo:
    cpf: str
    senha: str


@dataclass(frozen=True, slots=True)
class CredenciaisZabbix:
    api_url: str
    token: str
    automatico: bool = True


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
    if api.CredDeleteW(TARGET_NAME, CRED_TYPE_GENERIC, 0):
        return
    erro = ctypes.get_last_error()
    if erro != ERROR_NOT_FOUND:
        raise ctypes.WinError(erro)


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
    if api.CredDeleteW(ZABBIX_TARGET_NAME, CRED_TYPE_GENERIC, 0):
        return
    erro = ctypes.get_last_error()
    if erro != ERROR_NOT_FOUND:
        raise ctypes.WinError(erro)


__all__ = [
    "CredenciaisVivo",
    "CredenciaisZabbix",
    "TARGET_NAME",
    "ZABBIX_TARGET_NAME",
    "carregar",
    "normalizar_cpf",
    "remover",
    "salvar",
    "carregar_zabbix",
    "remover_zabbix",
    "salvar_zabbix",
]
