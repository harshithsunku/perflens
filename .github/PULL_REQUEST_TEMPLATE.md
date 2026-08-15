<!--
Thanks for contributing. CONTRIBUTING.md has the full setup; this is the
short version of what a reviewer will look for.
-->

## What this changes

<!-- And why. Link an issue if there is one. -->

## How it was verified

<!-- Which of these you actually ran, not which ones exist. -->

- [ ] `pytest tests/`
- [ ] `ruff check src/ tests/ tools/` and `mypy src/perflens`
- [ ] `npm --prefix frontend run typecheck` / `test` / `e2e` (frontend changes)
- [ ] Ran it and looked at the result

<!--
That last box carries more weight than it looks. Every defect the 0.8.0 and
0.9.0 stabilization passes fixed was found by running something and looking,
not by an assertion — including one where the committed fixtures agreed with
the bug.
-->

## Checklist

- [ ] If `api/models.py` or a route changed: re-ran `python tools/export_openapi.py`
      and `npm --prefix frontend run typegen` (CI diff-checks both)
- [ ] If the version changed: `python tools/check_version.py` passes, and the
      agent was rebuilt (`VERSION` is compiled into it)
- [ ] No IPs, hostnames, credentials or company-specific names in the diff

## Agent changes

<!-- Delete this section if agent-c/ is untouched. -->

`agent-c/` and the wire protocol are stable by default and change only by
explicit decision. If this touches them, say why here, note whether the wire
protocol changed, and confirm it was tested on real hardware — CI cross-compiles
five architectures but executes none of them.
