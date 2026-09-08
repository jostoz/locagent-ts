# Usar LocAgent-TS desde OMP (oh-my-pi / omp.sh)

`locagent_mcp.py` es un servidor MCP stdio con 4 tools:
`search_code_entities`, `get_entity`, `traverse`, `get_repo_overview`.
OMP lo descubre por `.omp/mcp.json`.

## 1. Instalar OMP (una vez, Windows)

```powershell
irm https://omp.sh/install.ps1 | iex
omp --version
```

Binario prebuilt `win32-x64`, nativo, sin WSL. No necesita `bun`
(el instalador `bun install -g @oh-my-pi/pi-coding-agent` sí requiere bun ≥ 1.3.14).

## 2. Cablear un proyecto

En la raíz del proyecto TS/TSX donde vas a trabajar:

```
<proyecto>/
  .omp/
    mcp.json     <- copia de docs/omp/mcp.json, con LOCAGENT_REPO y el slug puestos
    RULES.md     <- copia de docs/omp/RULES.md
```

En `mcp.json` reemplazá:
- `<RUTA_ABSOLUTA_DEL_PROYECTO>` → la raíz del proyecto (la misma en la que corrés `omp`).
- `<slug>` en `LOCAGENT_CACHE_DIR` → un nombre corto único (ej. `miro-clone`). El cache
  vive fuera del proyecto para no ensuciarlo.

`timeout: 120000` cubre el build del grafo en el primer `tools/call` (lazy); en repos
chicos son ~0.5 s, pero el default de OMP es 30 s.

## 3. Usar

```powershell
cd <proyecto>
git switch -c <rama>       # trabajá en una rama, no en detached HEAD
omp                        # sesión interactiva
```

Dentro de la sesión:
- `/mcp` — ver `locagent` conectado + las 4 tools.
- `/mcp reload` — reconstruir el grafo si agregaste/moviste archivos.
- Escribís la tarea en lenguaje natural; la regla `.omp/RULES.md` encadena
  `locagent` (localizar) → `lsp` (verificar tipos) → `edit` hashline (parche acotado).

## Notas

- OMP también auto-descubre `.cline/mcp.json` y `.claude/mcp.json` de un proyecto,
  pero se prefiere el `.omp/mcp.json` explícito (gana en prioridad, sin ambigüedad).
- Las rutas en `mcp.json` son absolutas de esta máquina (apuntan al `.venv` de
  `locagent-ts`). Si el `.omp/` se commitea a un repo compartido, cada dev ajusta esas
  rutas o usa un `.omp/mcp.json` local no versionado.
- Namespacing de las tools en OMP: puede ser `locagent/<tool>` o `locagent__<tool>`;
  confirmá con `/mcp` y ajustá `RULES.md` si hace falta.
