# Robô de Abertura de Chamados Vivo (v5.5)

Aplicação desktop Windows para abertura automatizada de chamados técnicos de links **VPN IP MPLS** no **Portal Vivo Empresas (ServiceNow)**, com reconhecimento (*acknowledge*) automático de incidentes via API JSON-RPC do **Zabbix**.

## Principais Recursos

- **Validação em Bases Locais (Somente Leitura):** Cruza códigos de filial (`VD`) ou hosts do Zabbix (`DSP_...` / `DPA_...`) com a relação de lojas e inventário de links para validar lojas ativas, horários de funcionamento (incluindo unidades 24h) e designações MPLS Vivo.
- **Modo Individual e em Lote (até 50 VDs):** Reutiliza a mesma sessão autenticada do Google Chrome mantendo até 5 abas simultâneas em paralelo, com confirmação humana obrigatória do circuito localizado antes do envio.
- **Prevenção de Duplicidade e Segurança:** Janela configurável de bloqueio de duplicidade, proteção contra reenvio acidental, pausa automática diante de desafios extras (CAPTCHA/SMS) e geração de frase de apoio caso o protocolo não seja emitido em até 5 minutos.
- **Integração Zabbix:** Reconhece automaticamente o incidente registrando o Protocolo Vivo somente quando há correspondência exata do host e um único incidente ativo não reconhecido.
- **Credenciais Protegidas:** CPF/senha do portal Vivo e token da API do Zabbix são armazenados criptografados no **Gerenciador de Credenciais do Windows** (`Advapi32.dll`), nunca em arquivos `.ini`, `.bat` ou logs.

## Estrutura do Repositório

```text
Automação Vivo/
├── Executar automação vivo.bat       # Inicializador e validador do pacote portátil
└── arquivos/
    ├── configuracao.ini              # Parâmetros do portal, contato e caminhos
    ├── LEIA-ME.txt                   # Manual operacional detalhado
    ├── app/
    │   ├── main.py                   # Interface gráfica (Tkinter) e orquestração
    │   ├── data_service.py           # Leitura e normalização das bases (Pandas/OpenPyXL)
    │   ├── vivo_worker.mjs           # Automação do navegador via Playwright (JSON Lines)
    │   ├── zabbix_client.py          # Cliente JSON-RPC para reconhecimento no Zabbix
    │   └── credential_store.py       # Integração com o Windows Credential Manager
    ├── assets/                       # Ícones e identidade visual da interface
    ├── modelos/                      # Templates de abertura (link inoperante / perda de pacotes)
    ├── data/                         # Bases locais (.xlsx / .csv) — ignoradas pelo Git
    └── historico/                    # Logs e histórico de auditoria — ignorados pelo Git
```

## Requisitos de Desenvolvimento

- **Sistema Operacional:** Windows 10/11 (64 bits) com **Google Chrome** instalado
- **Python:** 3.12+ (`pandas`, `openpyxl`, `tkinter`)
- **Node.js:** 20+ (`playwright`)

> **Nota:** Planilhas de inventário (`data/`), logs/históricos operacionais (`historico/`) e binários empacotados (`_internal/`, `runtime/`, `.exe`) não são versionados neste repositório por motivos de segurança e tamanho.
