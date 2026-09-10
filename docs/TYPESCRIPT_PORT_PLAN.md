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
   - **v2 (2026-09-09):** `renders` lleva `jsx_lines` + `sites` (`{line, props}`, `props` = prop→expresión bindeada colapsada) e `invokes` lleva `call_lines`. Query captura `@jsx.element` (nombre + atributos). `traverse` los muestra inline (`@L<líneas> {prop=expr}`). Cierra el gap de "el grafo sabe A→B pero no dónde ni con qué handler".

## Fase 2 — Índice propio + reuso (½-1 día)  ✅

**Estado (2026-09-08): completo, verificado sobre miro-clone.**

- ✅ **`dependency_graph/ts_bm25.py`** — `bm25s` + PyStemmer, 1 doc por nodo file/class/function
  (nid + `_split_identifiers` camelCase/snake/kebab + skeleton + `code` capado a 4k chars). Excluye
  tests. `TsBM25Index.from_graph(g)` → `.retrieve(q, k, search_scope, include_files, return_scores)`
  + `.save()`/`.load()` (usa `bm25s.BM25.save/load` + `ts_index.json`). Indexa 672 nodos en ~0.45s.
  Queries de prueba ("selection toolbar styling", "z order bring to front", "export deck to pptx",
  "crop image overlay") devuelven la entidad correcta en top-3. Mucho mejor que fuzzy solo.
- ✅ **`traverse_graph.py`** reusado con 2 ajustes mínimos (documentados en NOTICE):
  - `is_test_file`: agrega convención JS/TS (`*.test.*`, `*.spec.*`, `__tests__/`, `__mocks__/`,
    `*.stories.*`). Antes `Board.test.tsx` NO se detectaba como test → tests contaminaban el retrieval.
  - `RepoEntitySearcher.global_name_dict[_lowercase]`: `nid.endswith('.py')` → check por
    `type == NODE_TYPE_FILE` (indexa basename + stem de cualquier extensión). Refactor a
    `_build_name_dict(lowercase)` compartido. Comportamiento Python idéntico.
  - `traverse_graph_structure` (encode `pydot`) necesita `pydot` (no está en requirements-ts) → el
    MCP usará `traverse_tree_structure` / `traverse_json_structure` (sin deps extra), que además son
    mejor input para un LLM.
- ✅ **`fuzzy_retriever.py`** reusado **tal cual** (verificado: corre sobre el grafo TS sin cambios).
- ✅ **`plugins/location_tools/utils/compress_file_ts.py`** — skeletonizer tree-sitter: reemplaza cada
  `statement_block` de función/método/arrow por `{ ... }` vía splice de bytes (shallowest-only, no
  recursa en cuerpos ya elididos). Mantiene imports, JSDoc, firmas, tipos, top-level. API:
  `get_skeleton(raw_code, keep_constant=True, language=None, filename=None)`. Board.tsx: 7566 → 695
  líneas en 33ms. El `compress_file.py` original (libcst) queda para Python.
- ✅ **`plugins/__init__.py` + `plugins/location_tools/__init__.py`** — el import del harness
  (`locationtools` → `repo_ops` → `bm25_retriever` → `llama_index`) va en `try/except
  ModuleNotFoundError` para que los módulos hoja bajo `plugins/` sean importables en el runtime mínimo.

## Fase 3 — Servidor MCP (½-1 día)  ✅

**Estado (2026-09-08): `locagent_mcp.py` funcional, probado sobre miro-clone vía protocolo stdio real.**

- ✅ `locagent_mcp.py` con `mcp==1.13.1` (`FastMCP`, transport `stdio`). 4 tools:
  - `search_code_entities(query, max_results=10, scope="all"|"function"|"class"|"file")` →
    **fusión RRF** de `ts_bm25` + `fuzzy_retriever` → lista con id, tipo, `file:line`, firma corta,
    flag `[component]`.
  - `get_entity(entity_id, mode="skeleton"|"full")` → skeleton (attr del nodo, o `compress_file_ts`
    para files) o código con números de línea. `full` sobre entidad > 400 líneas devuelve skeleton +
    aviso (Board = 6325 líneas → no se inlinea). `_resolve_id` tolera nombre pelado / falta de prefijo
    de dir / `file:Entity` sin path / `Clase.metodo`.
  - `traverse(entity_id, edge_types?, direction="both"|"downstream"|"upstream", hops=2)` →
    `traverse_tree_structure` (árbol indentado; **sin `pydot`**). Cap 6k chars.
  - `get_repo_overview(max_depth=3)` → árbol de directorios/archivos (con nº de entidades por file) +
    conteos de nodos/aristas.
