{{/*
========================================================================
catalog/_template/templates/_helpers.tpl

THE CONTRACT. Every chart in catalog/ ships this file verbatim. When
this file changes here in _template, the change must be copied into
every existing chart — there is no symlink magic because Helm doesn't
support cross-chart includes.

If you find yourself editing this file inside a real chart, stop and
edit it here instead, then propagate.
========================================================================
*/}}

{{/*
catalog.name — chart name, overridable via .Values.nameOverride.
Capped at 63 chars per K8s label-value rules.
*/}}
{{- define "catalog.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{/*
catalog.fullname — fully qualified app name. Uses .Release.Name so the
same chart deployed to multiple namespaces (chat-sandbox / chat-dev /
chat-prod) doesn't collide on resource names.
*/}}
{{- define "catalog.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
catalog.chart — chart name + version, for the standard helm.sh/chart label.
*/}}
{{- define "catalog.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{/*
catalog.labels — the full label set every Kubernetes object in this
chart MUST emit. Combines:

  1. Standard Helm/Kubernetes recommended labels (app.kubernetes.io/*)
  2. The four chart-level catalog labels read from .Chart.Annotations
  3. The two release-level catalog labels read from .Values.catalog

Use as:
    metadata:
      labels:
        {{- include "catalog.labels" . | nindent 4 }}
*/}}
{{- define "catalog.labels" -}}
helm.sh/chart: {{ include "catalog.chart" . }}
{{ include "catalog.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{ include "catalog.homelabLabels" . }}
{{- end }}

{{/*
catalog.selectorLabels — the stable subset used by Service selectors
and Deployment matchLabels. MUST NOT include the catalog.homelab/*
labels because those (especially tier and data-class) can change per
deployment and would break selectors mid-flight.
*/}}
{{- define "catalog.selectorLabels" -}}
app.kubernetes.io/name: {{ include "catalog.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
catalog.homelabLabels — the six homelab catalog labels.

Chart-level (from Chart.yaml.annotations, fixed per chart):
  catalog.homelab/needs-sso
  catalog.homelab/llm-traffic
  catalog.homelab/wants-vector
  catalog.homelab/exposes-mcp

Release-level (from values.yaml, overridable per deployment):
  catalog.homelab/tier
  catalog.homelab/data-class

Defaults match the platform-wide defaults in catalog/README.md. If a
required value is missing the helper falls back to the safe default —
NEVER silently emits an empty value, because empty label values are
indistinguishable from "false" to downstream selectors.
*/}}
{{- define "catalog.homelabLabels" -}}
catalog.homelab/needs-sso: {{ index .Chart.Annotations "catalog.homelab/needs-sso" | default "false" | quote }}
catalog.homelab/llm-traffic: {{ index .Chart.Annotations "catalog.homelab/llm-traffic" | default "false" | quote }}
catalog.homelab/wants-vector: {{ index .Chart.Annotations "catalog.homelab/wants-vector" | default "false" | quote }}
catalog.homelab/exposes-mcp: {{ index .Chart.Annotations "catalog.homelab/exposes-mcp" | default "false" | quote }}
catalog.homelab/tier: {{ .Values.catalog.tier | default "dev" | quote }}
catalog.homelab/data-class: {{ .Values.catalog.dataClass | default "internal" | quote }}
{{- end }}

{{/*
catalog.ingressHost — the hostname for the IngressRoute. Defaults to
"<release-name>.lab.local" if .Values.ingress.host is blank.
*/}}
{{- define "catalog.ingressHost" -}}
{{- if .Values.ingress.host -}}
{{- .Values.ingress.host -}}
{{- else -}}
{{- printf "%s.lab.local" .Release.Name -}}
{{- end -}}
{{- end }}

{{/*
catalog.image — the fully-qualified image reference. Falls back to
.Chart.AppVersion if .Values.image.tag is blank.
*/}}
{{- define "catalog.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end }}
