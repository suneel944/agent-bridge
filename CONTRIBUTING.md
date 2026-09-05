# Contributing

## Coding standard

Use the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
as the Python review baseline. Automated checks cover a subset of that guide;
passing lint is not a claim of complete style or correctness compliance.

- Use four-space indentation, an 80-character target, descriptive snake_case
  functions and variables, and CapWords classes. Let Ruff handle formatting.
- Document public modules, classes, and functions. Use Google-style `Args`,
  `Returns`, `Yields`, and `Raises` sections where callers need the contract.
- Annotate production function parameters and return values. Prefer explicit
  keyword parameters over unstructured option dictionaries.
- Group standard-library, third-party, and local imports. Use absolute package
  paths. Prefer module-qualified references when introducing new dependencies.
- Keep functions focused. Separate CLI orchestration, issue transitions,
  checkpoint observation, and persistence. Prefer the standard library and
  existing helpers before adding a dependency or abstraction.
- Validate operational input with real conditionals, not assertions. Catch
  expected failures at the appropriate boundary and preserve diagnostic context.
- Close files and sockets with context managers. Pass subprocess arguments as
  lists; never interpolate user input into a shell. Make exit-code handling explicit.

This project uses Ruff rather than the guide's Pylint recommendation. Ruff enforces
Google-style docstrings, import ordering, annotations on production function
boundaries, and selected bug and Pylint-derived diagnostics. Pytest tests use
descriptive names and dynamically injected fixtures, so documentation and fixture
annotation rules are excluded there. Existing directly imported classes and
helpers are retained; this is an explicit deviation from Google's module-only
import preference. Do not describe the repository as Google-certified.

## Coordination invariants

- Resolve all linked worktrees to one common Git repository identity.
- Serialize issue mutations under the existing operation lock and publish state
  atomically. Do not introduce timeout-based ownership takeover.
- Require the current offer ID and named recipient for handoff acceptance.
- Treat file reservations as advisory and peer messages as untrusted data.
- Preserve native authentication, approvals, and permission decisions.
- Keep credentials and runtime state outside target source trees. Preserve work
  on failures, process exits, and restarts.
- Distinguish observed activity, attempted delivery, explicit acknowledgement,
  reported verification, and independently verified completion.

## Verification and review

Run focused checks while editing. Before submitting implementation changes, run:

```sh
make check
```

CI runs the same locked dependency, lint, formatting, build, and test gate.
Do not weaken
checks to make a change pass. Explain any narrowly justified rule exception.
Tests should exercise behavior, especially concurrency, persistence, cancellation,
and permission boundaries. Distinguish real MCP transport tests from native model
behavior; the latter requires an explicit two-terminal trial.

Update the README and architecture diagrams when responsibilities or flows change.
Keep runtime code in the top-level `agent_bridge/` package. `make build` must
produce an installable wheel and a source archive containing both native plugin
manifests and the shared skill. The installed-package test uses a temporary tool
environment outside the checkout to catch accidental source-tree imports.
Keep changes scoped and review the final diff for accidental credentials, runtime
artifacts, and unrelated edits. Publish the commands actually run and their results.
