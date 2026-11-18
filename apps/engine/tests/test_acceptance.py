"""E13 — cenários de aceitação que ainda não tinham um teste automático próprio."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.db import session_scope
from app.services.notify import digests
from tests.conftest import advance
from tests.test_digests import EVENING, UP, Outbox
from tests.test_sites import cloud, insert_job  # noqa: F401  (fixture `cloud`: os 6 sites)

SITES = ("cloud", "ia", "financas", "saude", "politica", "culinaria")


def test_site_key_inexistente_nao_chega_a_haver_job(cloud):  # noqa: F811
    """Um job de um blog inexistente é impossível (chave estrangeira)."""
    with session_scope() as db, pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO publication_jobs (site_key, local_date, slot_number, due_at_utc, "
                "produce_after_utc) VALUES ('nao-existe', '2026-10-05', 1, now(), now())"
            )
        )


def test_resumo_com_30_publicados_e_sem_avisos(cloud, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "approval_notify_email", "euclides@example.com")
    monkeypatch.setattr(settings, "wp_publish_mode", "live")
    with session_scope() as db:
        db.execute(text("UPDATE sites SET enabled = true"))
    for site in SITES:
        for slot in range(1, 6):
            job = insert_job(site, slot, "PLANNED")
            advance(job, *UP, "APPROVED", "PUBLISHING", "PUBLISHED_CONFIRMED")
            with session_scope() as db:
                db.execute(
                    text(
                        "UPDATE publication_jobs SET wordpress_url = :u, published_at_utc = :t "
                        "WHERE id = :i"
                    ),
                    {
                        "u": f"https://{site}.exemplo.test/{slot}/",
                        "t": datetime(2026, 10, 5, 13, tzinfo=UTC),
                        "i": job,
                    },
                )
    out = Outbox()
    result = digests.daily_digest(EVENING, out)
    assert result["sent"] is True and result["subject"].endswith("| 30/30 publicados")
    assert "⚠️" not in out.sent[0][2] and "Blog desligado" not in out.sent[0][2]
    for site in SITES:
        assert f"https://{site}.exemplo.test/5/" in out.sent[0][2]
