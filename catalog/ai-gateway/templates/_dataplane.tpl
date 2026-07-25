{{/*
aigateway.dataplaneName — the EG-generated data-plane Deployment/Service
name: envoy-<ns>-<gateway>-<first 8 hex of sha256("<ns>/<gateway>")>.
Deterministic, so templates (ExternalName Service, ScaledObject
scaleTargetRef) can reference it without hardcoding the hash.
*/}}
{{- define "aigateway.dataplaneName" -}}
envoy-{{ .Release.Namespace }}-{{ include "catalog.fullname" . }}-{{ printf "%s/%s" .Release.Namespace (include "catalog.fullname" .) | sha256sum | trunc 8 }}
{{- end }}
