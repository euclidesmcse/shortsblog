{{/* Nome base dos objetos: o nome do release (um blog por release). */}}
{{- define "sb.fullname" -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{- define "sb.labels" -}}
app.kubernetes.io/name: shortsblog
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* sb.selector: rótulos estáveis de um componente. Uso: include "sb.selector" (list . "api") */}}
{{- define "sb.selector" -}}
{{- $ctx := index . 0 -}}
app.kubernetes.io/name: shortsblog
app.kubernetes.io/instance: {{ $ctx.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{/* Rótulos de quem pode falar com os serviços internos (NetworkPolicy). */}}
{{- define "sb.engineClient" -}}
shortsblog.io/engine-client: "true"
{{- end -}}
{{- define "sb.wpClient" -}}
shortsblog.io/wp-client: "true"
{{- end -}}

{{- define "sb.tag" -}}
{{- .Values.image.tag | default (printf "v%s" .Chart.AppVersion) -}}
{{- end -}}
{{- define "sb.image.engine" -}}
{{ .Values.image.registry }}/{{ .Values.image.engine }}:{{ include "sb.tag" . }}
{{- end -}}
{{- define "sb.image.gateway" -}}
{{ .Values.image.registry }}/{{ .Values.image.gateway }}:{{ include "sb.tag" . }}
{{- end -}}
{{- define "sb.image.wpAssets" -}}
{{ .Values.image.registry }}/{{ .Values.image.wpAssets }}:{{ include "sb.tag" . }}
{{- end -}}

{{- define "sb.secretName" -}}
{{- .Values.existingSecret | default (printf "%s-secrets" (include "sb.fullname" .)) -}}
{{- end -}}

{{/* SB_WP_<SITE>_APP_*: o nome que o motor procura para a credencial do blog. */}}
{{- define "sb.siteEnv" -}}
{{- /* Hífen vira `_` (nome de variável válido), como o vault.wp_env_name do motor (R71). */ -}}
{{- .Values.site.key | upper | replace "-" "_" -}}
{{- end -}}

{{- define "sb.panelUrl" -}}
{{ .Values.scheme }}://{{ .Values.panel.host }}
{{- end -}}
{{- define "sb.siteUrl" -}}
{{ .Values.scheme }}://{{ .Values.site.host }}
{{- end -}}

{{/* Validação dos obrigatórios (falha o render com uma mensagem clara). */}}
{{- define "sb.validate" -}}
{{- if not .Values.panel.host }}{{ fail "defina panel.host (o nome do portal)" }}{{ end -}}
{{- if not .Values.site.host }}{{ fail "defina site.host (o nome do blog)" }}{{ end -}}
{{- if not .Values.admin.email }}{{ fail "defina admin.email" }}{{ end -}}
{{- if not (regexMatch "^[a-z][a-z0-9-]{1,30}$" .Values.site.key) }}{{ fail "site.key: minúsculas, dígitos e hífen (2 a 31)" }}{{ end -}}
{{- if not (has .Values.scheme (list "https" "http")) }}{{ fail "scheme: https ou http" }}{{ end -}}
{{- if not (has .Values.wpPublishMode (list "draft" "live")) }}{{ fail "wpPublishMode: draft ou live" }}{{ end -}}
{{- if or (contains "\"" .Values.site.name) (contains "@@" .Values.site.name) (contains "\"" .Values.site.tagline) (contains "@@" .Values.site.tagline) }}{{ fail "site.name e site.tagline não podem ter aspas duplas nem @@" }}{{ end -}}
{{- end -}}

{{/* Ambiente comum do motor (API, workers, agendador) — o x-app-env do compose. */}}
{{- define "sb.engineEnv" -}}
- name: ENVIRONMENT
  value: production
- name: TZ
  value: {{ .Values.timezone | quote }}
- name: SB_DB_APP_PASSWORD
  valueFrom: {secretKeyRef: {name: {{ include "sb.secretName" . }}, key: dbAppPassword}}
- name: DATABASE_URL
  value: "postgresql+psycopg://sb_app:$(SB_DB_APP_PASSWORD)@{{ include "sb.fullname" . }}-postgres:5432/sb"
- name: RUN_MIGRATIONS_ON_STARTUP
  value: "false"
- name: REDIS_URL
  value: "redis://{{ include "sb.fullname" . }}-redis:6379/0"
- name: SB_API_TOKEN
  valueFrom: {secretKeyRef: {name: {{ include "sb.secretName" . }}, key: apiToken}}
- name: SB_SECRET_KEY
  valueFrom: {secretKeyRef: {name: {{ include "sb.secretName" . }}, key: secretKey}}
- name: MEDIA_DIR
  value: /data/media
- name: INTERNAL_SCHEDULER_ENABLED
  value: "true"
- name: N8N_URL
  value: ""
- name: WP_PUBLISH_MODE
  value: {{ .Values.wpPublishMode | quote }}
- name: SEARXNG_URL
  value: "http://{{ include "sb.fullname" . }}-searxng:8080"
- name: AI_GATEWAY_URL
  value: "http://{{ include "sb.fullname" . }}-ai-gateway:8080"
- name: SB_HOST
  value: {{ .Values.panel.host | quote }}
- name: PUBLIC_BASE_URL
  value: {{ include "sb.panelUrl" . | quote }}
- name: PANEL_REQUIRE_ACCESS
  value: "false"
- name: PANEL_PASSWORD
  valueFrom: {secretKeyRef: {name: {{ include "sb.secretName" . }}, key: panelPassword}}
- name: APPROVAL_NOTIFY_EMAIL
  value: {{ .Values.admin.email | quote }}
- name: SB_WP_{{ include "sb.siteEnv" . }}_APP_USER
  value: sb-publisher
- name: SB_WP_{{ include "sb.siteEnv" . }}_APP_PASSWORD
  valueFrom: {secretKeyRef: {name: {{ include "sb.secretName" . }}, key: wpAppPassword}}
{{- end -}}

{{/* Espera até todas as migrações DESTA imagem estarem no banco (o Job de migração roda uma
     vez por revisão; o motor não tem trava para migrar em paralelo). */}}
{{- define "sb.waitMigrations" -}}
- name: wait-migrations
  image: {{ include "sb.image.engine" . }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command:
    - python
    - -c
    - |
      import pathlib, sys, time
      from sqlalchemy import text
      from app.core.config import settings
      from app.core.db import engine
      want = {p.name for p in pathlib.Path(settings.migrations_dir).glob("*.sql")}
      for i in range(400):
          try:
              with engine.connect() as c:
                  have = {r[0] for r in c.execute(text("SELECT filename FROM schema_migrations"))}
              if want <= have:
                  print(f"migrações em dia ({len(want)})"); sys.exit(0)
              print(f"faltam {len(want - have)} migração(ões)")
          except Exception as e:
              print("banco ainda indisponível:", str(e)[:100])
          time.sleep(3)
      sys.exit(1)
  env:
    {{- include "sb.engineEnv" . | nindent 4 }}
  securityContext:
    {{- include "sb.restricted" . | nindent 4 }}
  resources:
    {{- toYaml .Values.resources.migrate | nindent 4 }}
{{- end -}}

{{/* securityContext de container sem privilégios (motor, gateway, assets). */}}
{{- define "sb.restricted" -}}
allowPrivilegeEscalation: false
capabilities: {drop: [ALL]}
runAsNonRoot: true
seccompProfile: {type: RuntimeDefault}
{{- end -}}

{{/* Todos os pods do motor no mesmo nó quando o volume de mídia é ReadWriteOnce. */}}
{{- define "sb.mediaAffinity" -}}
{{- if eq .Values.persistence.mediaAccessMode "ReadWriteOnce" }}
affinity:
  podAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      - labelSelector:
          matchLabels:
            shortsblog.io/media: {{ .Release.Name | quote }}
        topologyKey: kubernetes.io/hostname
{{- end }}
{{- end -}}

{{- define "sb.scheduling" -}}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "sb.storageClass" -}}
{{- with .Values.persistence.storageClass }}
storageClassName: {{ . | quote }}
{{- end }}
{{- end -}}

