"""Configuração central — tudo vem do ambiente, nada é hardcoded.

Uma única instância (`settings`) é importada por todo o projeto.
Campos vazios desligam a funcionalidade correspondente em vez de quebrar:
o motor precisa continuar rodando mesmo com integrações parcialmente
configuradas.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Identidade / rede ---
    sb_host: str = "painel.shortsblog.com"
    public_base_url: str = "https://painel.shortsblog.com"
    environment: str = "production"
    log_level: str = "INFO"
    tz: str = "America/Sao_Paulo"
    # E19.6: fusos em que o portal mostra os horários, `fuso[=rótulo]` separados por vírgula
    # (o 1.º com a data). Vazio = só o fuso da instalação (`TZ`; o pacote usa o SB_TZ). A nossa
    # produção: "Europe/Lisbon=Lisboa,America/Sao_Paulo=SP" (slots em Lisboa, D-066).
    panel_timezones: str = ""

    # --- Infra ---
    database_url: str = "postgresql+psycopg://sb_app:sb_app@sb-engine-postgres:5432/sb"
    redis_url: str = "redis://sb-engine-redis:6379/0"

    # --- Segurança ---
    sb_api_token: str = ""
    sb_secret_key: str = "insecure-dev-key"

    # --- Painel (D-018: Cloudflare Access + login próprio) ---
    # Pedidos que chegam pelo domínio público (`sb_host`) só passam com um JWT
    # do Cloudflare Access válido: protege contra quem chame a origem pelo IP.
    cf_access_team_domain: str = ""      # ex.: misty-snow-8814.cloudflareaccess.com
    # --- Purge do cache do Cloudflare ao publicar (D-097; automatiza a D-048) ---
    # Token PRÓPRIO (Zone→Zone→Read + Zone→Cache Purge→Purge, só a zona), nunca o de DNS.
    # Vazio = purge desligado (a página nova aparece quando o cache de 1 h expira).
    cloudflare_purge_token: str = ""
    cloudflare_zone: str = "shortsblog.com"
    cloudflare_zone_id: str = ""         # opcional: dispensa a consulta por nome
    cf_access_aud: str = ""              # Application Audience (AUD) Tag
    panel_allowed_emails: str = ""       # separados por vírgula
    panel_password: str = ""             # 2.ª camada; vazio = login desativado
    panel_session_hours: int = 12
    # E18.1 (D-104): pelo domínio público exige o JWT do Cloudflare Access. Na nossa instalação
    # fica ligado (defesa em profundidade); o pacote da E19, sem Access, desliga.
    panel_require_access: bool = True
    panel_login_max_failures: int = 5      # erros seguidos até bloquear o usuário
    panel_login_lock_minutes: int = 15
    panel_login_ip_limit: int = 20         # tentativas por IP na janela de bloqueio
    # R69/E19: proxies cujo `X-Forwarded-For`/`X-Forwarded-Proto` o login aceita (CIDRs separados
    # por vírgula). Vazio = nenhum (o IP é o do par TCP). O pacote, cujo motor só é alcançável
    # pelo Caddy, e o Helm chart (ingress) usam as faixas privadas. A nossa produção usa o
    # `CF-Connecting-IP` do Access e deixa vazio.
    trusted_proxies: str = ""
    # R52: alerta por e-mail se o último backup bem-sucedido (pulsação) passar disto.
    backup_alert_hours: int = 26
    # R31: teto de cada volume de estado das CLIs (o gateway limpa o velho todo dia; passar
    # disto é sinal de que a limpeza não está a dar conta ou de que a CLI mudou de pastas).
    ai_storage_alert_mb: int = 500

    # --- Marca do painel ---
    brand_name: str = "ShortsBlog"
    # Azul do tema dos blogs (par claro/escuro do `cloud` em SB_ACCENTS); antes eram
    # as cores do fork do Growth Engine (#0B3D91/#00C2FF) — pacote visual da revisão 360°.
    brand_primary_color: str = "#2457d6"
    brand_accent_color: str = "#8fb0ff"

    # --- WordPress ---
    wordpress_url: str = "http://wp-app"
    # E9: `draft` cria só rascunhos (até ao piloto da E14); `live` publica de verdade.
    wp_publish_mode: str = "draft"

    # --- Governança ---
    require_human_approval: bool = True
    # Para onde vão os avisos ao operador (alertas, vigia, resumos).
    approval_notify_email: str = ""
    dry_run: bool = False

    # --- E-mail ao operador (Brevo API ou SMTP) ---
    mail_from_email: str = ""
    mail_from_name: str = "ShortsBlog"
    brevo_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # --- Newsletter (E17, D-062) ---
    # Desligada até haver política de privacidade publicada (E17.6): sem ela, o cadastro responde
    # «fechado» e o envio diário não manda nada.
    newsletter_enabled: bool = False
    # Token só da newsletter: o WordPress usa-o para falar com o motor (não é o SB_API_TOKEN
    # do n8n).
    newsletter_api_token: str = ""
    # Segredo que o Brevo põe no endereço do webhook de bounces e reclamações.
    newsletter_webhook_token: str = ""
    # --- Pauta por e-mail (D-094): newsletters de fabricantes viram ideias «só pauta» ---
    # Segredo próprio do webhook de entrada (vazio = rota desligada, responde 503).
    pauta_webhook_token: str = ""
    pauta_max_items: int = 30               # itens por e-mail, o mesmo teto dos feeds
    # authserv-id do Authentication-Results que o recebedor (Brevo) acrescenta ao e-mail —
    # conferir no 1.º e-mail real e preencher. Vazio = toda pauta por e-mail é recusada (R62).
    pauta_authserv_id: str = ""
    newsletter_from_email: str = ""         # vazio: MAIL_FROM_EMAIL
    newsletter_from_name: str = "ShortsBlog"
    newsletter_consent_version: str = "2026-10-21"
    newsletter_confirm_hours: int = 48
    newsletter_daily_limit: int = 300       # plano gratuito do Brevo (D-OPEN-12)
    newsletter_batch_size: int = 50         # e-mails seguidos antes de uma pausa
    newsletter_batch_pause_s: float = 2.0
    # A edição leva o que saiu desde a edição anterior (aprovado tarde não fica de fora), mas
    # nunca mais do que estas horas para trás (newsletter parada dias não vira uma edição gigante).
    newsletter_catchup_hours: int = 72
    # 23/11: a edição das 07:00 saiu com 1 post porque os 6 do dia foram aprovados às 07:01.
    # Com post do dia ainda à espera de aprovação, a edição espera até esta hora local (vazio
    # desliga); sai assim que a fila do dia esvaziar (verificado a cada `publish-due`).
    newsletter_hold_until: str = "10:00"
    newsletter_rate_ip_hour: int = 5        # pedidos de cadastro por IP e hora
    newsletter_rate_email_day: int = 3      # e-mails de confirmação por endereço e dia

    # --- HTTP de saída ---
    content_locale: str = "pt-BR"
    http_user_agent: str = "ShortsBlogBot/1.0 (+https://shortsblog.com)"
    http_timeout: int = 30
    http_max_rps: float = 1.0
    # D-070: ao ler uma página para a IA, o robots.txt vale também para os robôs de IA a quem
    # o texto é entregue (Claude na pesquisa e na redação, Codex na revisão).
    research_ai_agents: tuple[str, ...] = ("ClaudeBot", "Claude-User", "anthropic-ai",
                                           "GPTBot", "ChatGPT-User")

    # --- Agendamento (E11, E19.2) ---
    # true: o `sb-engine-scheduler` dispara os horários `/api/sched/*` sozinho (o pacote).
    # false: o n8n dispara (a nossa produção). Nunca os dois ao mesmo tempo.
    internal_scheduler_enabled: bool = True
    # Endereço do n8n citado no e-mail do vigia (vazio = não citar).
    n8n_url: str = ""

    # --- AI Gateway (E3) ---
    # Container `sb-ai-gateway`, na rede interna `sb_internal`.
    ai_gateway_url: str = "http://sb-ai-gateway:8080"
    ai_timeout_s: float = 300
    # E3.6: verificação de saúde dos provedores, corrida pelo scheduler.
    ai_probe_interval_s: int = 300
    # Depois de uma chamada real falhar por cota ou autenticação, minutos até o
    # probe barato poder dar o provedor outra vez como bom.
    ai_cooldown_min: int = 30

    # --- Multi-site (E4) ---
    # Antecedência da produção face ao horário do slot (D-033; D-066: 14 h, para o post das
    # 07:00 de Lisboa ficar pronto à tarde da véspera e ser aprovado à noite).
    production_lead_hours: int = 14
    # Horários-base dos slots na hora local do site (spec §5.2; D-066). E18.3b: o cofre
    # (`slot_times`, tela Sistema) tem precedência; este é o valor de reserva.
    slot_times: str = "07:00,10:30,12:30,15:30,18:30"
    # Dispatcher (E4.5): jobs em produção ao mesmo tempo, em todos os sites. Alinhado com
    # o número de workers `content` (1): um segundo job ficaria na fila com a lease a contar.
    max_active_jobs: int = 1
    # Lease de um job entregue a um worker; o worker renova-a enquanto trabalha.
    job_lease_minutes: int = 20
    job_max_attempts: int = 3
    # Espera antes de nova tentativa: este valor × número de tentativas já feitas.
    job_retry_minutes: int = 10
    # Sem provedor de IA para produzir: espera a dobrar (30 min, 1 h, 2 h…) até este teto (R27).
    provider_retry_cap_minutes: int = 240
    # Orçamento de chamadas de IA por job por dia (revisão 360° de 5/11; 3.ª ocorrência do
    # padrão «um job esgota a cota do dia»: o 174 gastou 36 chamadas em 3/11 e o 240 girou
    # 3 ciclos em 10/11). No teto, o job espera (RETRY_WAIT com a espera do R27) em vez de
    # queimar o resto da cota dos outros slots. 0 desliga.
    llm_job_cap: int = 0
    # Um slot que passou há mais do que isto sem publicar é dado como expirado.
    job_expire_hours: int = 24
    # Slot que passou há mais do que isto e ainda nem foi produzido (PLANNED, QUEUED ou
    # RETRY_WAIT sem aprovação) expira: produzir depois disso dá um post de ontem a sair junto
    # com o de hoje (21/11: o 372 voltaria a produzir à meia-noite o slot das 04:21 da véspera).
    late_production_hours: int = 6
    # Aprovado depois do dia dele (o job 372): não sai junto com o do dia; cobre o próximo slot
    # que falhar, até `late_hold_hours` depois do slot original (notícia envelhece).
    late_hold_hours: int = 48
    approval_expire_hours: int = 72   # sem aprovação 72 h depois do slot → EXPIRED (E10.4)
    # Pesquisa sem ideia viável (fontes insuficientes ou fila vazia): nova tentativa mais tarde,
    # quando os feeds já trouxeram ideias novas, se ainda couber antes do slot (E14.5l, job 43).
    source_retry_hours: int = 3
    source_retries: int = 1

    # --- Conteúdo externo (E6) ---
    # Fetcher seguro: tamanho e tempo máximos por pedido.
    fetch_max_bytes: int = 3 * 1024 * 1024
    # Teto próprio para PDF (5/11): o reforço da Saúde perdia as fontes oficiais (Fiocruz,
    # ILO, universidades) por `too_large` e por serem PDF; relatórios são maiores que uma
    # página HTML. Só vale quando o chamador pede (read_sources). 0 = sem teto especial.
    fetch_max_bytes_pdf: int = 10 * 1024 * 1024
    fetch_timeout_s: float = 30
    # R42: intervalo mínimo entre dois pedidos ao MESMO servidor (por processo). 0 desliga.
    fetch_host_interval_s: float = 1.0
    # Descoberta: itens lidos por feed, idade máxima de uma notícia e validade de uma ideia.
    feed_max_items: int = 30
    # Teto de ideias NOVAS (não repetidas) por feed em 24 h (24/11): Infobae, Exame,
    # InfoMoney e Poder360 mandavam 100–120 por dia, quase todas com nota < 5, e a triagem
    # gastava cota a lê-las. Ficam as mais recentes. 0 desliga.
    feed_max_ideas_day: int = 40
    # Resumo diário (feed_health, 22/11): fonte com tantas leituras seguidas com erro (a
    # descoberta corre de hora em hora) ou sem pauta nova há tantos dias aparece para olhar.
    feed_fail_alert_runs: int = 24
    feed_silent_days: int = 14
    # «Fontes novas» (23/11): feeds ligados há até tantos dias, com o que já trouxeram.
    feed_new_days: int = 14
    # Resumo diário (read_failures): sítios com pelo menos tantas falhas de leitura na semana.
    read_fail_alert_min: int = 5
    read_fail_alert_top: int = 15
    idea_max_age_days: int = 3
    idea_ttl_days: int = 4
    # Janela da deduplicação (por blog ao guardar; global ao escolher).
    idea_window_days: int = 7
    # Pipeline de um artigo (E6.3–E6.8).
    source_max_chars: int = 6000          # texto de cada fonte entregue ao modelo
    max_sources: int = 4                  # artigo-fonte + até 3 de corroboração
    # Corroboração entre línguas (E14.5g): ligações de outras línguas por ideia, e a partir de
    # quantos títulos da janela uma entidade (Google, Microsoft…) é comum demais para contar.
    corroboration_cross_max: int = 3
    corroboration_common: int = 6
    idea_attempts: int = 10               # ideias antes de bloquear (eram 5; job 43, E14.5l)
    # Reforço de pesquisa por busca na web (D-063). Desligado quando `searxng_url` está vazio.
    searxng_url: str = ""
    searxng_timeout_s: float = 15
    searxng_daily_cap: int = 30           # consultas por dia (cada consulta ao SearXNG conta uma)
    # Orçamento por job (4/11): um job sozinho não pode esgotar o teto do dia — o requeue do
    # 174 gastou as 60 consultas em 10 min e todos os jobs de 5/11 rodaram `capped`. 0 desliga.
    searxng_job_cap: int = 20             # consultas por job por dia
    # Parte do teto do dia guardada para a pauta de resgate/reserva (23/11: o teto acabou às
    # 17:33 no 5.º blog; o requeue da #404 e as 5 pautas de resgate rodaram com 0 consultas e
    # o job bloqueou). Limitada a 1/4 do teto, para não matar a fila normal com tetos baixos.
    searxng_rescue_reserve: int = 15
    searxng_cache_hours: int = 6
    searxng_max_results: int = 8          # ligações de reforço por ideia
    recipe_search_results: int = 12       # modo receita: mais candidatas (muitas não têm receita)
    searxng_max_age_days: int = 7         # página de reforço com data mais velha é ignorada
    triage_batch: int = 80                # títulos avaliados por chamada de triagem
    # A triagem corre ~1 vez por dia por blog; Política recebe ~640 títulos/dia e um feed
    # nacional (805 em 4 dias, nota média 1,1) enchia o lote dos «40 mais novos» (22/11).
    # O lote reveza as fontes (o mais novo de cada uma, depois o 2.º…), e a fonte de rendimento
    # baixo — média abaixo de `triage_source_low_avg` em pelo menos `triage_source_min_scored`
    # notas dos últimos `triage_yield_days` — só entra a cada `triage_source_penalty` voltas.
    triage_source_low_avg: float = 3.0
    triage_source_min_scored: int = 20
    triage_source_penalty: int = 4
    triage_yield_days: int = 14
    triage_min_relevance: int = 5         # abaixo disto a ideia é descartada
    triage_cache_minutes: int = 60        # P3 do ensaio E14: reentrada não re-triagem (0: sempre)
    triage_group_max: int = 5             # ideias por história agrupada pela triagem
    triage_group_overlap: int = 2         # palavras do título em comum com a líder
    cited_links_per_page: int = 8         # ligações citadas que se guardam de cada fonte
    cited_links_tried: int = 4            # quantas dessas se tentam ler como corroboração
    ms_article_min_words: int = 400
    # Modo receita (D-080): palavras mínimas, tolerância de temperatura e cópia máxima dos passos.
    recipe_min_words: int = 300
    recipe_temp_tolerance: int = 40
    recipe_copy_ratio: float = 0.2
    # Fonte com mais ingredientes ou passos que isto traz dados fora de escala (várias receitas).
    recipe_max_ingredients: int = 30
    recipe_max_steps: int = 30
    # O tempo total tem de cobrir esta fração da soma dos tempos citados nos passos.
    recipe_total_steps_ratio: float = 0.6
    # Ajuste C: receita aprovada com reparos menores ganha uma passada de polimento (~2 chamadas);
    # a versão aprovada fica de reserva e volta se o polimento tropeçar num gate ou na re-revisão.
    recipe_polish: bool = True
    review_min_score: int = 70
    review_max_rewrites: int = 2          # reescritas com o parecer antes de bloquear
    writer_decline_themes: int = 3        # temas recusáveis pelo redator antes de bloquear (D-085)
    review_exhausted_themes: int = 2      # pautas esgotáveis pela revisão antes de bloquear (D-093)
    translation_exhausted_themes: int = 2  # pautas com tradução sem saída antes de bloquear (D-100)
    duplicate_window_days: int = 30
    # Pauta de resgate (D-101): quando o slot ia bloquear (sem pauta viável ou tetos acima
    # esgotados), UMA chamada propõe temas intemporais e a busca procura-lhes fontes.
    slot_rescue: bool = True
    rescue_topics: int = 5                # temas propostos por resgate; 1 resgate por job
    # Post de reserva (D-115): cada blog guarda um artigo intemporal já aprovado; se o post
    # do dia não ficar pronto a tempo, a reserva publica no horário do slot e outra é feita.
    # A reserva começa a ser produzida a `reserve_hour` (hora local da instalação, longe da
    # produção dos slots, que corre na tarde e na noite da véspera) e vale
    # `reserve_valid_days` depois de criada; vencida, é trocada por outra.
    reserve_posts: bool = True
    reserve_hour: int = 6
    reserve_valid_days: int = 30
    # D-110 — cobertura mundial, regra fixa de TODOS os blogs (inclusive os que forem criados):
    # na janela, por blog, no máximo `world_brazil_max` posts centrados no Brasil e
    # `world_country_max` centrados num mesmo país qualquer. Não é configuração por blog.
    world_window_days: int = 7
    world_brazil_max: int = 1
    world_country_max: int = 2

    # --- Ilustração dos artigos (E7) ---
    illustration_enabled: bool = True
    illustration_min_width: int = 1000
    illustration_max_bytes: int = 12 * 1024 * 1024
    # Capa por IA (D-056): o Codex gera a imagem no gateway, com a assinatura do ChatGPT.
    ai_image_provider: str = "codex"
    ai_image_timeout_s: float = 240

    # --- Caminhos ---
    media_dir: str = "/data/media"
    migrations_dir: str = "/app/db/migrations"
    # content-config/sites do repositório, montado só-leitura (E4.2).
    sites_config_dir: str = "/config/sites"
    newsletters_config_file: str = "/config/newsletters.yaml"   # pauta por e-mail (D-094)
    # content-config/feeds: os feeds validados na E5 (E6.1).
    feeds_config_dir: str = "/config/feeds"
    # content-config/policies: regras editoriais comuns e por blog (E6.4).
    policies_config_dir: str = "/config/policies"
    # content-config/sem-leitura.yaml: sítios cujos termos proíbem robôs ou IA (D-070).
    no_fetch_config: str = "/config/sem-leitura.yaml"
    # Em produção as migrações correm no serviço `sb-engine-migrate` (admin) e a
    # API liga-se como `sb_app`, sem privilégio de DDL (E2.8).
    run_migrations_on_startup: bool = True

    # ------------------------------------------------------------------
    # Derivados
    # ------------------------------------------------------------------
    @property
    def allowed_panel_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.panel_allowed_emails.split(",") if e.strip()}



@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
