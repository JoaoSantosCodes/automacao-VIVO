"""Leitura e normalizacao das bases usadas pelo robo da Vivo.

Este modulo trata os arquivos de origem como somente leitura.  Toda limpeza e
normalizacao acontece em memoria para que a planilha e o CSV nunca sejam
alterados pelo robo.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Mapping

import pandas as pd


LIMITE_DESCRICAO = 1000


MODELO_LINK_INTERROMPIDO = """Prezados,

Favor verificar a filial: {NOME_LOJA}

Se encontra com o link interrompido.

Filial: {VD} - {NOME_LOJA}

Reset: Sim

Designação: {DESIGNACAO}

Horário Funcionamento: {HORARIO}

OBS: Para o envio de um técnico à loja, é necessário encaminhar um e-mail para central.comando@dpsp.com.br
, contendo os dados do técnico e o VD da loja, conforme informado na descrição do chamado.

Contato para validações:

WhatsApp: (11) 99111-8142 (apenas mensagens)
Telefone: (11) 3274-7527

Grato!
"""

MODELO_PERCA_PACOTES = MODELO_LINK_INTERROMPIDO.replace(
    "Se encontra com o link interrompido.",
    "Se encontra com perca de pacotes.",
)


class DataServiceError(RuntimeError):
    """Erro de dados que pode ser apresentado ao operador."""


class BaseInvalida(DataServiceError):
    """A base nao existe, nao abre ou nao possui uma coluna obrigatoria."""


class CodigoInvalido(DataServiceError, ValueError):
    """O codigo de loja informado nao pode ser interpretado."""


class LojaNaoEncontrada(DataServiceError, LookupError):
    """Nao ha loja com o codigo solicitado."""


class LojaInativa(DataServiceError, LookupError):
    """A loja existe, mas nao esta ativa."""


class LinkNaoEncontrado(DataServiceError, LookupError):
    """Nao ha link ativo da Vivo do tipo MPLS para a loja."""


class DadosAmbiguos(DataServiceError, LookupError):
    """Mais de um registro atende aos criterios e requer escolha humana."""


class HorarioInvalido(DataServiceError, ValueError):
    """O horario da base nao segue um formato reconhecido."""


class DescricaoExcedeLimite(DataServiceError, ValueError):
    """A descricao ultrapassa o limite aceito pelo portal."""


@dataclass(frozen=True, slots=True)
class Horario:
    """Horario pronto para o portal e para o texto do chamado."""

    horario_script: str
    hora_inicio: str
    hora_fim: str
    vinte_quatro_horas: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Loja:
    """Registro de loja ativo e normalizado."""

    codigo: str
    vd: str
    nome: str
    endereco: str
    bairro: str
    cidade: str
    uf: str
    cep: str
    horario_script: str
    hora_inicio: str
    hora_fim: str
    vinte_quatro_horas: bool

    @property
    def endereco_completo(self) -> str:
        partes = [self.endereco, self.bairro]
        cidade_uf = "/".join(parte for parte in (self.cidade, self.uf) if parte)
        if cidade_uf:
            partes.append(cidade_uf)
        if self.cep:
            partes.append(f"CEP {self.cep}")
        return ", ".join(parte for parte in partes if parte)

    @property
    def palavras_chave_endereco(self) -> tuple[str, ...]:
        return palavras_chave_endereco(self.endereco)

    def to_dict(self) -> dict[str, Any]:
        dados = asdict(self)
        dados["endereco_completo"] = self.endereco_completo
        dados["palavras_chave_endereco"] = list(self.palavras_chave_endereco)
        return dados


@dataclass(frozen=True, slots=True)
class Link:
    """Link Vivo MPLS ativo e normalizado."""

    vd: str
    designacao: str
    usuario: str
    tipo_acesso: str
    operadora: str
    velocidade: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Chamado:
    """Dados validados necessarios para preencher o portal."""

    loja: Loja
    link: Link
    problema: str
    descricao: str

    @property
    def vd(self) -> str:
        return self.loja.vd

    @property
    def nome_loja(self) -> str:
        return self.loja.nome

    @property
    def endereco(self) -> str:
        return self.loja.endereco

    @property
    def endereco_completo(self) -> str:
        return self.loja.endereco_completo

    @property
    def designacao(self) -> str:
        return self.link.designacao

    @property
    def hora_inicio(self) -> str:
        return self.loja.hora_inicio

    @property
    def hora_fim(self) -> str:
        return self.loja.hora_fim

    @property
    def horario_script(self) -> str:
        return self.loja.horario_script

    def to_payload(self) -> dict[str, Any]:
        """Retorna o contrato plano consumido pela automacao do navegador."""

        return {
            "vd": self.loja.vd,
            "codigo": self.loja.codigo,
            "nome_loja": self.loja.nome,
            "endereco": self.loja.endereco,
            "endereco_completo": self.loja.endereco_completo,
            "bairro": self.loja.bairro,
            "cidade": self.loja.cidade,
            "uf": self.loja.uf,
            "cep": self.loja.cep,
            "palavras_chave_endereco": list(self.loja.palavras_chave_endereco),
            "designacao": self.link.designacao,
            "usuario": self.link.usuario,
            "tipo_acesso": self.link.tipo_acesso,
            "operadora": self.link.operadora,
            "velocidade": self.link.velocidade,
            "hora_inicio": self.loja.hora_inicio,
            "hora_fim": self.loja.hora_fim,
            "horario_script": self.loja.horario_script,
            "vinte_quatro_horas": self.loja.vinte_quatro_horas,
            "problema": self.problema,
            "descricao": self.descricao,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_payload()


def _sem_acentos(valor: Any) -> str:
    texto = unicodedata.normalize("NFKD", str(valor))
    return "".join(ch for ch in texto if not unicodedata.combining(ch))


def _chave_texto(valor: Any) -> str:
    return re.sub(r"\s+", " ", _sem_acentos(valor).strip()).upper()


def _slug_cabecalho(valor: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", _chave_texto(valor))


def _texto_limpo(valor: Any) -> str:
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return ""
    return " ".join(str(valor).replace("\r", " ").replace("\n", " ").split())


def canonicalizar_codigo(valor: Any) -> str:
    """Converte ``1517``, ``VD1517`` e ``L1517`` para ``1517``."""

    if valor is None:
        raise CodigoInvalido("Informe o VD da loja.")
    texto = _texto_limpo(valor).upper()
    correspondencia = re.fullmatch(r"(?:VD|L)?\s*0*(\d+)(?:\.0+)?", texto)
    if not correspondencia:
        raise CodigoInvalido(
            f"Codigo de loja invalido: {valor!r}. Use, por exemplo, 1517 ou VD1517."
        )
    numero = correspondencia.group(1)
    return str(int(numero))


def rotulo_vd(valor: Any) -> str:
    """Gera o rotulo VD com, no minimo, tres algarismos."""

    codigo = canonicalizar_codigo(valor)
    return f"VD{codigo.zfill(3)}"


def _eh_24_horas(valor: Any) -> bool:
    chave = re.sub(r"[^A-Z0-9]+", "", _chave_texto(valor))
    return chave in {"24HORAS", "24H", "24HRS"}


def _formatar_hora_script(hora: int, minuto: int) -> str:
    if minuto:
        return f"{hora:02d}:{minuto:02d}"
    return f"{hora:02d}"


def normalizar_horario_funcionamento(valor: Any) -> Horario:
    """Normaliza um horario da base para portal e descricao.

    Reconhece as grafias ``AS``, ``AS`` com acento grave/agudo, horarios com
    minutos e tambem ``24 HORAS``. A separacao do texto de saida e sempre
    ``AS`` com acento grave (``ÀS``).
    """

    texto_original = _texto_limpo(valor)
    if _eh_24_horas(texto_original):
        return Horario("24 HORAS", "00:00", "23:59", True)

    texto = _chave_texto(texto_original)
    padrao = re.compile(
        r"^\s*(\d{1,2})(?:\s*[:H]\s*(\d{1,2}))?\s*"
        r"(?:AS|A|-)\s*"
        r"(\d{1,2})(?:\s*[:H]\s*(\d{1,2}))?\s*$"
    )
    correspondencia = padrao.fullmatch(texto)
    if not correspondencia:
        raise HorarioInvalido(
            f"Horario de funcionamento nao reconhecido: {texto_original!r}."
        )

    hora_inicio = int(correspondencia.group(1))
    minuto_inicio = int(correspondencia.group(2) or 0)
    hora_fim = int(correspondencia.group(3))
    minuto_fim = int(correspondencia.group(4) or 0)
    if not (0 <= hora_inicio <= 23 and 0 <= hora_fim <= 23):
        raise HorarioInvalido(f"Hora fora do intervalo permitido: {texto_original!r}.")
    if not (0 <= minuto_inicio <= 59 and 0 <= minuto_fim <= 59):
        raise HorarioInvalido(
            f"Minuto fora do intervalo permitido: {texto_original!r}."
        )

    inicio_portal = f"{hora_inicio:02d}:{minuto_inicio:02d}"
    fim_portal = f"{hora_fim:02d}:{minuto_fim:02d}"
    script = (
        f"{_formatar_hora_script(hora_inicio, minuto_inicio)} ÀS "
        f"{_formatar_hora_script(hora_fim, minuto_fim)}"
    )
    return Horario(script, inicio_portal, fim_portal, False)


def palavras_chave_endereco(endereco: Any) -> tuple[str, ...]:
    """Extrai termos uteis para a busca aproximada no portal."""

    texto = _chave_texto(_texto_limpo(endereco))
    tokens = re.findall(r"[A-Z0-9]+", texto)
    logradouros = {
        "RUA",
        "R",
        "AV",
        "AVENIDA",
        "ALAMEDA",
        "AL",
        "ESTRADA",
        "EST",
        "RODOVIA",
        "ROD",
        "TRAVESSA",
        "TRAV",
        "PRACA",
        "PC",
    }
    resultado: list[str] = []
    for token in tokens:
        if token in logradouros or token in resultado:
            continue
        resultado.append(token)
    return tuple(resultado)


def _resolver_colunas(
    frame: pd.DataFrame, especificacao: Mapping[str, Iterable[str]], nome_base: str
) -> dict[str, str]:
    por_slug: dict[str, str] = {}
    for coluna in frame.columns:
        por_slug.setdefault(_slug_cabecalho(coluna), str(coluna))

    resolvidas: dict[str, str] = {}
    ausentes: list[str] = []
    for campo, alternativas in especificacao.items():
        encontrada = next(
            (por_slug[_slug_cabecalho(nome)] for nome in alternativas if _slug_cabecalho(nome) in por_slug),
            None,
        )
        if encontrada is None:
            ausentes.append(next(iter(alternativas)))
        else:
            resolvidas[campo] = encontrada
    if ausentes:
        raise BaseInvalida(
            f"A base {nome_base} nao possui as colunas obrigatorias: "
            + ", ".join(ausentes)
            + "."
        )
    return resolvidas


def _normalizar_cep(valor: Any) -> str:
    texto = _texto_limpo(valor)
    if not texto:
        return ""
    digitos = re.sub(r"\D", "", texto)
    if digitos and len(digitos) <= 8:
        return digitos.zfill(8)
    return texto


def _normalizar_problema(valor: Any) -> str:
    chave = _slug_cabecalho(valor)
    if chave in {"1", "LINKINTERROMPIDO", "LINKINOPERANTE", "INOPERANTE"}:
        return "link_interrompido"
    if chave in {
        "2",
        "PERCAPACOTES",
        "PERCADEPACOTES",
        "PERDAPACOTES",
        "PERDADEPACOTES",
    }:
        return "perca_pacotes"
    raise DataServiceError(
        "Problema invalido. Escolha link_interrompido ou perca_pacotes."
    )


def gerar_descricao(
    problema: Any,
    loja: Loja,
    link: Link,
    *,
    modelo_link_interrompido: str | None = None,
    modelo_perca_pacotes: str | None = None,
) -> str:
    """Preenche o modelo do incidente e valida o limite do portal."""

    problema_normalizado = _normalizar_problema(problema)
    if problema_normalizado == "link_interrompido":
        modelo = modelo_link_interrompido or MODELO_LINK_INTERROMPIDO
    else:
        modelo = modelo_perca_pacotes or MODELO_PERCA_PACOTES

    try:
        descricao = modelo.format(
            NOME_LOJA=loja.nome,
            VD=loja.vd,
            DESIGNACAO=link.designacao,
            HORARIO=loja.horario_script,
        )
    except KeyError as exc:
        raise BaseInvalida(
            f"O modelo de descricao possui um marcador desconhecido: {exc.args[0]}."
        ) from exc

    if len(descricao) > LIMITE_DESCRICAO:
        raise DescricaoExcedeLimite(
            f"A descricao possui {len(descricao)} caracteres; o limite do portal e "
            f"{LIMITE_DESCRICAO}."
        )
    return descricao


class DataService:
    """Consulta as bases locais sem modifica-las."""

    _COLUNAS_LOJAS = {
        "status": ("STATUS",),
        "codigo": ("CODIGO", "CÓDIGO"),
        "nome": ("LOJAS", "LOJA", "NOME DA LOJA"),
        "endereco": ("ENDEREÇO", "ENDERECO"),
        "bairro": ("BAIRRO",),
        "cidade": ("CIDADE",),
        "uf": ("ESTADO", "UF"),
        "cep": ("CEP",),
        "sabado": ("SAB", "SÁBADO", "SABADO"),
        "domingo": ("DOM", "DOMINGO"),
        "semana": ("2ª a 6ª", "2a a 6a", "SEGUNDA A SEXTA"),
    }
    _COLUNAS_LINKS = {
        "status": ("Status", "STATUS"),
        "vd": ("VD", "CODIGO", "CÓDIGO"),
        "usuario": ("USUARIO", "USUÁRIO"),
        "tipo_acesso": ("TIPO DE ACESSO",),
        "operadora": ("OPERADORA",),
        "designacao": (
            "NÚMERO (DESIGNAÇÃO)",
            "NUMERO (DESIGNACAO)",
            "DESIGNAÇÃO",
            "DESIGNACAO",
        ),
        "velocidade": ("VELOCIDADE",),
    }

    def __init__(
        self,
        lojas_path: str | Path,
        links_path: str | Path,
        modelo_link_interrompido: str | Path | None = None,
        modelo_perca_pacotes: str | Path | None = None,
    ) -> None:
        self.lojas_path = Path(lojas_path)
        self.links_path = Path(links_path)
        self.modelo_link_interrompido_path = (
            Path(modelo_link_interrompido) if modelo_link_interrompido else None
        )
        self.modelo_perca_pacotes_path = (
            Path(modelo_perca_pacotes) if modelo_perca_pacotes else None
        )
        self._lojas: pd.DataFrame | None = None
        self._links: pd.DataFrame | None = None
        self._colunas_lojas: dict[str, str] | None = None
        self._colunas_links: dict[str, str] | None = None

    @staticmethod
    def _verificar_arquivo(caminho: Path, descricao: str) -> None:
        if not caminho.is_file():
            raise BaseInvalida(f"Arquivo de {descricao} nao encontrado: {caminho}.")

    def _carregar_lojas(self) -> tuple[pd.DataFrame, dict[str, str]]:
        if self._lojas is None:
            self._verificar_arquivo(self.lojas_path, "lojas")
            try:
                frame = pd.read_excel(
                    self.lojas_path,
                    dtype=str,
                    keep_default_na=False,
                    engine="openpyxl",
                )
            except Exception as exc:  # pandas/openpyxl fornecem varios tipos
                raise BaseInvalida(
                    f"Nao foi possivel ler a base de lojas: {self.lojas_path}."
                ) from exc
            colunas = _resolver_colunas(frame, self._COLUNAS_LOJAS, "de lojas")
            self._lojas = frame
            self._colunas_lojas = colunas
        return self._lojas, self._colunas_lojas or {}

    def _carregar_links(self) -> tuple[pd.DataFrame, dict[str, str]]:
        if self._links is None:
            self._verificar_arquivo(self.links_path, "links")
            try:
                # O parser do pandas respeita campos CSV entre aspas que ocupam
                # mais de uma linha. O cp1252 e deliberado: e o encoding da base.
                frame = pd.read_csv(
                    self.links_path,
                    encoding="cp1252",
                    dtype=str,
                    keep_default_na=False,
                    engine="python",
                )
            except Exception as exc:
                raise BaseInvalida(
                    f"Nao foi possivel ler a base de links: {self.links_path}."
                ) from exc
            colunas = _resolver_colunas(frame, self._COLUNAS_LINKS, "de links")
            self._links = frame
            self._colunas_links = colunas
        return self._links, self._colunas_links or {}

    def localizar_loja(self, vd: Any) -> Loja:
        codigo = canonicalizar_codigo(vd)
        frame, colunas = self._carregar_lojas()

        codigos = frame[colunas["codigo"]].map(
            lambda valor: _codigo_ou_vazio(valor)
        )
        correspondentes = frame.loc[codigos.eq(codigo)]
        if correspondentes.empty:
            raise LojaNaoEncontrada(f"Loja {rotulo_vd(codigo)} nao encontrada na base.")

        ativas = correspondentes.loc[
            correspondentes[colunas["status"]].map(_chave_texto).eq("ATIVA")
        ]
        if ativas.empty:
            raise LojaInativa(f"A loja {rotulo_vd(codigo)} consta como inativa.")
        if len(ativas) > 1:
            raise DadosAmbiguos(
                f"A base possui {len(ativas)} lojas ativas para {rotulo_vd(codigo)}."
            )

        linha = ativas.iloc[0]
        sabado = linha[colunas["sabado"]]
        domingo = linha[colunas["domingo"]]
        semana = linha[colunas["semana"]]
        vinte_quatro_horas = all(
            _eh_24_horas(valor) for valor in (sabado, domingo, semana)
        )
        if vinte_quatro_horas:
            horario = Horario("24 HORAS", "00:00", "23:59", True)
        else:
            horario_semana = normalizar_horario_funcionamento(semana)
            horario = Horario(
                horario_semana.horario_script,
                horario_semana.hora_inicio,
                horario_semana.hora_fim,
                False,
            )

        return Loja(
            codigo=codigo,
            vd=rotulo_vd(codigo),
            nome=_texto_limpo(linha[colunas["nome"]]),
            endereco=_texto_limpo(linha[colunas["endereco"]]),
            bairro=_texto_limpo(linha[colunas["bairro"]]),
            cidade=_texto_limpo(linha[colunas["cidade"]]),
            uf=_texto_limpo(linha[colunas["uf"]]).upper(),
            cep=_normalizar_cep(linha[colunas["cep"]]),
            horario_script=horario.horario_script,
            hora_inicio=horario.hora_inicio,
            hora_fim=horario.hora_fim,
            vinte_quatro_horas=horario.vinte_quatro_horas,
        )

    def localizar_links(self, vd: Any) -> list[Link]:
        codigo = canonicalizar_codigo(vd)
        frame, colunas = self._carregar_links()
        codigos = frame[colunas["vd"]].map(lambda valor: _codigo_ou_vazio(valor))
        filtro = (
            codigos.eq(codigo)
            & frame[colunas["status"]].map(_chave_texto).eq("ATIVO")
            & frame[colunas["operadora"]].map(_chave_texto).eq("VIVO")
            & frame[colunas["tipo_acesso"]].map(_chave_texto).eq("MPLS")
        )
        registros = frame.loc[filtro]
        if registros.empty:
            raise LinkNaoEncontrado(
                f"Nao ha link ATIVO da VIVO do tipo MPLS para {rotulo_vd(codigo)}."
            )

        links: list[Link] = []
        for _, linha in registros.iterrows():
            links.append(
                Link(
                    vd=rotulo_vd(codigo),
                    designacao=_texto_limpo(linha[colunas["designacao"]]),
                    usuario=_texto_limpo(linha[colunas["usuario"]]),
                    tipo_acesso=_texto_limpo(linha[colunas["tipo_acesso"]]).upper(),
                    operadora=_texto_limpo(linha[colunas["operadora"]]).upper(),
                    velocidade=_texto_limpo(linha[colunas["velocidade"]]),
                )
            )
        return links

    def preparar_chamado(
        self, vd: Any, problema: Any, designacao: str | None = None
    ) -> Chamado:
        loja = self.localizar_loja(vd)
        links = self.localizar_links(vd)
        if designacao is not None:
            procurada = _texto_limpo(designacao)
            candidatos = [link for link in links if link.designacao == procurada]
            if not candidatos:
                raise LinkNaoEncontrado(
                    f"A designacao {procurada!r} nao e um link Vivo MPLS ativo de {loja.vd}."
                )
            if len(candidatos) > 1:
                raise DadosAmbiguos(
                    f"A designacao {procurada!r} aparece mais de uma vez para {loja.vd}."
                )
            link = candidatos[0]
        elif len(links) == 1:
            link = links[0]
        else:
            raise DadosAmbiguos(
                f"Ha {len(links)} links Vivo MPLS ativos para {loja.vd}; escolha a designacao."
            )

        problema_normalizado = _normalizar_problema(problema)
        descricao = gerar_descricao(
            problema_normalizado,
            loja,
            link,
            modelo_link_interrompido=self._ler_modelo(
                self.modelo_link_interrompido_path, MODELO_LINK_INTERROMPIDO
            ),
            modelo_perca_pacotes=self._ler_modelo(
                self.modelo_perca_pacotes_path, MODELO_PERCA_PACOTES
            ),
        )
        return Chamado(loja, link, problema_normalizado, descricao)

    @staticmethod
    def _ler_modelo(caminho: Path | None, padrao: str) -> str:
        if caminho is None:
            return padrao
        if not caminho.is_file():
            raise BaseInvalida(f"Modelo de descricao nao encontrado: {caminho}.")
        try:
            return caminho.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            # Mantem compatibilidade com um modelo salvo pelo Bloco de Notas em
            # uma instalacao Windows mais antiga.
            return caminho.read_text(encoding="cp1252")

    # Aliases em ingles para facilitar integracoes e testes externos.
    find_store = localizar_loja
    find_links = localizar_links
    build_ticket = preparar_chamado


def _codigo_ou_vazio(valor: Any) -> str:
    try:
        return canonicalizar_codigo(valor)
    except CodigoInvalido:
        return ""


# Nomes equivalentes, sem duplicar implementacao.
Store = Loja
Circuit = Link
Ticket = Chamado
DataRepository = DataService
normalize_store_code = canonicalizar_codigo
format_vd = rotulo_vd
normalize_business_hours = normalizar_horario_funcionamento
render_description = gerar_descricao


__all__ = [
    "BaseInvalida",
    "Chamado",
    "Circuit",
    "CodigoInvalido",
    "DataRepository",
    "DataService",
    "DataServiceError",
    "DadosAmbiguos",
    "DescricaoExcedeLimite",
    "Horario",
    "HorarioInvalido",
    "LIMITE_DESCRICAO",
    "Link",
    "LinkNaoEncontrado",
    "Loja",
    "LojaInativa",
    "LojaNaoEncontrada",
    "Store",
    "Ticket",
    "canonicalizar_codigo",
    "format_vd",
    "gerar_descricao",
    "normalize_business_hours",
    "normalize_store_code",
    "normalizar_horario_funcionamento",
    "palavras_chave_endereco",
    "render_description",
    "rotulo_vd",
]
