#!/usr/bin/env bash
# Teste de isolamento do container sb-ai-gateway (E3.2, R26).
# Corre no host contra o container em execução; não altera nada fora dos tmpfs.
# Uso: apps/ai-gateway/tests/isolation.sh [nome-do-container]
set -uo pipefail
C="${1:-sb-ai-gateway}"
fail=0

ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FALHA %s\n' "$1"; fail=1; }
# check "descrição" comando…  → passa se o comando DENTRO do container sair com 0
check() { local d="$1"; shift; if docker exec "$C" "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d"; fi; }
# deny  "descrição" comando…  → passa se o comando DENTRO do container falhar
deny()  { local d="$1"; shift; if docker exec "$C" "$@" >/dev/null 2>&1; then bad "$d"; else ok "$d"; fi; }
# host "descrição" valor esperado
host() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (obtido: $2, esperado: $3)"; fi; }

echo "== Identidade e privilégios"
host "uid 10001"                 "$(docker exec "$C" id -u)" "10001"
host "sem capabilities efetivas" "$(docker exec "$C" sh -c "grep CapEff /proc/self/status | awk '{print \$2}'")" "0000000000000000"
host "no-new-privileges"         "$(docker exec "$C" sh -c "grep NoNewPrivs /proc/self/status | awk '{print \$2}'")" "1"
host "não privilegiado"          "$(docker inspect -f '{{.HostConfig.Privileged}}' "$C")" "false"

echo "== Sistema de ficheiros"
host "raiz só-leitura"           "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$C")" "true"
deny  "não escreve em /app"        sh -c 'touch /app/x'
deny  "não escreve em /opt/clis"   sh -c 'touch /opt/clis/bin/x'
deny  "não escreve em /home/sb"    sh -c 'touch /home/sb/x'
check "escreve em /work (tmpfs)"   sh -c 'touch /work/x && rm /work/x'
check "escreve em /state/claude"   sh -c 'touch /state/claude/.w && rm /state/claude/.w'
check "escreve em /state/codex"    sh -c 'touch /state/codex/.w && rm /state/codex/.w'
check "escreve em ~/.gemini"       sh -c 'touch /home/sb/.gemini/.w && rm /home/sb/.gemini/.w'

echo "== Host fora de alcance"
deny  "sem docker.sock"            test -e /var/run/docker.sock
deny  "sem /home/ubuntu"           test -e /home/ubuntu
host "nenhum bind mount do host" "$(docker inspect -f '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}} {{end}}{{end}}' "$C")" ""
host "só volumes sb_ai_*"        "$(docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}' "$C" | tr ' ' '\n' | grep -v '^$' | sort | tr '\n' ' ')" "sb_ai_agy sb_ai_claude sb_ai_codex "

echo "== Rede"
host "redes"                     "$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$C")" "sb_ai_egress sb_internal "
host "sem portas publicadas"     "$(docker inspect -f '{{len .HostConfig.PortBindings}}' "$C")" "0"
# Sem -f: basta completar o TLS e receber resposta HTTP (a raiz devolve 404).
check "saída HTTPS"                curl -sS -o /dev/null --max-time 10 https://api.anthropic.com/
deny  "sem credenciais de API"     sh -c 'env | grep -Eq "^(ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|OPENAI_API_KEY)="'

echo "== CLIs (utilizador sem privilégios, FS só-leitura)"
check "claude --version"           claude --version
check "codex --version"            codex --version
check "agy --version"              agy --version
check "API /health/live"           curl -fsS http://127.0.0.1:8080/health/live

echo "== Sandbox do Codex (barreira das ferramentas locais; não chama modelos)"
# O Codex anuncia sempre a ferramenta `exec` ao modelo. O que impede qualquer comando
# de arrancar é o bwrap não conseguir criar namespaces neste container. Se um dia isto
# passar a funcionar (outro perfil seccomp, outra versão do Docker), o modelo ganha shell.
deny  "codex sandbox não executa"   sh -c 'cd /work && codex sandbox -- /usr/bin/id'

echo
if [ "$fail" -eq 0 ]; then echo "isolation: OK"; else echo "isolation: FALHOU"; fi
exit "$fail"
