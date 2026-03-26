Watcher smoke marker: glacier-lantern-4821

Sudo-ID watcher smoke test

This file exists to verify that `brain watch` notices a new document, re-runs sync, and makes the new text searchable.

If retrieval is working, a query for the watcher smoke marker should return this file and the marker value above.

If watcher intelligence is working, `.codex_brain/watch_status.json` should also reflect the changed file, the watcher-related subsystem, and reviewer-facing follow-up questions after a save triggers sync.
