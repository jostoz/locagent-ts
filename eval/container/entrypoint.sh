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
if [ -n "${CLINE_DISABLED_TOOLS:-}" ]; then
node -e '
const fs = require("fs");
const tools = process.env.CLINE_DISABLED_TOOLS.split(",").map(s => s.trim()).filter(Boolean);
fs.writeFileSync("/state/settings/global-settings.json", JSON.stringify({disabledTools: tools}));
'
else
rm -f /state/settings/global-settings.json
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
#
# temperature=0 forces greedy decoding: identical input, identical output,
# every run -- without it, qwen3.8-27b under tool-calling pressure shows real
# sampling variance (confirmed empirically: same canonical package produced a
# clean 5-call success once and a 132-call identical-chunk loop another time).
#
# CLINE_REASONING_MAX_TOKENS is a hard cap on reasoning tokens, not a request
# like --thinking/reasoning_effort (which the model can and does ignore under
# load -- confirmed: --thinking none alone still let qwen3.8-27b reason freely
# in several transcripts this session). 0 or unset disables the cap.
: "${CLINE_TEMPERATURE:=0}"
: "${CLINE_REASONING_MAX_TOKENS:=0}"
node -e '
const fs = require("fs");
const p = "/state/settings/providers.json";
const d = JSON.parse(fs.readFileSync(p, "utf8"));
const s = d.providers[process.env.CLINE_PROVIDER].settings;
s.maxTokens = Number(process.env.CLINE_MAX_TOKENS);
s.temperature = Number(process.env.CLINE_TEMPERATURE);
const reasoningMax = Number(process.env.CLINE_REASONING_MAX_TOKENS);
if (reasoningMax > 0) {
  s.reasoning = Object.assign({}, s.reasoning, {enabled: true, maxTokens: reasoningMax});
}
fs.writeFileSync(p, JSON.stringify(d));
'

exec cline --config /state/config --data-dir /state "$@"
