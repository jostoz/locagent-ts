# Plan: Port de LocAgent a TypeScript/TSX (tree-sitter) + MCP para Cline

> Fork de `gersteinlab/LocAgent` (Apache-2.0). Rama de trabajo: `feat/typescript-support`.
> Repo de prueba: `C:/Users/joz/orca/workspaces/miro-clone/bichir` (referenciado por ruta absoluta, NO se toca su código).

## Contexto

**Problema:** un agente (Cline) con un modelo local de contexto chico (qwen, 32k) se ahoga con archivos gigantes — en miro-clone `Board.tsx` = 7.565 líneas ≈ 95k tokens (23% del repo), + su test 5.053, + `slide-layouts.tsx` 3.060. `read_files` trunca, el modelo ve un fragmento, alucina "discrepancia con el disco" y entra en loop de arqueología git entre worktrees hermanos. El resto del repo (32.286 líneas) es normal.

**Solución:** retrieval guiado por grafo de código — nunca pasar el archivo entero, solo el nodo relevante (~400 tokens).

**Por qué construirlo y no usar Serena/LSP:** lo que da de más — traversía de grafo como herramienta del agente, BM25 difuso sobre el grafo, y un artefacto de research agnóstico al lenguaje que se controla y extiende. El paper de LocAgent (ACL 2025) reporta Qwen-2.5-Coder-32B fine-tuned con 92.7% de localización a nivel archivo y ~86% menos costo — exactamente el caso de uso.

## Arquitectura de LocAgent (lo relevante)

Tiene **dos** capas de parseo:
1. `dependency_graph/build_graph.py` (30KB) → **Python `ast`** → el grafo heterogéneo. **Esto es lo que hay que portar.** Node types: `directory/file/class/function`. Edge types: `contains/inherits/invokes/imports` (constantes `NODE_TYPE_*` / `EDGE_TYPE_*` al tope del archivo; `VALID_NODE_TYPES` / `VALID_EDGE_TYPES`).
   Funciones clave: `find_imports`, `class CodeAnalyzer(ast.NodeVisitor)`, `resolve_module`, `add_imports`, `analyze_invokes`, `analyze_init`, `find_all_possible_callee`, `build_graph()` (orquestador networkx, ~167 líneas — se conserva).
2. `repo_index/codeblocks/parser/` → **ya usa tree-sitter** vía `from tree_sitter_languages import get_language` (`python.py`, `java.py`, `queries/*.scm`, dispatcher `create.py`). Patrón a seguir. Prueba que Java (no-Python) funciona en LocAgent.

Todo lo downstream del grafo es agnóstico al lenguaje.

## Hallazgos de setup (revisando imports reales)

- **NO instalar `requirements.txt`** (~180 paquetes: `torch`, stack CUDA `nvidia-cu12`, `datasets`, `faiss`, `llama-index` entero, `libcst`, `matplotlib` — entorno de training/eval; `torch==2.5.1` probablemente sin wheel para Python 3.13).
- **venv mínimo** (`requirements-ts.txt`): `networkx`, `tree-sitter==0.21.3`, `tree-sitter-languages==1.10.2` (ya son deps de LocAgent), `bm25s`, `PyStemmer`, `rapidfuzz`, `mcp`.
- **`plugins/location_tools/utils/compress_file.py` usa `libcst`** (CST de Python) → el skeletonizer es Python-only, **reescribir con tree-sitter**.
- **`plugins/location_tools/retriever/bm25_retriever.py` arrastra `llama-index` entero** (`llama_index.core`, `llama_index.retrievers.bm25`, `repo_index.index.epic_split.EpicSplitter`) → **no portar; escribir `ts_bm25.py` propio con `bm25s`** sobre los nodos del grafo (`name` + `code` + skeleton). Sin EpicSplitter (ya tenemos el chunking del grafo).
- **`plugins/location_tools/retriever/fuzzy_retriever.py`** (`rapidfuzz` + networkx + `dependency_graph`) → reutilizar casi tal cual.
- **`dependency_graph/traverse_graph.py`** → reutilizar tal cual.
- **`plugins/location_tools/repo_ops/repo_ops.py`** (tools del agente) muy acoplado al harness de benchmark (`util.benchmark.setup_repo`, `datasets`) → no reutilizar entero; el MCP llama directo a: builder + `ts_bm25` + `fuzzy_retriever` + `traverse_graph`.
- `batch_build_graph.py` / `build_bm25_index.py` importan `torch.multiprocessing` + `datasets` → no se usan; llamar `build_graph()` directo.
- `build_graph.py` es liviano (`ast`, `networkx`, `matplotlib` solo para `visualize_graph` → opcional).