- ✅ Confinamiento: build sólo de `REPO` (= `cwd` o `$LOCAGENT_REPO`). `os.walk` poda `SKIP_DIRS` +
  dotdirs. Cero `..`.
- ✅ Cache: `<repo>/.locagent/` (graph.pkl + bm25/ + `signature`). `$LOCAGENT_CACHE_DIR` la reubica
  (para árboles read-only — p.ej. el repo de prueba). Firma = sha256 de (path|mtime|size) de todos
  los fuentes + `_CACHE_SCHEMA`; si no coincide → rebuild. Repo no-escribible → corre sin cache.
- ✅ **Pureza de stdout**: `_protect_stdout()` (ctx mgr, swap `sys.stdout`→`stderr`) envuelve el build
  y las llamadas a `bm25s` en request-time — el log "Building index..." de `bm25s` no corrompe el
  JSON-RPC. `logging.getLogger('bm25s')` a WARNING.
- ✅ **NO** usar `from __future__ import annotations` en el server: `mcp` 1.13.x introspecciona
  `param.annotation` cruda y explota con genéricos stringificados (`Optional[List[str]]`).
- ✅ `~/.cline/data/settings/cline_mcp_settings.json` corregido (había `command: "python"` +
  `args: ["/ruta/a/LocAgent/locagent_mcp.py"]` — placeholder muerto). Ahora: `command` = el
  `python.exe` del `.venv`, `args` = ruta real del script, `cwd` = repo objetivo, `env` con
  `LOCAGENT_CACHE_DIR` (fuera del repo read-only) + `PYTHONWARNINGS/PYTHONIOENCODING`.
  Backup en `cline_mcp_settings.json.bak-locagent`.

Verificación (cliente stdio mínimo): `initialize` + `tools/list` + `tools/call` ×4 → todo JSON-RPC
limpio en stdout; `search_code_entities("z order bring to front")` → `zorder.ts:reorder` +
`BringToFrontIcon`; `traverse(LayerButtons, upstream)` → `renders-by ShapeFormatToolbar/
ImageFormatToolbar`; warm start recarga cache sin rebuild.

## Fase 4 — Paper: estudio empírico (DIFERIDA — no ahora)

**Decisión (2026-09-08):** la Fase 4 **no se ejecuta todavía**. Cuando se retome, se
materializa como un **paper de estudio empírico** (ambición: conferencia), no como un
script de eval suelto. Las Fases 0–3 (el sistema) ya están completas y commiteadas.

**Pregunta de investigación:** ¿el retrieval guiado por grafo (LocAgent-TS MCP) mejora la
localización de código para un agente con ventana de contexto chica (qwen-2.5-coder-32B en
Cline) sobre un codebase TS/React con archivos gigantes, frente a: sin retrieval, la
búsqueda nativa del agente, y retrieval por LSP (Serena)?

**Condiciones:** (a) qwen-Cline sin retrieval · (b) + `search_codebase` nativo · (c) +
LocAgent-TS MCP · (d) + Serena.
**Ablations de (c):** sin edge `renders`; sin `invokes`; solo BM25 vs BM25 + fuzzy + `traverse`.

**Métricas:** acc@k y recall@k a nivel archivo y a nivel función/entidad, k ∈ {1, 3, 5, 10}.
Nota: reimplementar acc@k / recall@k **sin `torch`** — las de `evaluation/eval_metric.py`
importan `torch`, que no está en `requirements-ts.txt`; numpy puro, ~30 líneas.

**Corpus:** solo miro-clone. `eval/miro_clone_localization.jsonl` — 25-30 tareas etiquetadas
a mano sobre `C:/Users/joz/orca/workspaces/miro-clone/bichir` (toolbar, sticky notes,
z-order, slide layouts, export PPTX, crop, frames, AI chat). Cada caso: prompt de tarea +
conjunto ground-truth de archivos/entidades relevantes. Encuadrado como **case study a
fondo**, no como benchmark multi-repo.

**Amenazas a la validez (a documentar en el paper):** un solo repo → validez externa
limitada (riesgo de revisor: señalarlo explícito y encuadrarlo como case study);
`invokes` / `renders` son heurísticos por nombre (sin tipos); el etiquetado lo hace el
autor → mitigar con un criterio de relevancia escrito y, de ser posible, doble etiquetado.

