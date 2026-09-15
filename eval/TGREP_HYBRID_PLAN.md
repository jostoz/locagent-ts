# Plan de evaluación: tgrep + LocAgent

## Objetivo

Determinar si tgrep reduce la latencia y el contexto de la exploración léxica sin
perder la cobertura estructural que produjo el mejor resultado de localización de
LocAgent (12/12 respuestas; recall@10 de archivo y entidad 0.9583).

tgrep no reemplaza el grafo. El router elige la herramienta según la operación:

- literal, regex o enumeración exhaustiva de una expresión: tgrep;
- intención, símbolo incierto o búsqueda conceptual: `graph_search`;
- contenido delimitado de una entidad: `graph_get`;
- callers, imports, renders y dependencias: `graph_traverse`;
- verificación inmediatamente posterior a una edición: tgrep `--no-index`.

## Etapa 1: viabilidad y fidelidad

1. Instalar una versión fijada de microsoft/tgrep y registrar versión, plataforma y opciones del índice.
2. Indexar un checkout inmutable y añadir `.tgrep/` al ignore del entorno de prueba.
3. Comparar tgrep y ripgrep sobre el mismo conjunto de consultas literales y regex.
4. Exigir igualdad de archivos y líneas encontradas. Registrar por separado diferencias por UTF-8 inválido, límite de 64 MiB, reglas ignore o regex no soportado.
5. Medir construcción inicial, tamaño del índice, RAM privada, consulta fría y caliente.

## Etapa 2: matriz de agentes

Ejecutar el mismo modelo, prompts, commit, sandbox y corpus bajo cuatro condiciones:

1. `rg`: búsqueda léxica sin índice y sin LocAgent.
2. `tgrep`: búsqueda léxica indexada y sin LocAgent.
3. `locagent`: herramientas `graph_*`, sin tgrep.
4. `hybrid`: router tgrep + `graph_*`.

Separar tareas por clase antes de mirar resultados: literal/regex, navegación
estructural, intención ambigua y tareas mixtas. Medir exactitud final, recall@k de
archivo/entidad, tiempo total, tiempo de herramientas, llamadas por herramienta,
archivos/líneas devueltos, tokens, flakes y frescura después de una edición.

## Router v1

1. Identificador, cadena o regex explícita: tgrep con `-F` cuando aplique, primero `-l` y después contexto limitado en los archivos candidatos.
2. Conducta o intención: `graph_search`.
3. Relaciones: `graph_traverse`; no reconstruir callers con texto salvo fallback.
4. Convención a nivel valor: tgrep enumera checks exactos y `graph_get` lee las entidades relevantes.
5. Máximo 4 consultas por capa antes de sintetizar o cambiar de estrategia. qC1 demostró que este límite evita exploración improductiva.

## Gate de adopción

Integrar tgrep en el ejecutor y Scout sólo si la condición híbrida:

- conserva 12/12 respuestas correctas y recall@10 no inferior a LocAgent por más de 0.02;
- reduce al menos 20% la latencia de herramienta o 15% el tiempo total en tareas léxicas;
- reduce llamadas o tokens en tareas mixtas;
- pasa la prueba de frescura tras crear, modificar y eliminar archivos;
- expone el fallback a escaneo y trata exit code 1 como “sin coincidencias”.

Miro-clone sólo valida integración y corrección. La afirmación de rendimiento requiere
además un repositorio de al menos 30,000 archivos y otro de 100,000 o más, porque el
margen depende fuertemente del tamaño, la plataforma y el volumen de coincidencias.

## Integración posterior al gate

Exponer una herramienta de sólo lectura con flags permitidos, ruta canonicalizada dentro
del repo, salida JSON y límites de resultados. Supervisar `tgrep serve` fuera del proceso
del agente y registrar si cada consulta usó servidor, índice local o escaneo. Mantener
LocAgent como autoridad estructural y tgrep como acelerador léxico.

Referencias: https://github.com/microsoft/tgrep y
https://github.com/microsoft/tgrep/blob/main/AGENTS.md.
