{{- define "shop.name" -}}{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}{{- end -}}
{{- define "shop.fullname" -}}{{- default (printf "%s-%s" .Release.Name (include "shop.name" .)) .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}{{- end -}}
{{- define "shop.commonLabels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/part-of: shop
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "shop.meshAnnotations" -}}
{{- if .Values.mesh.enabled }}
linkerd.io/inject: enabled
config.linkerd.io/proxy-cpu-request: {{ .Values.mesh.proxyResources.cpuRequest | quote }}
config.linkerd.io/proxy-memory-request: {{ .Values.mesh.proxyResources.memoryRequest | quote }}
config.linkerd.io/proxy-cpu-limit: {{ .Values.mesh.proxyResources.cpuLimit | quote }}
config.linkerd.io/proxy-memory-limit: {{ .Values.mesh.proxyResources.memoryLimit | quote }}
{{- end }}
{{- end -}}
