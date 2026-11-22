"""E19 — fuso da instalação: dia local e horários mostrados às pessoas (sem São Paulo fixo)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core import localtime
from app.core.config import settings

MOMENT = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def test_o_fuso_local_e_o_tz_da_instalacao(monkeypatch):
    monkeypatch.setattr(settings, "tz", "Asia/Tokyo")
    assert localtime.local_zone().key == "Asia/Tokyo"
    # TZ inválido ou vazio não derruba nada: UTC
    monkeypatch.setattr(settings, "tz", "Marte/Olympus")
    assert localtime.local_zone().key == "UTC"
    monkeypatch.setattr(settings, "tz", "")
    assert localtime.local_zone().key == "UTC"


def test_rotulo_vem_do_panel_timezones_ou_da_cidade(monkeypatch):
    monkeypatch.setattr(settings, "tz", "America/Sao_Paulo")
    monkeypatch.setattr(settings, "panel_timezones", "")
    assert localtime.clock(MOMENT) == "09:00 Sao Paulo"
    # A nossa produção: SP com rótulo próprio, mesmo não sendo o 1.º da lista
    monkeypatch.setattr(settings, "panel_timezones", "Europe/Lisbon=Lisboa,America/Sao_Paulo=SP")
    assert localtime.clock(MOMENT) == "09:00 SP"
    # Instalação noutro fuso que não está na lista: a cidade
    monkeypatch.setattr(settings, "tz", "Asia/Tokyo")
    assert localtime.clock(MOMENT) == "21:00 Tokyo"
