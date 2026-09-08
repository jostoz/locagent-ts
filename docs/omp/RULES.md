# Localización de código: usar el MCP `locagent` antes de leer archivos

Plantilla de regla OMP. Copiar a `<proyecto>/.omp/RULES.md` (se aplica siempre,
sticky). Para cualquier bug / refactor en un repo TS/TSX:

## 1. Diagnóstico — MCP `locagent`

- `get_repo_overview` una vez por sesión para el layout.
- `search_code_entities("<descripción de la tarea>")` → entidades con `file:line`,
  firma corta y flag `[component]`.
- `traverse("<id>", edge_types=["invokes","renders","imports"], direction="both")`
  para ver llamadores y dependencias **antes** de tocar nada.
- `get_entity("<id>", "skeleton")` primero; `"full"` solo cuando vas a editar.
  Nunca `read` de un archivo grande entero — si la entidad es enorme, pedí la
  entidad anidada (`Componente.handleX`).

## 2. Verificación — nativo OMP

- `lsp` para confirmar firma / tipos / referencias reales de la entidad localizada
  (LocAgent matchea por nombre, sin tipos; el LSP lo valida).
- `ast_grep` si hay que buscar una forma sintáctica concreta.

## 3. Edición — nativo OMP

- `edit` hashline sobre el rango acotado que devolvió `get_entity`.
- Nunca reescribir el archivo completo.

## Notas

- El grafo de `locagent` se construye al primer `tools/call` y se cachea
  (`LOCAGENT_CACHE_DIR`). Si agregás o movés archivos durante la sesión: `/mcp reload`.
- El primer call puede tardar unos segundos (build); los siguientes son instantáneos.
