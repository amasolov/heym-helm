{{- define "heym.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* Selector labels. Sandbox pods never carry app.kubernetes.io/name, so the
backend's selectors and NetworkPolicy cannot match them. */}}
{{- define "heym.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "heym.labels" -}}
{{ include "heym.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: heym
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "heym.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "heym.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "heym.secretName" -}}
{{- default (printf "%s-secrets" (include "heym.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}

{{- define "heym.claimName" -}}
{{- default (printf "%s-data" (include "heym.fullname" .)) .Values.persistence.existingClaim -}}
{{- end -}}

{{- define "heym.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- if .Values.image.digest -}}
{{- printf "%s:%s@%s" .Values.image.repository $tag .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "heym.validPlatform" -}}
{{- if not (has .Values.platform (list "generic" "openshift" "eks" "aks" "gke")) -}}
{{- fail (printf "platform must be one of generic, openshift, eks, aks, gke; got %q" .Values.platform) -}}
{{- end -}}
{{- end -}}

{{/* ---- Platform defaults. Each returns "true"/"" or YAML. ---- */}}

{{- define "heym.openshift" -}}
{{- if eq .Values.platform "openshift" }}true{{ end -}}
{{- end -}}

{{- define "heym.routeEnabled" -}}
{{- if kindIs "bool" .Values.route.enabled -}}
{{- if .Values.route.enabled }}true{{ end -}}
{{- else if eq .Values.platform "openshift" }}true{{ end -}}
{{- end -}}

{{- define "heym.ciliumApiServerPolicy" -}}
{{- if kindIs "bool" .Values.ciliumApiServerPolicy -}}
{{- if .Values.ciliumApiServerPolicy }}true{{ end -}}
{{- else if and (eq .Values.cni "cilium") .Values.networkPolicy.enabled }}true{{ end -}}
{{- end -}}

{{- define "heym.sandboxAllowAllEgress" -}}
{{- if kindIs "bool" .Values.sandbox.networkPolicy.allowAllEgress -}}
{{- if .Values.sandbox.networkPolicy.allowAllEgress }}true{{ end -}}
{{- end -}}
{{- end -}}

{{/* CIDRs online sandboxes and the backend may not reach. AKS adds the
WireServer, which sits on a public address and so escapes the private ranges. */}}
{{- define "heym.exceptCIDRs" -}}
{{- $cidrs := concat .Values.sandbox.networkPolicy.exceptCIDRs .Values.sandbox.networkPolicy.extraExceptCIDRs -}}
{{- if eq .Values.platform "aks" -}}
{{- $cidrs = append $cidrs "168.63.129.16/32" -}}
{{- end -}}
{{- toYaml (uniq $cidrs) -}}
{{- end -}}

{{/* The cluster DNS peer and ports. OpenShift's DNS pods listen on 5353: the
Service maps 53 to 5353, and NetworkPolicy ports apply after that mapping. */}}
{{- define "heym.dnsEgress" -}}
{{- $dns := .Values.sandbox.networkPolicy.dns -}}
{{- $ns := $dns.namespaceSelector -}}
{{- $pod := $dns.podSelector -}}
{{- $ports := list 53 -}}
{{- if eq .Values.platform "openshift" -}}
{{- if not $ns }}{{ $ns = dict "matchLabels" (dict "kubernetes.io/metadata.name" "openshift-dns") }}{{ end -}}
{{- if not $pod }}{{ $pod = dict "matchLabels" (dict "dns.operator.openshift.io/daemonset-dns" "default") }}{{ end -}}
{{- $ports = list 53 5353 -}}
{{- else -}}
{{- if not $ns }}{{ $ns = dict "matchLabels" (dict "kubernetes.io/metadata.name" "kube-system") }}{{ end -}}
{{- if not $pod }}{{ $pod = dict "matchLabels" (dict "k8s-app" "kube-dns") }}{{ end -}}
{{- end -}}
- to:
    - namespaceSelector:
        {{- toYaml $ns | nindent 8 }}
      podSelector:
        {{- toYaml $pod | nindent 8 }}
  ports:
    {{- range $ports }}
    - port: {{ . }}
      protocol: UDP
    - port: {{ . }}
      protocol: TCP
    {{- end }}
{{- end -}}

{{- define "heym.podSecurityContext" -}}
{{- $ctx := deepCopy .Values.podSecurityContext -}}
{{- if eq .Values.platform "openshift" -}}
{{- $ctx = omit $ctx "runAsUser" "runAsGroup" "fsGroup" "fsGroupChangePolicy" -}}
{{- end -}}
{{- toYaml $ctx -}}
{{- end -}}