**Artefactos cuando se retome:** `docs/PAPER.md` (o LaTeX), `eval/` (harness + `.jsonl` +
métrica torch-free + runner), tablas/figuras. Related work a cubrir: LocAgent (ACL 2025),
Serena / agentes sobre LSP, Aider repo-map, herramientas de grafo de código sobre tree-sitter.

**Esfuerzo estimado:** etiquetado + harness ~2-3 días; corridas de las 4 condiciones +
ablations ~2 días; redacción ~1 semana.

## Verificación end-to-end

1. `python -m dependency_graph.ts_build_graph --repo C:/Users/joz/orca/workspaces/miro-clone/bichir` → grafo networkx sin excepciones; `Board.tsx` produce N nodos función (incluye el componente de la toolbar) con line-ranges correctos.
2. Spot-check: nodo del componente de la barra tiene aristas `renders` entrantes de su contenedor + `imports` correctas.
3. MCP: `search_code_entities("toolbar")` → el componente correcto; `get_entity(id, "full")` → ~150 líneas, no 7.565.
4. Cline + qwen con el MCP: "estandarizá los estilos de la barra" → llama `search_code_entities` → `get_entity` → edita el trozo acotado, sin `read_files` del archivo entero, sin `cd ..`.
5. (diferida) Fase 4: acc@5 a nivel archivo de (c) > (a) y (b); comparar con (d).

## Integración con clientes MCP

`locagent_mcp.py` es un server MCP stdio genérico — mismo binario para cualquier
cliente. Confinado a `LOCAGENT_REPO` (o `cwd`); cache en `LOCAGENT_CACHE_DIR` (o
`<repo>/.locagent/`).

- **Cline** — `~/.cline/data/settings/cline_mcp_settings.json` (hecho, Fase 3):
  `command` = el `python.exe` del `.venv`, `args` = ruta del script, `cwd` = repo
  objetivo, `env` con `LOCAGENT_CACHE_DIR`. Backup en `.bak-locagent`.

- **OMP** (oh-my-pi, `omp.sh`) — `.omp/mcp.json` project-level en la raíz del repo.
  Plantillas y pasos en [`docs/omp/`](omp/). Puntos: `timeout: 120000` (el primer
  `tools/call` construye el grafo, lazy; default OMP = 30 s); regla sticky
  `.omp/RULES.md` que encadena `locagent` (localizar) → `lsp` (verificar tipos) →
  `edit` hashline; `/mcp reload` tras agregar archivos. Cableado real:
  `C:/Users/joz/Documents/miro-clone` (Vite+React+TS, `Board.tsx` 9k líneas).
  Instalación: `irm https://omp.sh/install.ps1 | iex` (binario nativo win32-x64).
  OMP también auto-descubre `.cline/` y `.claude/` de un repo, pero gana el `.omp/`
  explícito.

- **Claude Code** — `.mcp.json` en la raíz del proyecto, o
  `claude mcp add locagent -e LOCAGENT_REPO=<repo> -- <venv python> locagent_mcp.py`.
  Tools namespaced `mcp__locagent__*`. (El mismo `.mcp.json` lo hereda OMP.)

## Piloto informal — Cline + modelo local (2026-09-09)

Primer dato empírico del stack completo: **Cline CLI + `qwen/qwen3.5-9b`** (local,
LM Studio, 65k ctx) + `locagent` MCP sobre `Documents/miro-clone` @ `origin/main`
(`Board.tsx` 7.5k líneas). 7 prompts, uno por turno.

**Resultado: 8/8 localizaciones correctas**, todas con tool calls nativos, sin
basura. Ej.: "z-order de las shapes" → `zorder.ts:reorder` + wrappers por tipo +
`Board.applyZOrder` + `LayerButtons`; "mapear llamadores de `reorder` antes de
tocar" → mapa de impacto completo, se detuvo a preguntar la firma nueva;
"traverse renders desde Board" → árbol de componentes de 4 hops.

**Primer A/B medido (b) vs (c)** — mismo modelo, mismo prompt exacto
(`traverse upstream renders sobre LayerButtons`):

