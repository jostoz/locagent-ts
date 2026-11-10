"""Runner-side state carrier for TASK-003, backed by the GESTADO runtime.

TASK-003 acceptance criterion (3) requires the executor's inter-step state to
be managed by the GESTADO runtime (deterministic merge
``Sigma(t+1) = Sigma(t) xor DeltaSigma(t)``, no unbounded accumulated
transcript) instead of the ad-hoc ``carry.json`` list-of-dicts that
``eval/render_step.py`` builds for the Stage-1/2 planner.

GESTADO lives in a sibling workspace
(``C:/Users/joz/orca/workspaces/Gestado/cabezon``, overridable with
``$GESTADO_ROOT``) and is not a dependency of this repo, so it is imported at
runtime by adding its root to ``sys.path`` -- never vendored, never copied in.
The fusion primitive this module reuses verbatim is
``gestado.stateful.merge_state``: a deep merge where an explicit ``null``
deletes a key and an omitted key leaves it untouched -- exactly
Sigma(t+1) = Sigma(t) (deep-merge) DeltaSigma(t).

This runner does NOT wrap Cline itself in GESTADO's ``AgentRuntime`` -- Cline
is an external, already-agentic CLI process with its own internal tool loop,
not a client of GESTADO's (state, observation) -> (patch, action) contract.
What GESTADO replaces here is the *runner's own* inter-step bookkeeping: the
four sequential phases each arm goes through (init -> localized -> edited ->
validated) are folded through ``merge_state`` exactly like an
``AgentRuntime`` step would, instead of appending to an unbounded transcript
list. Sigma(t) for this experiment is a small, fully-serializable dict (see
``INITIAL_SIGMA`` below); the final Sigma(t) for each arm/run is written
verbatim into that arm's result artifact under ``gestado_sigma_final`` --
that is the durable evidence of "how the runner invokes GESTADO and what
shape Sigma(t) has" required by TASK-003.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

GESTADO_ROOT = Path(os.environ.get(
    'GESTADO_ROOT', r'C:/Users/joz/orca/workspaces/Gestado/cabezon')).resolve()
if str(GESTADO_ROOT) not in sys.path:
    sys.path.insert(0, str(GESTADO_ROOT))

from gestado.stateful import merge_state  # noqa: E402  (path patched above)

SIGMA_SCHEMA_VERSION = 'task-003-heldout-runner/v1'


def initial_sigma(*, run_id: str, arm: str, record_id: str) -> Dict[str, Any]:
    """Sigma(0) for one arm of one run. Every field below is either set once
    here or only ever touched by a DeltaSigma passed to :func:`advance`."""
    return {
        'schema': SIGMA_SCHEMA_VERSION,
        'run_id': run_id,
        'arm': arm,
        'record_id': record_id,
        'phase': 'init',
        'canonical_package': [],
        'canonical_package_bytes': 0,
        'guard_rejections': [],
        'guard_rejection_count': 0,
        'edit_applied': None,
        'acceptance_ok': None,
        'acceptance': [],
        'wall_time_s': {},
        'tokens': {},
    }


def advance(sigma: Dict[str, Any], delta: Dict[str, Any]) -> Dict[str, Any]:
    """Sigma(t+1) = Sigma(t) xor DeltaSigma(t) via GESTADO's deterministic
    deep merge. ``delta`` follows GESTADO's patch convention: an omitted key
    leaves the current value untouched, an explicit ``None`` deletes the key
    (not used by this runner -- every phase only ever adds/replaces fields)."""
    return merge_state(sigma, delta)