## Alcance del repo de prueba (miro-clone) — simplifica imports

- Único alias: `@/*` → `./src/*` (de `tsconfig.app.json`). Sin `baseUrl`, sin monorepo.
- Imports reales: `@/lib/utils`, relativos (`./zorder`, `./useStickyNotes`), bare specifiers (`react`, `konva`, `lucide-react` → externos).

---

## Fase 0 — Proyecto standalone + setup  ✅

- ✅ Fork `gersteinlab/LocAgent` → `jostoz/locagent-ts` (Apache-2.0)
- ✅ Clonado en `C:/Users/joz/orca/projects/locagent-ts/`, registrado en Orca (`repo id 0b8a1435-4214-413c-b311-a2a621c2b673`), rama `feat/typescript-support`
- ✅ venv + `requirements-ts.txt` (7 paquetes). **Python 3.12** (`.venv/`), NO 3.13:
  `tree-sitter-languages==1.10.2` no tiene wheels cp313. `py -3.12 -m venv .venv && .venv/Scripts/pip install -r requirements-ts.txt`.
- ✅ `NOTICE` (cambios sobre el original Apache-2.0) + sección "🔀 Fork" en `README.md`
- ✅ `build_graph.py`: `matplotlib` movido a import lazy dentro de `visualize_graph` (no está en requirements-ts); `EDGE_TYPE_RENDERS` agregado a `VALID_EDGE_TYPES`.
- ⬜ (opcional) correr el flujo Python baseline sobre un repo `.py` chico — no bloqueante

## Fase 1 — Extractor TS/TSX (3-5 días) — el trabajo central

**Estado (2026-09-08): primer cut funcional end-to-end sobre miro-clone.**
`python -m dependency_graph.ts_build_graph --repo <miro-clone>` → 87 files, 379 func + 5 class,
edges: contains 510 / imports 259 / invokes 284 / renders 141 / inherits 0 (correcto: no hay
herencia real en el repo). ~0.5s. Grafo picklea OK. Verificado: `Board` (607-6931, `is_component`),
`renders` entrantes correctas a toolbars/`CropConfirmToolbar`, `imports` resueltas a la entidad
concreta, `invokes` cross-file por scope de import, `inherits` en repo sintético.

Archivos: `ts_build_graph.py`, `ts_resolver.py`, `queries/typescript.scm`, `queries/tsx.scm`.

Decisiones / gramática (tree-sitter-languages 1.10.2, gramática ~2023):
- función-expresión = nodo `function` (NO `function_expression`).
- `const X = forwardRef(fn)` / `memo(fn)` / `observer(fn)`: nueva captura `@def.wrapped`;
  `_find_inner_function` saca la fn de los argumentos del `call_expression` (recursivo para
  `memo(forwardRef(fn))`). Un declarator con call sin fn adentro (`const x = compute()`) se descarta.
- id de nodo tree-sitter: usar `(start_byte, end_byte)`, NO `id()` builtin — los bindings devuelven
  un wrapper nuevo en cada `.parent`/`.children`.
- Node-ids del grafo normalizados a POSIX (`/`), no `os.sep` (Windows).
- `_CALL_STOPLIST`: ~70 métodos ultra-comunes de Array/Promise/DOM excluidos de `invokes` (ruido).

Pendientes (no bloqueantes para Fase 2/3):
- `interface` / `type` / `enum` NO se materializan aún (plan v1 = function + class). Alto valor para
  localización TS — candidato a mapear a `NODE_TYPE_CLASS` con metadata `ts_kind`.
