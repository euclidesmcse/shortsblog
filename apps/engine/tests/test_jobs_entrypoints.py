"""Pontos de entrada dos containers worker e scheduler (sem bloquear o teste)."""
from __future__ import annotations

import time

import pytest

from app.jobs import scheduler, worker


class FakeScheduler:
    instances: list[FakeScheduler] = []

    def __init__(self, timezone: str) -> None:
        self.timezone = timezone
        self.jobs: list[dict] = []
        self.started = False
        FakeScheduler.instances.append(self)

    def add_job(self, func, trigger, **kwargs) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def start(self) -> None:
        self.started = True


@pytest.fixture(autouse=True)
def _heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "HEARTBEAT_FILE", tmp_path / "hb")


def test_scheduler_ativo_agenda_as_tarefas_de_casa_e_os_8_horarios(monkeypatch):
    FakeScheduler.instances.clear()
    monkeypatch.setattr(scheduler, "ENABLED", True)
    monkeypatch.setattr(scheduler, "BlockingScheduler", FakeScheduler)
    scheduler.main()

    sched = FakeScheduler.instances[0]
    assert sched.started and sched.timezone == "America/Sao_Paulo"
    # E19: as fases do pipeline antigo saíram; ficam as tarefas de casa e (E19.2) os 8 horários
    # que o n8n dispara na nossa instalação.
    routes = ["plan-day", "dispatch", "publish-due", "reconcile", "discover", "approval-batch",
              "digest", "newsletter"]
    assert {job["id"] for job in sched.jobs} == {
        "sb-heartbeat", "sb-watchdog", "sb-monitoring", "sb-ai-probe",
        *(f"sb-route-{r}" for r in routes)}
    for job in sched.jobs:
        if job["id"].startswith("sb-route-"):
            assert job["func"] is scheduler.run_route and job["max_instances"] == 1
            assert job["args"][0] == job["id"].removeprefix("sb-route-")
    probe = [job for job in sched.jobs if job["id"] == "sb-ai-probe"]
    assert probe and probe[0]["func"] is scheduler.probe_ai
    assert probe[0]["seconds"] == 300 and probe[0]["max_instances"] == 1
    heartbeat = [job for job in sched.jobs if job["id"] == "sb-heartbeat"]
    assert heartbeat and heartbeat[0]["func"] is scheduler.beat
    assert scheduler.is_healthy()


def test_scheduler_desativado_fica_parado(monkeypatch):
    class Stop(Exception):
        pass

    calls = []

    def fake_sleep(seconds):
        assert seconds == scheduler.HEARTBEAT_EVERY
        calls.append(seconds)
        if len(calls) == 2:
            raise Stop

    probes = []
    monkeypatch.setattr(scheduler, "probe_ai", lambda: probes.append(1))
    monkeypatch.setattr(scheduler, "ENABLED", False)
    monkeypatch.setattr(scheduler, "BlockingScheduler",
                        lambda **_: pytest.fail("não devia criar o scheduler"))
    monkeypatch.setattr(time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        scheduler.main()
    assert scheduler.is_healthy()  # continua a bater mesmo desativado
    # O probe de IA corre logo ao arrancar e depois só de 5 em 5 minutos.
    assert probes == [1]


def test_horarios_iguais_aos_do_n8n():
    """Os gatilhos batem com os workflows exportados (fuso do motor)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Sao_Paulo")
    start = datetime(2026, 11, 17, 0, 0, tzinfo=tz)

    def fires(trigger, n=6):
        out, prev = [], None
        now = start
        for _ in range(n):
            nxt = trigger.get_next_fire_time(prev, now)
            out.append(nxt.strftime("%H:%M"))
            prev, now = nxt, nxt
        return out

    t = {route: trigger for route, _, trigger in scheduler.ROUTES}
    assert fires(t["plan-day"], 2) == ["00:05", "00:05"]
    assert fires(t["dispatch"], 3) == ["00:00", "00:01", "00:02"]
    assert fires(t["reconcile"], 3) == ["00:00", "00:05", "00:10"]
    assert fires(t["discover"], 3) == ["00:00", "01:00", "02:00"]
    assert fires(t["approval-batch"], 6) == ["06:30", "10:00", "13:00", "16:00", "20:00", "06:30"]
    assert fires(t["digest"], 1) == ["21:00"] and fires(t["newsletter"], 1) == ["07:00"]
    from app.api.routes import sched
    assert all(callable(getattr(sched, func)) for _, func, _ in scheduler.ROUTES)


def test_run_route_regista_no_vigia_e_chama_a_funcao(monkeypatch):
    from app.api.routes import sched
    from app.services import watchdog

    seen = []
    monkeypatch.setattr(sched, "reconcile", lambda: seen.append("chamou") or {"ok": 1})
    monkeypatch.setattr(watchdog, "record", lambda route: seen.append(route))
    scheduler.run_route("reconcile", "reconcile")
    assert seen == ["reconcile", "chamou"]

    def boom():
        raise RuntimeError("caiu")

    monkeypatch.setattr(sched, "reconcile", boom)
    scheduler.run_route("reconcile", "reconcile")  # não levanta: os outros horários seguem


def test_heartbeat_velho_ou_ausente_e_doente():
    assert not scheduler.is_healthy()  # ainda não existe
    scheduler.beat()
    assert scheduler.is_healthy()
    assert not scheduler.is_healthy(now=time.time() + scheduler.HEARTBEAT_MAX_AGE + 1)
    scheduler.HEARTBEAT_FILE.write_text("lixo")
    assert not scheduler.is_healthy()


def test_worker_consome_filas_por_prioridade(monkeypatch):
    seen = {}

    class FakeWorker:
        def __init__(self, queues, connection) -> None:
            seen["queues"] = [q.name for q in queues]
            seen["connection"] = connection

        def work(self, with_scheduler: bool, burst: bool = False) -> None:
            seen["with_scheduler"] = with_scheduler
            seen["burst"] = burst

    monkeypatch.setattr(worker, "Worker", FakeWorker)
    worker.main()
    assert seen["queues"] == ["high", "default", "low"]
    assert seen["with_scheduler"] is True and seen["burst"] is False

    # E4.6: cada worker da stack consome só as suas filas.
    monkeypatch.setenv("WORKER_QUEUES", "content, low,default")
    worker.main()
    assert seen["queues"] == ["content", "low", "default"]
    monkeypatch.setenv("WORKER_QUEUES", "fast,high")
    worker.main()
    assert seen["queues"] == ["fast", "high"]
    for bad in ("fast,inexistente", " , "):
        monkeypatch.setenv("WORKER_QUEUES", bad)
        with pytest.raises(SystemExit):
            worker.main()
    # GOTCHAS #2: a fila usa a conexão sem decode_responses.
    assert seen["connection"] is worker.rq_redis
