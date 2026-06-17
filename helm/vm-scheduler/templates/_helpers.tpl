{{/*
Expand the name of the chart.
*/}}
{{- define "vm-scheduler.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "vm-scheduler.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart label
*/}}
{{- define "vm-scheduler.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "vm-scheduler.labels" -}}
helm.sh/chart: {{ include "vm-scheduler.chart" . }}
{{ include "vm-scheduler.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "vm-scheduler.selectorLabels" -}}
app.kubernetes.io/name: {{ include "vm-scheduler.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service account name
*/}}
{{- define "vm-scheduler.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "vm-scheduler.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Database URL — uses bundled Postgres if enabled, otherwise externalDatabase
*/}}
{{- define "vm-scheduler.databaseUrl" -}}
{{- if .Values.postgresql.enabled }}
{{- printf "postgresql://%s:%s@%s-postgresql:5432/%s" .Values.postgresql.auth.username .Values.postgresql.auth.password (include "vm-scheduler.fullname" .) .Values.postgresql.auth.database }}
{{- else }}
{{- printf "postgresql://%s:%s@%s:%d/%s" .Values.externalDatabase.username .Values.externalDatabase.password .Values.externalDatabase.host (.Values.externalDatabase.port | int) .Values.externalDatabase.database }}
{{- end }}
{{- end }}

{{/*
Redis URL — uses bundled Redis if enabled, otherwise externalRedis
*/}}
{{- define "vm-scheduler.redisUrl" -}}
{{- if .Values.redis.enabled }}
{{- printf "redis://%s-redis-master:6379/0" (include "vm-scheduler.fullname" .) }}
{{- else }}
{{- printf "redis://%s:%d/0" .Values.externalRedis.host (.Values.externalRedis.port | int) }}
{{- end }}
{{- end }}

{{/*
Security context — empty for OpenShift (random UID assigned by SCC),
explicit non-root for AKS.
*/}}
{{- define "vm-scheduler.securityContext" -}}
{{- if not .Values.openshift.enabled }}
securityContext:
  {{- toYaml .Values.securityContext | nindent 2 }}
{{- end }}
{{- end }}

{{/*
Common environment variables shared across api, worker, and beat.
*/}}
{{- define "vm-scheduler.commonEnv" -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "vm-scheduler.fullname" . }}-db
      key: url
- name: CELERY_BROKER_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "vm-scheduler.fullname" . }}-redis
      key: url
- name: VAULT_ADDR
  valueFrom:
    secretKeyRef:
      name: {{ include "vm-scheduler.fullname" . }}-vault
      key: addr
- name: VAULT_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "vm-scheduler.fullname" . }}-vault
      key: token
- name: VAULT_NAMESPACE
  valueFrom:
    secretKeyRef:
      name: {{ include "vm-scheduler.fullname" . }}-vault
      key: namespace
- name: VAULT_AWS_CRED_PATH
  valueFrom:
    secretKeyRef:
      name: {{ include "vm-scheduler.fullname" . }}-vault
      key: awsCredPath
- name: VAULT_VCENTER_CRED_PATH
  valueFrom:
    secretKeyRef:
      name: {{ include "vm-scheduler.fullname" . }}-vault
      key: vcenterCredPath
{{- if .Values.proxy.httpProxy }}
- name: HTTP_PROXY
  value: {{ .Values.proxy.httpProxy | quote }}
- name: http_proxy
  value: {{ .Values.proxy.httpProxy | quote }}
{{- end }}
{{- if .Values.proxy.httpsProxy }}
- name: HTTPS_PROXY
  value: {{ .Values.proxy.httpsProxy | quote }}
- name: https_proxy
  value: {{ .Values.proxy.httpsProxy | quote }}
{{- end }}
{{- if .Values.proxy.noProxy }}
- name: NO_PROXY
  value: {{ .Values.proxy.noProxy | quote }}
- name: no_proxy
  value: {{ .Values.proxy.noProxy | quote }}
{{- end }}
{{- if .Values.proxy.requestsCaBundle }}
- name: REQUESTS_CA_BUNDLE
  value: {{ .Values.proxy.requestsCaBundle | quote }}
{{- end }}
{{- if .Values.proxy.awsCaBundle }}
- name: AWS_CA_BUNDLE
  value: {{ .Values.proxy.awsCaBundle | quote }}
{{- end }}
{{- end }}
