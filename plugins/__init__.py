# Requirements
#
# The benchmark/agent harness (openhands-style plugin requirement) pulls in the
# full training stack (llama_index, datasets, ...). The minimal TS runtime
# (requirements-ts.txt) does not install it, but leaf modules under plugins/
# -- e.g. location_tools/retriever/fuzzy_retriever.py and
# location_tools/utils/compress_file_ts.py -- are still imported directly by the
# MCP server. Guard the harness import so those stay reachable.
try:
    from plugins.location_tools import (
        # AgentSkillsPlugin,
        LocationToolsRequirement,
    )
    from plugins.requirement import PluginRequirement  # , Plugin

    __all__ = [
        'PluginRequirement',
        'LocationToolsRequirement',
    ]
except ModuleNotFoundError:
    __all__ = []
