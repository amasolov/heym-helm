{{- define "heym.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* Selector labels. Sandbox pods never carry app.kubernetes.io/name, so the
backend's selectors and NetworkPolicy cannot match them. */}}
{{- define "heym.selectorLabels" -}}
{{- if .Values.selectorLabels -}}
{{- toYaml .Values.selectorLabels -}}
{{- else -}}
app.kubernetes.io/name: {{ default .Chart.Name .Values.nameOverride }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- end -}}

{{- define "heym.labels" -}}
app.kubernetes.io/name: {{ default .Chart.Name .Values.nameOverride }}
app.kubernetes.io/instance: {{ .Release.Name }}
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

{{- define "heym.generateKeys" -}}
{{- $s := .Values.secrets -}}
{{- $signing := or $s.secretKey (hasKey $s.keyRefs "SECRET_KEY") -}}
{{- $encryption := or $s.encryptionKey (hasKey $s.keyRefs "ENCRYPTION_KEY") -}}
{{- if and $s.generate (not $s.existingSecret) (not (and $signing $encryption)) }}true{{ end -}}
{{- end -}}

{{- define "heym.inlineSecret" -}}
{{- if and (not .Values.secrets.existingSecret) (or .Values.secrets.secretKey .Values.secrets.encryptionKey .Values.secrets.databaseUrl) }}true{{ end -}}
{{- end -}}

{{- define "heym.validateSecrets" -}}
{{- $s := .Values.secrets -}}
{{- range $name, $ref := $s.keyRefs -}}
{{- if hasKey $.Values.env $name -}}
{{- fail (printf "Define %s in either secrets.keyRefs or env, not both" $name) -}}
{{- end -}}
{{- end -}}
{{- if and $s.existingSecret (or $s.secretKey $s.encryptionKey $s.databaseUrl) -}}
{{- fail "Do not combine secrets.existingSecret with inline secret values; use existingSecrets for split sources" -}}
{{- end -}}
{{- if not (or $s.existingSecret $s.existingSecrets .Values.envFrom (and (or $s.secretKey (hasKey $s.keyRefs "SECRET_KEY")) (or $s.encryptionKey (hasKey $s.keyRefs "ENCRYPTION_KEY"))) (include "heym.generateKeys" .)) -}}
{{- fail "Provide pre-created Secrets, inline application keys, or enable secrets.generate" -}}
{{- end -}}
{{- if not (or $s.databaseUrl $s.existingSecret $s.existingSecrets .Values.envFrom (hasKey $s.keyRefs "DATABASE_URL") (hasKey .Values.env "POSTGRES_HOST") (hasKey .Values.env "DATABASE_URL")) -}}
{{- fail "Configure the external database via secrets.databaseUrl, existing Secrets, envFrom or env.POSTGRES_HOST" -}}
{{- end -}}
{{- end -}}

{{- define "heym.claimName" -}}
{{- default (printf "%s-data" (include "heym.fullname" .)) .Values.persistence.existingClaim -}}
{{- end -}}

{{- define "heym.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- /* A digest in the tag (version@sha256:...) wins over image.digest. */ -}}
{{- if and .Values.image.digest (not (contains "@" $tag)) -}}
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