{{/* Preenche os @@...@@ dos modelos de config (o mesmo `fill` do install.sh). */}}
{{- define "sb.fill" -}}
{{- $v := index . 0 -}}
{{- index . 1
    | replace "@@SITE_KEY@@" $v.site.key
    | replace "@@SITE_NAME@@" $v.site.name
    | replace "@@SITE_TAGLINE@@" $v.site.tagline
    | replace "@@SITE_HOST@@" $v.site.host
    | replace "@@TZ@@" $v.timezone -}}
{{- end -}}

{{/* Volume /config do motor: a árvore que o pacote monta de ./config. */}}
{{- define "sb.configVolume" -}}
- name: config
  configMap:
    name: {{ include "sb.fullname" . }}-config
    items:
      - {key: sites.yaml, path: "sites/{{ .Values.site.key }}.yaml"}
      - {key: policies-comum.md, path: policies/_comum.md}
      - {key: policies-site.md, path: "policies/{{ .Values.site.key }}.md"}
      - {key: feeds.yaml, path: "feeds/{{ .Values.site.key }}.yaml"}
      - {key: wordpress-site.json, path: "wordpress-sites/{{ .Values.site.key }}.json"}
      - {key: sem-leitura.yaml, path: sem-leitura.yaml}
      - {key: newsletters.yaml, path: newsletters.yaml}
{{- end -}}