| | tool calls | contexto | resultado |
|---|---|---|---|
| **con `locagent`** | **1** (`traverse`) | **~5k tokens** | `Board → {ShapeFormatToolbar, ImageFormatToolbar} → LayerButtons`, completo |
| **sin** (solo grep/read de Cline) | **~25** (10+ reads, 10+ searches, 3 comandos PowerShell fallidos) | **~60k tokens** (leyó ~5000 líneas de `Board.tsx` en chunks) | mismo, por el camino largo |

**12× menos contexto, 25× menos tool calls, misma respuesta.** Para un modelo de
65k de ventana: 5k usados = ~9 turnos de headroom; 60k = a un turno del
compaction. La eficiencia del grafo *es* la diferencia entre funcionar y
ahogarse en el archivo de 7.5k líneas — exactamente el problema que motivó el
proyecto. `tool_calls` y `context_tokens` son las primeras métricas duras para
la Fase 4 (falta acc@k con ground-truth).

**Segundo A/B medido (2026-09-09)** — prompt de wiring natural (no directiva):
*"which handler is connected to `AiChatPanel`'s `onAction`, and on which line is
it mounted?"*, Cline + qwen3.5-9b, misma pregunta en 3 condiciones:

| corrida | setup | tool calls | grep/read nativo | contexto |
|---|---|---|---|---|
| 1 | MCP, sin rule | 1 `search` + ~11 nativos | 5 read + 6 search | 18.6k |
| 2 | MCP + `.clinerules`, pre-fix | 3 `search` + `get_entity` + `traverse`→∅ | ~10 `Select-String` | 27.6k |
| 3 | MCP + `.clinerules` + fixes `64dc9ad` | **1 `search` + 1 `traverse`** | **0** | **7.6k** |

Respuesta correcta en las 3 (`handleAiAction`, L6077). La corrida 3 es el camino
buscado: `search_code_entities` devuelve el componente #1, `traverse renders
upstream` da la respuesta en la anotación `@L6077 {onAction=handleAiAction}`. El
delta 1→3 (18.6k→7.6k, 12 calls→2) no es v2 solo: es v2 + ranking exact-match +
`traverse` sobre file + la rule que orienta al 9B al grafo. Sin *alguno* de esos
cuatro, el modelo chico vuelve a grep.

**Arnés — hallazgo clave:** OMP volteó a los modelos chicos con su indirección
`xd://` para tools MCP (el 9b escribía JSON como texto, `write()` a paths,
loopeaba). Cline las expone directo (`mcp__locagent__*`) → el mismo 9b las usa
bien. El servidor MCP no cambió. Ver [[harness-matters-mcp-clients]] en memoria.

**Fixes que salieron del piloto** (todos commiteados):
- `084f4de` — `get_entity`/`traverse` aceptan `id` además de `entity_id`; entidad
  < 40 líneas → devuelve `full` (elidir no ahorra nada).
- `420b195` + `51cd828` — `get_entity` sobre un **file** (skeleton **o** full) da
  el *outline* del grafo (entidades top-level, ~60 líneas), no el skeleton crudo
  (`Board.tsx`: 1375 → 16 líneas). Un file nunca se inlinea entero.
- `226e12a` — `traverse` `include_tests` (default True para `upstream`): los tests
  son lo que más rompe un cambio de firma; el docstring guía a
  `edge_types=["invokes","imports"]` upstream (`imports` engancha dependientes
  cuyas call sites no están en una entidad nombrada, p.ej. asserts en callbacks
  anónimos de `it()`).

**Gap sistémico (v2) — CERRADO 2026-09-09:** el grafo v1 no modelaba **binding de
props a nivel de sitio JSX** (`renders` decía "Board monta AiChatPanel" pero no
"`onAction` bindeada a `handleAiAction` en la línea N"; el modelo lo suplía con
grep+read nativo, runs 5/7/8).

v2 enriquece las aristas con el call site, extraído en tree-sitter (sin tipos):
- **`renders`** lleva `jsx_lines` (list[int]) + `sites` (`{line, props}` donde
  `props` = prop → fuente colapsada de la expresión bindeada; arrows se reducen a
  `(args) => …`). Query `tsx.scm` ahora captura `@jsx.element` entero (nombre +
  atributos), no solo `@jsx.name`.
- **`invokes`** lleva `call_lines` (list[int]).
- `traverse` los renderiza inline:
  `renders ── toolbars.tsx:ShapeFormatToolbar  @L189 {onLayer=onLayer, buttonStyle=iconButtonStyle(false)}`
  y en 2 hops se ve el prop-drill completo hasta `Board  @L6511 {… onLayer=applyZOrder}`.