- `export default memo(...)` / `export default function` anónimo: sólo se cubre la forma
  `const X = wrapper(fn)`.
- defs anidadas dentro del cuerpo de un componente/función grande (p.ej. helpers dentro de `Board`)
  no se materializan — se atribuyen al contenedor. Limitación conocida del v1 heurístico.
- skeleton corta a 600 chars; firmas con object-type inline largas quedan truncadas.

---

Nuevo `dependency_graph/ts_build_graph.py`. Reutiliza el ensamblado networkx, el walk de directorios y el esquema de node-ids de `build_graph()`; reemplaza **solo la capa de parseo**:

1. **Parser**: `tree_sitter_languages.get_language('typescript')` / `get_language('tsx')`. Gramática por extensión: `.ts/.mts/.cts` → typescript; `.tsx/.jsx` → tsx; `.js` → tsx (tolera JS).
2. **Query** `dependency_graph/queries/typescript.scm` (+ `tsx.scm` deltas), modelado sobre `repo_index/codeblocks/parser/queries/python.scm`. Capturar:
   - `function`: `function_declaration`, `method_definition`, `variable_declarator` con valor `arrow_function`/`function_expression`, `export const X = () =>`
   - `class`: `class_declaration`
   - Componente React (heurística): nombre PascalCase + cuerpo retorna `jsx_element`/`jsx_fragment`/`jsx_self_closing_element` → `NODE_TYPE_FUNCTION` con metadata `is_component=true`
   - Metadata por nodo: `code` (slice por byte-range), `start_line`/`end_line` (`start_point.row`), `parent_type`, `skeleton` (firma + JSDoc/comentario previo, cuerpo elidido)
3. **`contains`**: del anidamiento del árbol (file→class→method, file→function).
4. **`imports`** — nuevo `dependency_graph/ts_resolver.py` (reemplaza `find_imports`/`resolve_module`):
   - `import_statement` → especificador + nombres (`import_clause`, `named_imports`, `namespace_import`, default). También `export ... from` (barrels).
   - Resolver: relativos (`./`, `../`) + alias `@/* → src/*`; probar `.ts .tsx .js .jsx` y luego `/index.{ts,tsx,js}`; bare specifiers → **omitir la arista** en v1 (no crear nodo).
   - Barrels: resolver 1 hop en v1; documentar limitación.
5. **`inherits`**: `class_heritage` → `extends`/`implements` → match por nombre a nodos clase.
6. **`invokes`** — reemplaza `analyze_invokes`/`find_all_possible_callee`: walk de `call_expression`; callee `identifier` o `member_expression.property`; **match por nombre** contra nodos función/método conocidos (misma limitación heurística que la versión Python `ast`). Saltear defs anidadas.
7. **Nuevo edge `renders`** (TS, alto valor): opening tag de `jsx_element`/`jsx_self_closing_element` con identificador PascalCase → nodo componente. Análogo UI de `invokes`. Agregar a `VALID_EDGE_TYPES` en `build_graph.py`; traversía/tools lo tratan como cualquier arista.

## Fase 2 — Índice propio + reuso (½-1 día)

- **`ts_bm25.py` propio** con `bm25s` sobre los nodos del grafo (`name` + `code` + skeleton).
- Reusar `dependency_graph/traverse_graph.py` tal cual.
- Reusar `plugins/location_tools/retriever/fuzzy_retriever.py` (ajuste mínimo si hace falta).
- **`compress_file_ts.py`**: skeletonizer tree-sitter (elide cuerpos, deja firma + JSDoc). El `compress_file.py` original (libcst) queda para Python.

## Fase 3 — Servidor MCP (½-1 día)

- `locagent_mcp.py` con el SDK `mcp` (`FastMCP`). Herramientas:
  - `search_code_entities(keywords)` → `ts_bm25` + `fuzzy_retriever` → entidades ranked con file:line
  - `traverse(entity_id, edge_types, hops)` → vecindario vía `traverse_graph`
  - `get_entity(entity_id, mode="skeleton"|"full")` → código o esqueleto del nodo
  - `get_repo_overview()` → árbol de directorios + entidades top-level
