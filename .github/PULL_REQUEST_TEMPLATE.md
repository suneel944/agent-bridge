## Problem and result

Describe the concrete trigger and the resulting behavior. Link the issue.

## Verification

State the exact checks run and their results. Distinguish local protocol tests
from live native-agent sessions. Include measured evidence for performance claims.

## Compatibility and risks

Describe state/schema changes, migration or rollback needs, and remaining limits.

- [ ] Scope is focused and existing work is preserved.
- [ ] Authentication, ownership and permission boundaries are retained.
- [ ] Context budgets and concurrency behavior are covered where affected.
- [ ] Documentation and changelog reflect the final change.
- [ ] `make check` passes; no checks or hooks were bypassed.
- [ ] No credentials, private messages, or generated state are included.
