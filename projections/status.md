# Status — PAIOS

> Proyección derivada generada automáticamente.
> Autoridad: `state/` + `contracts/` + `decisions/`.

- Generado: 2026-09-21T05:47:07Z
- Script: `scripts/update_status.sh`

## Cola

| Estado | Cantidad |
|--------|----------|
| pending | 2 |
| in_progress | 0 |
| blocked | 0 |
| done | 1 |

### pending
- TASK-001-plan-step-to-paios-packet
- TASK-003-jev-vs-bm25-heldout-runner

### in_progress
- (vacío)

### blocked
- (vacío)

### done
- TASK-002-jev-semantic-search

## Últimos deltas (max 10)

- state/deltas/20260920T233235Z_TASK-002-jev-semantic-search_verifier.yaml
- state/deltas/20260920T232901Z_TASK-002-jev-semantic-search_builder.yaml
- state/deltas/20260920T204843Z_TASK-002-jev-semantic-search_builder.yaml

## Reviews

- TASK-002-jev-semantic-search_review.md

## Verifications recientes

- TASK-002-jev-semantic-search_20260920T233235Z.yaml

## Next actions sugeridas

1. Ejecutar `./scripts/queue.sh` para reconciliar.
2. Si hay pending: claim + trabajo + delta + dual review + verify.
3. Si hay blocked: revisar unresolved y escalar.
4. Regenerar esta proyección después de cada transición relevante.

---
Regenerar: `./scripts/update_status.sh`
