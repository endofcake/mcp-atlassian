{{/*
Expand the name of the chart.
*/}}
{{- define "mcp-atlassian.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "mcp-atlassian.fullname" -}}
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
Create chart name and version as used by the chart label.
*/}}
{{- define "mcp-atlassian.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "mcp-atlassian.labels" -}}
helm.sh/chart: {{ include "mcp-atlassian.chart" . }}
{{ include "mcp-atlassian.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "mcp-atlassian.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcp-atlassian.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "mcp-atlassian.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "mcp-atlassian.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Whether the OAuth token cache is persisted: persistence is enabled and either
the top-level auth mode or an enabled Bitbucket block uses oauth. Renders
"true" or nothing, for use in an if.
*/}}
{{- define "mcp-atlassian.persistOAuthTokens" -}}
{{- $bitbucket := .Values.bitbucket | default dict -}}
{{- $bitbucketOAuth := and $bitbucket.enabled (eq (toString $bitbucket.authMode | trim) "oauth") -}}
{{- if and .Values.persistence.enabled (or (eq .Values.authMode "oauth") $bitbucketOAuth) -}}true{{- end -}}
{{- end }}