- Al arrancar: construir (o cargar cache) grafo + BM25 **solo de la raíz del cwd** (confinar = "cero vagabundeo por `..`"). Cache en `.locagent/` del repo *objetivo*. v1 rebuild completo al arrancar.
- Corregir `C:\Users\joz\.cline\data\settings\cline_mcp_settings.json`: `"args": ["C:/Users/joz/orca/projects/locagent-ts/locagent_mcp.py"]` + `cwd` del worktree objetivo. (Hoy `/ruta/a/LocAgent/...` — placeholder muerto que falla en silencio cada arranque de Cline.)

## Fase 4 — Eval (1-2 días, opcional — la parte "researcher")

- `eval/miro_clone_localization.jsonl`: 20-30 casos etiquetados a mano ("dada esta tarea, qué archivos/funciones son relevantes"): toolbar, sticky notes, z-order, slide layouts, export PPTX.
- Métricas de `evaluation/eval_metric.py` (acc@k a nivel archivo y función).
- Comparar: (a) qwen-Cline sin retrieval, (b) + `search_codebase` nativo, (c) + LocAgent-TS MCP, (d) + Serena. → responde "¿valió la pena vs Serena?" con datos.

## Verificación end-to-end

1. `python -m dependency_graph.ts_build_graph --repo C:/Users/joz/orca/workspaces/miro-clone/bichir` → grafo networkx sin excepciones; `Board.tsx` produce N nodos función (incluye el componente de la toolbar) con line-ranges correctos.
2. Spot-check: nodo del componente de la barra tiene aristas `renders` entrantes de su contenedor + `imports` correctas.
3. MCP: `search_code_entities("toolbar")` → el componente correcto; `get_entity(id, "full")` → ~150 líneas, no 7.565.
4. Cline + qwen con el MCP: "estandarizá los estilos de la barra" → llama `search_code_entities` → `get_entity` → edita el trozo acotado, sin `read_files` del archivo entero, sin `cd ..`.
5. Fase 4: acc@5 a nivel archivo de (c) > (a) y (b); comparar con (d).

## Riesgos / caveats

- **`invokes` es heurístico por nombre** (sin tipos) — más ruidoso en TS. v2 híbrida posible: MCP llama a `tsserver` para `references`, tree-sitter para estructura.
- **Prompts de LocAgent** (`util/prompts/*.j2`) mencionan idioms Python → edición ligera.
- **Repo research, no librería mantenida** → asperezas de setup.
- **Barrels/re-exports y monorepos**: cola larga más allá del v1.
- Esfuerzo: **~1 semana v1** (Fases 0-3), +1-2 días el eval.

## Archivos

**Nuevos:** `dependency_graph/ts_build_graph.py`, `dependency_graph/queries/typescript.scm`, `dependency_graph/queries/tsx.scm`, `dependency_graph/ts_resolver.py`, `dependency_graph/ts_bm25.py`, `plugins/location_tools/utils/compress_file_ts.py`, `locagent_mcp.py`, `requirements-ts.txt`, `NOTICE`, `eval/miro_clone_localization.jsonl`

**Modificados:** `dependency_graph/build_graph.py` (`VALID_EDGE_TYPES` += `renders`, dispatch `--language` opcional), `README.md`, `util/prompts/*.j2` (ligero), `~/.cline/data/settings/cline_mcp_settings.json`

**Reutilizados sin cambios:** `dependency_graph/traverse_graph.py`, `plugins/location_tools/retriever/fuzzy_retriever.py`, `repo_index/codeblocks/parser/*` (referencia), `evaluation/eval_metric.py`

## Referencias

- https://github.com/gersteinlab/LocAgent · Paper https://arxiv.org/abs/2503.09089 (ACL 2025)
- `tree_sitter_languages.get_language('typescript' | 'tsx')` (ya en deps)
