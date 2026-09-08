from dataclasses import dataclass

# See plugins/__init__.py: the full harness (locationtools -> repo_ops ->
# bm25_retriever -> llama_index) is optional in the minimal TS runtime.
try:
    from plugins.location_tools import locationtools
    from plugins.requirement import PluginRequirement  # , Plugin,

    @dataclass
    class LocationToolsRequirement(PluginRequirement):
        name: str = 'location_tools'
        documentation: str = locationtools.DOCUMENTATION

except ModuleNotFoundError:
    LocationToolsRequirement = None  # type: ignore


# class LocationToolsPlugin(Plugin):
#     name: str = 'location_tools'
