# Componentes de terceiros — AI Gateway (shortsblog-ai-gateway)

Pacotes Python instalados na imagem, com a licença declarada por cada um (metadados do pip).
Os textos das licenças acompanham cada pacote em `site-packages/<pacote>.dist-info/`.

Observações (revisão de procedência da E19.6, `docs/reviews/E19.6-procedencia.md`):

- `psycopg`/`psycopg-binary` (LGPL-3.0): usados como biblioteca, sem modificação; podem ser
  trocados por outra versão com `pip install`.
- `certifi` (MPL-2.0): sem modificação.
- `text-unidecode` (Artistic ou GPL): usado sob a Artistic License.

| Pacote | Versão | Licença |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| certifi | 2026.7.22 | Mozilla Public License 2.0 (MPL 2.0) |
| click | 8.5.0 | BSD-3-Clause |
| fastapi | 0.142.2 | MIT |
| h11 | 0.16.0 | MIT License |
| httpcore | 1.0.9 | BSD-3-Clause |
| httptools | 0.8.0 | MIT |
| httpx | 0.28.1 | BSD License |
| idna | 3.20 | BSD-3-Clause |
| opentelemetry-api | 1.45.0 | Apache-2.0 |
| pip | 25.0.1 | MIT License |
| pydantic | 2.13.5 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| pydantic_core | 2.46.5 | MIT |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| PyYAML | 6.0.3 | MIT License |
| starlette | 1.7.0 | BSD-3-Clause |
| typing-inspection | 0.4.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| uvicorn | 0.54.0 | BSD-3-Clause |
| uvloop | 0.23.0 | Apache Software License; MIT License |
| watchfiles | 1.3.0 | MIT License |
| websockets | 17.2 | BSD-3-Clause |

## Programas fora do Python

| Programa | Licença | Na imagem publicada? |
|---|---|---|
| Node.js (lançador do Codex) | MIT (e as licenças dos seus componentes) | sim |
| OpenAI Codex CLI (`@openai/codex`) | Apache-2.0 — texto em `Apache-2.0-codex.txt` | sim |
| Claude Code (`@anthropic-ai/claude-code`) | proprietária (© Anthropic PBC) | **não** (`PROPRIETARY_CLIS=false`) |
| Google Antigravity CLI (`agy`) | proprietária (Google) | **não** (`PROPRIETARY_CLIS=false`) |
