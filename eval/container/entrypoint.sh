#!/bin/sh
set -eu

: "${CLINE_BASE_URL:=http://host.docker.internal:11434/v1}"
: "${CLINE_PROVIDER:=openai-compatible}"
: "${CLINE_MODEL:=qwen3.8-27b}"
: "${CLINE_API_KEY:=lm-studio-local}"
: "${CLINE_MAX_TOKENS:=8192}"
export LOCAGENT_REPO=/workspace
export LOCAGENT_CACHE_DIR=/tmp/locagent-cache

mkdir -p /state/settings /tmp/locagent-cache
if [ "${DISABLE_LOCAGENT_MCP:-0}" != "1" ]; then
cat >/state/settings/cline_mcp_settings.json <<'JSON'
{
  "mcpServers": {
    "locagent": {
      "transport": {
        "type": "stdio",
        "command": "python3",
        "args": ["/opt/locagent/locagent_mcp.py"]
      }
    }
  }
}
JSON
else
rm -f /state/settings/cline_mcp_settings.json
fi
if [ "${ENABLE_TGREP_MCP:-0}" = "1" ]; then
  node -e '
const fs = require("fs");
const p = "/state/settings/cline_mcp_settings.json";
const d = fs.existsSync(p) ? JSON.parse(fs.readFileSync(p, "utf8")) : {mcpServers:{}};
d.mcpServers = d.mcpServers || {};
d.mcpServers.tgrep = {transport:{type:"stdio",command:"python3",args:["/opt/tgrep_mcp.py"]}};
fs.writeFileSync(p, JSON.stringify(d));
'
fi

# State is recreated per invocation. The local API key is only a marker for
# LM Studio, and the container receives no planner credential.
if [ "$CLINE_PROVIDER" = "openai-compatible" ]; then
  cline auth --config /state/config --data-dir /state \
    -p "$CLINE_PROVIDER" -k "$CLINE_API_KEY" -b "$CLINE_BASE_URL" -m "$CLINE_MODEL" >/dev/null
else
  cline auth --config /state/config --data-dir /state \
    -p "$CLINE_PROVIDER" -k "$CLINE_API_KEY" -m "$CLINE_MODEL" >/dev/null
fi

# Cline otherwise assumes the provider's advertised maximum completion size.
# Serverless providers reject that reservation when the account balance cannot
# cover it, even though agent turns normally use far fewer tokens.
node -e '
const fs = require("fs");
const p = "/state/settings/providers.json";
const d = JSON.parse(fs.readFileSync(p, "utf8"));
d.providers[process.env.CLINE_PROVIDER].settings.maxTokens = Number(process.env.CLINE_MAX_TOKENS);
fs.writeFileSync(p, JSON.stringify(d));
'

exec cline --config /state/config --data-dir /state "$@"