- Schema de cache → `v2` (auto-invalida las caches v1).

Verificado sobre `Documents/miro-clone`: build 0.5 s, 142 `renders` / 612
`invokes` con metadata; `traverse upstream renders` sobre `LayerButtons` da la
cadena `onLayer` con líneas en 1 llamada, sin fallback a grep.

Cola para v3: `renders` sigue siendo name-match (colisión `FrameIcon` en 2
archivos); resolución por import-scope como en `invokes`.

**Re-test v2 (2026-09-09) — dos footguns del arnés/tool, ambos arreglados
(`64dc9ad`):** corriendo *"which handler is wired to `AiChatPanel`'s `onAction`"*
con Cline + qwen3.5-9b, el modelo (a) recibió una lista de entidades inútil
—`search_code_entities("AiChatPanel", max=5)` rankeaba `SendIcon`/`nextMessageId`
por encima del componente `AiChatPanel`, que caía fuera del top 5— y (b) hizo
`traverse` sobre el **nodo file** (`AiChatPanel.tsx`), que devolvía *"no renders
neighbours"* porque `renders`/`invokes`/`inherits` cuelgan de entidades, no de
files. Sin respuesta del grafo → fallback a `Select-String` (10+ comandos).
Fixes: exact-name-match sube al top en `search_code_entities` (componentes
primero); `traverse` sobre un file expande a sus entidades top-level y recorre
cada una. **Lección para el paper:** el valor del grafo depende de que (1) el
retrieval devuelva el id correcto y (2) las tool signatures no tengan bordes que
manden al modelo chico de vuelta a grep. Sin la rule de Cline retrieval-first el
9B no llama a `traverse` en una pregunta natural de wiring —lo hace cuando el
prompt nombra la herramienta (`a5e6fa1`) o cuando una `.clinerules` lo obliga.

**Config recurrente que muerde:** LM Studio JIT auto-load recarga el modelo al
default (8192 ctx) si se descarga → todo revienta. Cargar explícito
(`lms load ... -c 65536`, sin `--ttl`) y desactivar JIT en la GUI.

## Riesgos / caveats

- **`invokes` es heurístico por nombre** (sin tipos) — más ruidoso en TS. v2 híbrida posible: MCP llama a `tsserver` para `references`, tree-sitter para estructura.
- **Prompts de LocAgent** (`util/prompts/*.j2`) mencionan idioms Python → edición ligera.
- **Repo research, no librería mantenida** → asperezas de setup.
- **Barrels/re-exports y monorepos**: cola larga más allá del v1.
- Esfuerzo: **~1 semana v1** (Fases 0-3) — **hecho y commiteado**. Lo que resta es el paper (Fase 4, diferida).

## Archivos

**Nuevos:** `dependency_graph/ts_build_graph.py`, `dependency_graph/queries/typescript.scm`, `dependency_graph/queries/tsx.scm`, `dependency_graph/ts_resolver.py`, `dependency_graph/ts_bm25.py`, `plugins/location_tools/utils/compress_file_ts.py`, `locagent_mcp.py`, `requirements-ts.txt`, `NOTICE`, `docs/omp/` (plantillas `mcp.json` + `RULES.md` + `README.md` para OMP), `eval/miro_clone_localization.jsonl` _(Fase 4, diferido)_

**Fuera del repo:** `C:/Users/joz/Documents/miro-clone/.omp/{mcp.json,RULES.md}` (config OMP del proyecto real, sin versionar acá)

**Modificados:** `dependency_graph/build_graph.py` (`VALID_EDGE_TYPES` += `renders`; `matplotlib` a import lazy), `dependency_graph/traverse_graph.py` (`is_test_file` convención JS/TS; `global_name_dict` no-`.py`), `plugins/__init__.py` + `plugins/location_tools/__init__.py` (harness import opcional), `README.md`, `util/prompts/*.j2` (ligero), `~/.cline/data/settings/cline_mcp_settings.json`

**Reutilizados sin cambios:** `plugins/location_tools/retriever/fuzzy_retriever.py`, `repo_index/codeblocks/parser/*` (referencia), `evaluation/eval_metric.py`

## Referencias

- https://github.com/gersteinlab/LocAgent · Paper https://arxiv.org/abs/2503.09089 (ACL 2025)
- `tree_sitter_languages.get_language('typescript' | 'tsx')` (ya en deps)
