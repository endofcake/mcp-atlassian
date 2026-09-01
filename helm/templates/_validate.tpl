{{/*
Render-time validation of the Bitbucket block. Included from every template
that reads .Values.bitbucket so a partial render (helm template --show-only)
fails on the same misconfiguration as a full one.
*/}}
{{- define "mcp-atlassian.validateBitbucket" -}}
{{- $bitbucket := .Values.bitbucket | default dict }}
{{- if $bitbucket.enabled }}
{{- /* A list or map cannot be coerced to a string; reject it before the
       checks below would trim its printed form. */}}
{{- range $key := list "url" "authMode" "oauthClientId" "oauthClientSecret" "oauthRedirectUri" "oauthScope" "oauthAccessToken" }}
{{- $value := index $bitbucket $key }}
{{- if or (kindIs "slice" $value) (kindIs "map" $value) }}
{{- fail (printf "bitbucket.%s must be a string, got a %s." $key (kindOf $value)) }}
{{- end }}
{{- end }}
{{- if not (trim (toString (default "" $bitbucket.url))) }}
{{- fail "bitbucket.url is required when bitbucket.enabled is true; without it the server cannot resolve BITBUCKET_URL and the pod crashloops." }}
{{- end }}
{{- if not (has $bitbucket.authMode (list "oauth" "byot")) }}
{{- fail (printf "bitbucket.authMode must be one of \"oauth\" or \"byot\" when bitbucket.enabled is true, got %q. An unrecognised value silently disables Bitbucket." (toString $bitbucket.authMode)) }}
{{- end }}
{{- /* Jira and Confluence are enabled by default with an empty url, so an
       untouched block is unused rather than configured; only a nonblank url
       counts as a second provider family. */}}
{{- $configuredFamilies := list }}
{{- if and .Values.jira.enabled (trim (toString (default "" .Values.jira.url))) }}
{{- $configuredFamilies = append $configuredFamilies "jira" }}
{{- end }}
{{- if and .Values.confluence.enabled (trim (toString (default "" .Values.confluence.url))) }}
{{- $configuredFamilies = append $configuredFamilies "confluence" }}
{{- end }}
{{- if and .Values.oauthProxy.enabled $configuredFamilies }}
{{- fail (printf "bitbucket cannot be enabled alongside a configured %s while oauthProxy.enabled is true, whatever the top-level authMode: the OAuth proxy fronts exactly one provider family, and the server rejects proxy-minted tokens for the other family, leaving its tools visible but unusable. Disable the other family or clear its url, or run a separate release per provider family." (join " and " $configuredFamilies)) }}
{{- end }}
{{- /* The proxy is built from Bitbucket OAuth client credentials and a
       redirect URI; without them the server logs a warning and starts with
       the proxy disabled, and a second set of proxy credentials from the
       top-level oauth block makes it refuse to start. */}}
{{- if .Values.oauthProxy.enabled }}
{{- if ne $bitbucket.authMode "oauth" }}
{{- fail (printf "bitbucket.authMode must be \"oauth\" when oauthProxy.enabled is true, got %q: the OAuth proxy needs Bitbucket OAuth client credentials to front the instance, and under \"byot\" the server starts with the proxy disabled." $bitbucket.authMode) }}
{{- end }}
{{- if not (trim (toString (default "" $bitbucket.oauthRedirectUri))) }}
{{- fail "bitbucket.oauthRedirectUri is required when oauthProxy.enabled is true and bitbucket.authMode is \"oauth\"; without it the server starts with the proxy disabled. Set it to the redirect URI registered on the Bitbucket incoming application link." }}
{{- end }}
{{- /* The Secret emits oauth.clientId untrimmed, so any nonempty value,
       whitespace included, reaches the server as a second client id. */}}
{{- $oauth := .Values.oauth | default dict }}
{{- if and (eq .Values.authMode "oauth") (toString (default "" $oauth.clientId)) }}
{{- fail "oauth.clientId cannot be set alongside Bitbucket OAuth client credentials while oauthProxy.enabled is true: the OAuth proxy fronts exactly one provider family, and the server refuses to start when both families supply proxy credentials. Clear oauth.clientId or change the top-level authMode." }}
{{- end }}
{{- end }}
{{- if eq $bitbucket.authMode "oauth" }}
{{- if not (trim (toString (default "" $bitbucket.oauthClientId))) }}
{{- fail "bitbucket.oauthClientId is required when bitbucket.authMode is \"oauth\"; without it the server cannot obtain tokens and fails at startup." }}
{{- end }}
{{- if not (trim (toString (default "" $bitbucket.oauthClientSecret))) }}
{{- fail "bitbucket.oauthClientSecret is required when bitbucket.authMode is \"oauth\"; without it the server cannot obtain tokens and fails at startup." }}
{{- end }}
{{- if not (trim (toString (default "" $bitbucket.oauthScope))) }}
{{- fail "bitbucket.oauthScope is required when bitbucket.authMode is \"oauth\": Bitbucket Data Center has no default OAuth scope. Set it to the scopes granted to the incoming application link, e.g. PUBLIC_REPOS, REPO_READ, REPO_WRITE." }}
{{- end }}
{{- else if eq $bitbucket.authMode "byot" }}
{{- if not (trim (toString (default "" $bitbucket.oauthAccessToken))) }}
{{- fail "bitbucket.oauthAccessToken is required when bitbucket.authMode is \"byot\"; without it the server has no credential and fails at startup." }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
