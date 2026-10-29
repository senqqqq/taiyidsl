## Change

Describe the problem and resulting behavior. Link related issues if applicable.

## Verification

- [ ] `uv run --locked ruff check .`
- [ ] `uv run --locked ruff format --check .`
- [ ] `uv run --locked pytest --basetemp=.test-tmp-pytest`
- [ ] `uv build --no-sources`

For compiler or exporter changes, describe event/timing equivalence checks and
any intentionally changed behavior. For docs-only changes, state which checks
were relevant instead of claiming unrun checks passed.

## Hardware and compatibility

Record affected units, timing contracts, result layouts, or configuration schemas.
Separate offline verification from real ARTIQ compilation and hardware testing;
mark unperformed checks as not verified.
