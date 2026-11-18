# ShortsBlog — motor de conteúdo

Planeja, redige, revisa, ilustra e publica posts nos blogs — e **nada é publicado sem um clique de
aprovação humana**.

```
feeds + ideias ──▶ slot do dia ──▶ pesquisa ──▶ redação ──▶ validação + revisão (IA) ──▶ tradução ──▶ gate humano ──▶ WordPress
```

- API e portal de operação: FastAPI (`app/main.py`, telas em `app/dashboard/templates`).
- Workers RQ (`app/jobs/worker.py`) e agendador interno (`app/jobs/scheduler.py`,
  `INTERNAL_SCHEDULER_ENABLED=true`), que dispara os horários `/api/sched/*`.
- A IA passa pelo AI Gateway (`app/services/ai/`), com redator e revisor de famílias diferentes.
- Migrações SQL em `db/migrations` (`python -m app.core.migrate`, com o usuário admin do banco;
  as apps usam um papel só com DML).

## Desenvolvimento

```bash
make check      # ruff + mypy + pytest (Postgres e Redis efêmeros via compose.test.yml)
make test ARGS="-q tests/test_setup_panel.py"
```

Licença: AGPL-3.0 (`licenses/`, e o `LICENSE` na raiz do projeto).
