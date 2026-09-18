# Acceptance Criteria — Genéricos

Una transición de estado se acepta solo si se cumplen TODOS:

1. `./scripts/judge.sh current` retorna PASS
2. Existe `state/reviews/<task>_review.md`, no vacía
3. El hash de zonas protegidas (`contracts/`, `decisions/`, `AGENTS.md`,
   `scripts/judge.sh`, `scripts/verify.sh`) no cambió desde el claim — o,
   si cambió, existe una aprobación humana registrada con
   `scripts/approve_gate.sh` (ver contracts/action-gating.md)
4. Existe al menos un state delta válido para la tarea
5. `next_action` está definido y es coherente
6. No hay `TODO(agent)` de bloqueo abierto sin escalamiento
