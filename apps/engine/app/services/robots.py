"""Leitor de robots.txt segundo a RFC 9309 (23/11).

O `urllib.robotparser` da biblioteca padrão não serve: ignora os curingas (`*` no meio do
caminho, `$` no fim) e aplica a PRIMEIRA regra que casa, não a mais específica. Com ele, o
`Disallow: /feed*/` da AEMPS deixava o validador aceitar o feed, e um `Allow` estreito antes
de um `Disallow` largo dava o resultado oposto ao do Google.

Regras (RFC 9309):
- grupos: linhas `User-agent` seguidas abrem um grupo; as `Allow`/`Disallow` seguintes são
  dele. Vários grupos do mesmo robô juntam-se. Sem grupo do robô, vale o `*`; sem nenhum,
  tudo é permitido;
- o robô casa pelo nome do produto (a parte antes de `/`), sem distinguir maiúsculas;
- vence a regra mais longa que casa; empate entre `Allow` e `Disallow`: vence o `Allow`;
- `*` casa qualquer sequência; `$` no fim prende ao fim do endereço; `Disallow:` vazio não
  proíbe nada; o próprio `/robots.txt` é sempre permitido.

Só a biblioteca padrão: `scripts/validate_feeds.py` carrega este ficheiro diretamente.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

_SAFE = "/?&=:@!$,;+*~-._()'%"


def _product(agent: str) -> str:
    """`ShortsBlogBot/1.0 (+https://…)` → `shortsblogbot`."""
    return agent.strip().split("/", 1)[0].split(None, 1)[0].lower() if agent.strip() else ""


def _norm(value: str) -> str:
    """Mesma forma para o endereço e para a regra: descodifica e volta a codificar."""
    return quote(unquote(value), safe=_SAFE)


@dataclass(frozen=True)
class _Rule:
    allow: bool
    length: int
    pattern: re.Pattern[str]


def _compile(path: str, allow: bool) -> _Rule:
    anchored = path.endswith("$")
    body = _norm(path[:-1] if anchored else path)
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return _Rule(allow, len(path), re.compile(regex + ("$" if anchored else ""), re.DOTALL))


@dataclass
class Robots:
    """Regras de um robots.txt. `Robots.parse(linhas)`; `can_fetch(robô, endereço)`."""

    groups: dict[str, list[_Rule]] = field(default_factory=dict)

    @classmethod
    def parse(cls, lines: list[str] | tuple[str, ...]) -> Robots:
        robots = cls()
        agents: list[str] = []
        in_rules = False
        for raw in lines:
            line = raw.split("#", 1)[0].strip().lstrip("﻿")
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip().lower(), value.strip()
            if key in ("user-agent", "useragent"):
                if in_rules:                     # um User-agent depois de regras: grupo novo
                    agents, in_rules = [], False
                token = "*" if value == "*" else _product(value)
                if token:
                    agents.append(token)
                    robots.groups.setdefault(token, [])
            elif key in ("allow", "disallow"):
                in_rules = True
                if not agents or (key == "disallow" and not value):
                    continue                     # regra sem grupo, ou `Disallow:` vazio
                if key == "allow" and not value:
                    continue
                rule = _compile(value, key == "allow")
                for agent in agents:
                    robots.groups[agent].append(rule)
        return robots

    def can_fetch(self, agent: str, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if path == "/robots.txt":
            return True
        target = _norm(path + (f"?{parts.query}" if parts.query else ""))
        rules = self.groups.get(_product(agent))
        if rules is None:
            rules = self.groups.get("*", [])
        best: _Rule | None = None
        for rule in rules:
            if rule.pattern.match(target) and (
                    best is None or rule.length > best.length
                    or (rule.length == best.length and rule.allow and not best.allow)):
                best = rule
        return best is None or best.allow


DISALLOW_ALL = Robots.parse(["User-agent: *", "Disallow: /"])
