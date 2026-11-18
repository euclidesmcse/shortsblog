# Componentes de terceiros — motor (shortsblog-engine)

Pacotes Python instalados na imagem, com a licença declarada por cada um (metadados do pip).
Os textos das licenças acompanham cada pacote em `site-packages/<pacote>.dist-info/`.

Observações (revisão de procedência da E19.6, `docs/reviews/E19.6-procedencia.md`):

- `psycopg`/`psycopg-binary` (LGPL-3.0): usados como biblioteca, sem modificação; podem ser
  trocados por outra versão com `pip install`.
- `certifi` (MPL-2.0): sem modificação.
- `text-unidecode` (Artistic ou GPL): usado sob a Artistic License.

| Pacote | Versão | Licença |
|---|---|---|
| annotated-doc | 0.0.6 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.16.0 | MIT |
| APScheduler | 3.11.3 | MIT License |
| argon2-cffi | 25.1.0 | MIT |
| argon2-cffi-bindings | 26.2.0 | MIT |
| beautifulsoup4 | 4.15.0 | MIT License |
| certifi | 2026.10.23 | Mozilla Public License 2.0 (MPL 2.0) |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.1 | BSD-3-Clause |
| croniter | 7.0.0 | MIT |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| fastapi | 0.142.2 | MIT |
| feedparser | 6.0.14 | BSD-2-Clause |
| feedparser-sgmllib | 2.1.0 | PSF-2.0 |
| h11 | 0.16.0 | MIT License |
| httpcore | 1.0.9 | BSD-3-Clause |
| httptools | 0.9.0 | MIT |
| httpx | 0.28.1 | BSD License |
| idna | 3.20 | BSD-3-Clause |
| itsdangerous | 2.2.0 | BSD License |
| Jinja2 | 3.1.6 | BSD License |
| lxml | 6.1.3 | BSD-3-Clause |
| Markdown | 3.11 | BSD-3-Clause |
| MarkupSafe | 3.0.4 | BSD-3-Clause |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| pillow | 12.3.0 | MIT-CMU |
| pip | 25.0.1 | MIT License |
| prometheus_client | 0.26.0 | Apache-2.0 AND BSD-2-Clause |
| psycopg | 3.3.6 | LGPL-3.0-only |
| psycopg-binary | 3.3.6 | LGPL-3.0-only |
| pycparser | 3.12 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| pydantic_core | 2.46.5 | MIT |
| PyJWT | 2.15.1 | MIT |
| pypdf | 6.21.4 | BSD-3-Clause |
| python-dateutil | 4.0.0 | BSD-3-Clause |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| python-slugify | 9.1.2 | MIT |
| PyYAML | 6.0.3 | MIT License |
| redis | 8.1.0 | MIT |
| rq | 2.12.0 | BSD-2-Clause |
| soupsieve | 4.1.1 | MIT |
| SQLAlchemy | 2.1.2 | MIT |
| starlette | 1.10.0 | BSD-3-Clause |
| tenacity | 9.1.4 | Apache Software License |
| text-unidecode | 1.3 | Artistic License; GNU General Public License (GPL); GNU General Public License v2 or later (GPLv2+) |
| typing-inspection | 0.4.5 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| tzdata | 2026.6 | Apache-2.0 |
| tzlocal | 5.4.5 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| uvloop | 0.23.0 | Apache Software License; MIT License |
| watchfiles | 1.3.0 | MIT License |
| websockets | 17.4 | BSD-3-Clause |
