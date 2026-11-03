{{- define "heym.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "heym.validateConfiguration" -}}
{{- $selector := include "heym.selectorLabels" . | fromYaml -}}
{{- range $key, $value := .Values.podLabels -}}
{{- if and (hasKey $selector $key) (ne (get $selector $key) $value) -}}
{{- fail (printf "podLabels must not override Deployment selector label %s" $key) -}}
{{- end -}}
{{- end -}}
{{- $labels := mergeOverwrite (deepCopy $selector) .Values.podLabels -}}
{{- if or (eq (get $labels "app.kubernetes.io/managed-by") "heym") (hasKey $labels "heym.run/network") -}}
{{- fail "Backend labels must not select sandbox NetworkPolicies (managed-by=heym or heym.run/network)" -}}
{{- end -}}
{{- if .Values.sandbox.enabled -}}
{{- if and (hasKey .Values.env "HEYM_SANDBOX_BACKEND") (ne (toString (get .Values.env "HEYM_SANDBOX_BACKEND")) "kubernetes") -}}
{{- fail "sandbox.enabled requires HEYM_SANDBOX_BACKEND=kubernetes; disable sandbox.enabled to use another backend" -}}
{{- end -}}
{{- if hasKey .Values.env "HEYM_SANDBOX_K8S_POD_OVERRIDES" -}}
{{- fail "Use sandbox.podOverrides instead of env.HEYM_SANDBOX_K8S_POD_OVERRIDES so scheduling fields are validated" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "heym.podLabels" -}}
{{- $selector := include "heym.selectorLabels" . | fromYaml -}}
{{- toYaml (mergeOverwrite (deepCopy $selector) .Values.podLabels) -}}
{{- end -}}

{{/* Workers. Their name label differs from the backend's (<name>-worker, as
upstream's manifests use), so the Service, the backend's NetworkPolicy and
anything else selecting the backend never matches a worker. */}}
{{- define "heym.workerName" -}}
{{- printf "%s-worker" (include "heym.fullname" . | trunc 56 | trimSuffix "-") -}}
{{- end -}}

{{- define "heym.workerSelectorLabels" -}}
app.kubernetes.io/name: {{ printf "%s-worker" (default .Chart.Name .Values.nameOverride) }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "heym.workerLabels" -}}
{{ include "heym.workerSelectorLabels" . }}
app.kubernetes.io/component: worker
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: heym
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "heym.validateCluster" -}}
{{- if .Values.cluster.enabled -}}
{{- range $key := list "HEYM_CLUSTER_ENABLED" "HEYM_INSTANCE_ROLE" "HEYM_INSTANCE_ID" "HEYM_INSTANCE_NAME" -}}
{{- if or (hasKey $.Values.env $key) (hasKey $.Values.cluster.env $key) -}}
{{- fail (printf "%s is set by the chart when cluster.enabled; remove it from env and cluster.env" $key) -}}
{{- end -}}
{{- end -}}
{{- /* A worker must never receive the backend's traffic. */ -}}
{{- $backend := include "heym.selectorLabels" . | fromYaml -}}
{{- $worker := mergeOverwrite (include "heym.workerSelectorLabels" . | fromYaml) .Values.cluster.podLabels -}}
{{- $overlap := true -}}
{{- range $key, $value := $backend -}}
{{- if ne (toString (get $worker $key)) (toString $value) }}{{ $overlap = false }}{{ end -}}
{{- end -}}
{{- if $overlap -}}
{{- fail "Worker labels match the backend's selector, so the Service would route to workers; change cluster.podLabels or selectorLabels" -}}
{{- end -}}
{{- if or (eq (toString (get $worker "app.kubernetes.io/managed-by")) "heym") (hasKey $worker "heym.run/network") -}}
{{- fail "Worker labels must not select sandbox NetworkPolicies (managed-by=heym or heym.run/network)" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Egress for the backend and workers alike. */}}
{{- define "heym.backendEgress" -}}
{{- include "heym.dnsEgress" . }}
{{- if .Values.networkPolicy.allowInternet }}
- to:
    - ipBlock:
        cidr: 0.0.0.0/0
        except:
          {{- include "heym.exceptCIDRs" . | nindent 10 }}
{{- end }}
{{- if and .Values.sandbox.enabled .Values.networkPolicy.apiServerCIDRs }}
# The API server, to start sandbox pods. Its endpoints are node or
# control-plane addresses, usually inside the private ranges above.
- to:
    {{- range .Values.networkPolicy.apiServerCIDRs }}
    - ipBlock:
        cidr: {{ . }}
    {{- end }}
  ports:
    - port: 443
      protocol: TCP
    - port: 6443
      protocol: TCP
{{- end }}
{{- with .Values.networkPolicy.extraEgress }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* Secret sources, identical for the backend and workers: a worker whose
key digest differs from main's shows as Mismatch and gets no work. */}}
{{- define "heym.envFrom" -}}
{{- if include "heym.generateKeys" . }}
- secretRef:
    name: {{ include "heym.fullname" . }}-generated-keys
{{- end }}
{{- if or .Values.secrets.existingSecret (include "heym.inlineSecret" .) }}
- secretRef:
    name: {{ include "heym.secretName" . }}
{{- end }}
{{- range .Values.secrets.existingSecrets }}
- secretRef:
    name: {{ . | quote }}
{{- end }}
{{- with .Values.envFrom }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* Preserve default-before-custom ordering for Kubernetes $(VAR) expansion.
An explicit env value replaces its default entry instead of duplicating it.
Writable cache/data paths and UV_NO_SYNC keep the stock image compatible with
read-only root storage. The Python runner uses the GHCR release image layout.
Called with (dict "root" $ "role" "main"|"worker"). Workers take env, then
cluster.env on top, and get their instance ID from the pod name. */}}
{{- define "heym.environment" -}}
{{- $root := .root -}}
{{- $worker := eq .role "worker" -}}
{{- $env := $root.Values.env -}}
{{- if $worker -}}
{{- $env = mergeOverwrite (deepCopy $root.Values.env) $root.Values.cluster.env -}}
{{- end -}}
{{- $defaults := list
    (dict "name" "FRONTEND_URL" "value" $root.Values.url)
    (dict "name" "CORS_ORIGINS" "value" $root.Values.url)
    (dict "name" "BACKEND_WORKERS" "value" (ternary $root.Values.cluster.processes $root.Values.workers $worker))
    (dict "name" "AUTO_REWRITE_LOCAL_DATABASE_HOST" "value" "false")
    (dict "name" "FILE_STORAGE_DIR" "value" "/app/data/files")
    (dict "name" "HEYM_PLUGINS_DIR" "value" "/app/data/plugins")
    (dict "name" "HOME" "value" "/tmp")
    (dict "name" "UV_CACHE_DIR" "value" "/tmp/uv-cache")
    (dict "name" "UV_NO_SYNC" "value" "1") -}}
{{- if $root.Values.sandbox.enabled -}}
{{- $defaults = append $defaults (dict "name" "HEYM_SANDBOX_BACKEND" "value" "kubernetes") -}}
{{- if $root.Values.sandbox.podOverrides -}}
{{- $defaults = append $defaults (dict "name" "HEYM_SANDBOX_K8S_POD_OVERRIDES" "value" (toJson $root.Values.sandbox.podOverrides)) -}}
{{- end -}}
{{- $defaults = append $defaults (dict "name" "HEYM_PYTHON_TOOL_RUNNER_PATH" "value" "/app/backend/app/services/python_tool_runner.py") -}}
{{- end -}}
{{- if $root.Values.cluster.enabled -}}
{{- $defaults = append $defaults (dict "name" "HEYM_CLUSTER_ENABLED" "value" "true") -}}
{{- $defaults = append $defaults (dict "name" "HEYM_INSTANCE_ROLE" "value" .role) -}}
{{- end -}}
{{- $seen := dict -}}
{{- range $entry := $defaults }}
{{- $_ := set $seen $entry.name true }}
- name: {{ $entry.name }}
  {{- if hasKey $env $entry.name }}
  value: {{ get $env $entry.name | quote }}
  {{- else }}
  value: {{ $entry.value | quote }}
  {{- end }}
{{- end }}
{{- if $worker }}
{{- /* Unique and stable per worker: a rescheduled pod rejoins its own row
under Settings -> Instances and keeps its weight. Without an ID every worker
falls back to "worker" and they collapse into one. */}}
- name: HEYM_INSTANCE_ID
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: HEYM_INSTANCE_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
{{- end }}
{{- range $name, $value := $env }}
{{- if not (hasKey $seen $name) }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{- end }}
{{- range $name, $ref := $root.Values.secrets.keyRefs }}
- name: {{ $name }}
  valueFrom:
    secretKeyRef:
      name: {{ $ref.name | quote }}
      key: {{ $ref.key | quote }}
      optional: false
{{- end }}
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
