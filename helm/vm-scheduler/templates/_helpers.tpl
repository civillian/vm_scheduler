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

{{- define "vm-scheduler.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "vm-scheduler.labels" -}}
helm.sh/chart: {{ include "vm-scheduler.chart" . }}
{{ include "vm-scheduler.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "vm-scheduler.selectorLabels" -}}
app.kubernetes.io/name: {{ include "vm-scheduler.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "vm-scheduler.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "vm-scheduler.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Database host — bundled Postgres or external
*/}}
{{- define "vm-scheduler.dbHost" -}}
{{- if .Values.postgresql.enabled }}
{{- printf "%s-postgresql" (include "vm-scheduler.fullname" .) }}
{{- else }}
{{- .Values.externalDatabase.host }}
{{- end }}
{{- end }}

{{/*
Security context — empty for OpenShift (SCC assigns random UID),
explicit non-root uid 1000 for AKS.
*/}}
{{- define "vm-scheduler.securityContext" -}}
{{- if not .Values.openshift.enabled }}
securityContext:
  {{- toYaml .Values.securityContext | nindent 2 }}
{{- end }}
{{- end }}

{{/*
Common environment variables shared across api, worker, beat, and flower.
Redis is configured via component parts (host/port/password/db) rather than
a full URL, so that shared Redis instances can use specific databases without
embedding passwords in values files.
*/}}
{{- define "vm-scheduler.commonEnv" -}}
- name: DB_HOST
  value: {{ include "vm-scheduler.dbHost" . | quote }}
- name: DB_PORT
  value: {{ if .Values.postgresql.enabled }}"5432"{{ else }}{{ .Values.externalDatabase.port | default 5432 | quote }}{{ end }}
- name: DB_USER
  value: {{ if .Values.postgresql.enabled }}{{ .Values.postgresql.auth.username | quote }}{{ else }}{{ .Values.externalDatabase.username | quote }}{{ end }}
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "vm-scheduler.fullname" . }}-db
      key: password
- name: DB_NAME
  value: {{ if .Values.postgresql.enabled }}{{ .Values.postgresql.auth.database | quote }}{{ else }}{{ .Values.externalDatabase.database | quote }}{{ end }}
- name: REDIS_HOST
  value: {{ .Values.externalRedis.host | default (printf "%s-redis-master" (include "vm-scheduler.fullname" .)) | quote }}
- name: REDIS_PORT
  value: {{ .Values.externalRedis.port | default 6379 | quote }}
{{- if .Values.externalRedis.password }}
- name: REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "vm-scheduler.fullname" . }}-redis
      key: password
{{- end }}
- name: REDIS_BROKER_DB
  value: {{ .Values.externalRedis.brokerDb | default 0 | quote }}
- name: REDIS_RESULT_DB
  value: {{ .Values.externalRedis.resultDb | default 1 | quote }}
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
  value: {{ .Values.vault.awsCredPath | quote }}
- name: VAULT_VCENTER_CRED_PATH
  value: {{ .Values.vault.vcenterCredPath | quote }}
- name: VAULT_AZURE_MOUNT
  value: {{ .Values.vault.azureMount | default "azure" | quote }}
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
