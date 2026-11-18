## Change

Describe the problem and resulting behavior. Link related issues if applicable.

## Verification

- Lint/format results for the published source set:
- Focused behavioral checks and why they cover this change:
- Package installation/build checks, if packaging changed:
- Tutorial execution, if the authoring examples or their APIs changed:

Keep full offline checks in CI. Do not run the whole local suite merely to fill
this template; report checks actually run and any relevant omissions.

For compiler or exporter changes, describe event/timing equivalence checks and
any intentionally changed behavior. For docs-only changes, state which checks
were relevant instead of claiming unrun checks passed.

## Hardware and compatibility

Record affected units, timing contracts, result layouts, or configuration schemas.
Separate offline verification from real ARTIQ compilation and hardware testing;
mark unperformed checks as not verified.
