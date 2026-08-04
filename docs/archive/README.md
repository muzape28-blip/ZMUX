# Documentation Archive Staging

This directory is reserved for historical ZABAWHEELS/ZMUX reports once their
links and current-doc references have been cleaned up.

During the Alpine-first cleanup, docs are first labeled in place rather than moved.
That avoids breaking links from issues, changelog entries, workflow logs,
and previous release notes. Move a document here only when:

1. It is no longer current user/developer guidance.
2. `docs/README.md` has a replacement pointer.
3. Tests and workflow references have been updated.
4. The move does not obscure useful debugging history.

Suggested future layout:

```text
docs/archive/
  zabawheels/       # wheelhouse/package-pipeline era
  reports/          # device/debug/capability snapshots
  research/         # reference-mining and design analysis
```

Do not use this directory as a trash can. If a document still explains an active
invariant, update and keep it in `docs/` instead.
