# Robô de Abertura de Chamados Vivo (v5.5)

Aplicação Windows (janela Tkinter ou interface web local) para abertura automatizada de chamados técnicos de links **VPN IP MPLS** no **Portal Vivo Empresas (ServiceNow)**, com reconhecimento (*acknowledge*) automático de incidentes via API JSON-RPC do **Zabbix**.

## Principais Recursos

- **Validação em Bases Locais (Somente Leitura):** Cruza códigos de filial (`VD`) ou hosts do Zabbix (`DSP_...` / `DPA_...`) com a relação de lojas e inventário de links para validar lojas ativas, horários de funcionamento (incluindo unidades 24h) e designações MPLS Vivo.
- **Modo Individual e em Lote (até 50 VDs):** Reutiliza a mesma sessão autenticada do Google Chrome mantendo até 5 abas simultâneas em paralelo, com confirmação humana obrigatória do circuito localizado antes do envio.
- **Prevenção de Duplicidade e Segurança:** Janela configurável de bloqueio de duplicidade, proteção contra reenvio acidental, pausa automática diante de desafios extras (CAPTCHA/SMS) e geração de frase de apoio caso o protocolo não seja emitido em até 5 minutos.
- **Integração Zabbix:** Reconhece automaticamente o incidente registrando o Protocolo Vivo somente quando há correspondência exata do host e um único incidente ativo não reconhecido.
- **Interface Web Local:** A mesma automação pelo navegador, servida apenas em `127.0.0.1` com token por sessão. Inclui abas de chamado, lote com progresso em tempo real, histórico e configurações, além de um **modo simulação** para treinamento que não acessa o portal nem o Zabbix.
- **Credenciais Protegidas:** CPF/senha do portal Vivo e token da API do Zabbix são armazenados criptografados no **Gerenciador de Credenciais do Windows** (`Advapi32.dll`), nunca em arquivos `.ini`, `.bat` ou logs.

## Estrutura do Repositório

```text
Automação Vivo/
├── Executar automação vivo.bat              # Inicializador e validador do pacote portátil
├── Executar automação vivo (web).bat        # Abre a interface web local
├── Executar automação vivo (simulação).bat  # Interface web em modo simulação
└── arquivos/
    ├── configuracao.ini              # Parâmetros do portal, contato e caminhos
    ├── LEIA-ME.txt                   # Manual operacional detalhado
    ├── app/
    │   ├── main.py                   # Interface Tkinter, regras compartilhadas e entrada (--web, --simulacao, --self-test)
    │   ├── web_server.py             # Servidor web local (stdlib) e orquestração do worker para a página
    │   ├── web/                      # Página da interface web (HTML/CSS/JS, sem dependências externas)
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

## Interface Web

`main.py --web` sobe um servidor HTTP da biblioteca padrão em `127.0.0.1` (porta livre, configurável em `[web]` no `configuracao.ini`) e abre o navegador padrão. `--simulacao` faz o mesmo passando `--dry-run` ao worker: nenhum acesso ao portal, ao Zabbix ou ao histórico.

- **Regras compartilhadas:** montagem do chamado, trava de duplicidade, gravação do histórico, auditoria e reconhecimento no Zabbix, e localização do Node/worker ficam em funções de módulo no `main.py`, usadas pelas duas interfaces.
- **Segurança:** o token aleatório de cada execução vira um cookie `HttpOnly`/`SameSite=Strict`. Os `POST` exigem o cabeçalho `X-Robo-Vivo` e origem local, o `Host` é validado contra *DNS rebinding*, a CSP bloqueia scripts inline e as URLs não são registradas em log.
- **Instância única:** usa o mesmo mutex da janela Tkinter. Abrir o atalho de novo reabre a aba da sessão em andamento (`%LOCALAPPDATA%\RoboVivo\web_session.json`).

> **Empacotamento:** o executável portátil precisa ser gerado de novo com o PyInstaller a partir deste `main.py` para reconhecer `--web`/`--simulacao`. `web_server.py` e `web/` são lidos da pasta `app/`, como os demais módulos.
