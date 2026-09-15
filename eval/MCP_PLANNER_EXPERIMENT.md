# Ablación: acceso MCP del planeador

## Pregunta

¿El planeador mejora cuando puede consultar LocAgent directamente, además de recibir el mapa estático?

## Condiciones

1. **Static planner**: Qwen `qwen3.8-27b` recibe tarea + `miro_map.txt`; no tiene MCP.
2. **MCP planner**: el mismo Qwen recibe tarea + mapa y puede usar únicamente `graph_search`, `graph_get` y relaciones de LocAgent; no puede editar.

En ambas condiciones el plan se entrega al mismo ejecutor Qwen dentro del sandbox con LocAgent MCP habilitado. El commit, corpus, prompts de ejecución y límites de GPU permanecen iguales.

## Métricas

- validez del plan y número de pasos;
- llamadas MCP, latencia y tokens del planeador;
- recall de archivos y entidades del plan antes de ejecutar;
- exactitud final y recall del ejecutor;
- aceptación de las seis ediciones;
- fallos por timeout, respuesta vacía o plan inválido.

## Control

El planeador MCP debe usar un directorio de datos y worktree de sólo lectura. Su transcript se guarda separado del transcript del ejecutor. La clave de DeepSeek no se entrega a ningún proceso local; esta ablación usa Qwen en ambos roles. El planificador no puede modificar el repositorio ni consultar la solución esperada.

## Interpretación

Si MCP mejora el recall del plan y mantiene el coste acotado, LocAgent aporta valor también antes de la ejecución. Si sólo aumenta llamadas y latencia sin mejorar recall o exactitud, el mapa estático es suficiente para planificar.
