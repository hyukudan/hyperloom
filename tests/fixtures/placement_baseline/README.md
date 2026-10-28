# Placement legacy baselines

These sources are copied from main `8539c28`, the integrated placement base. Tests read them directly, so source
archives and depth-one checkouts run every comparison without git objects or history-dependent skips.

Each Python fixture contains only the named function sources and executes with the corresponding current module's
globals. The descriptor fixture preserves the complete module, including the legacy `Store` writer and imports.
This checks the writer's actual serialized bytes rather than duplicating its formatting in the test.

The session evaluator/picker, core call, facts gatherer, NVIDIA query, probe report, fidelity gate, accelerator
summary, recipe command runner and service deploy functions are unchanged from the earlier comparison bases.
The snapshot summary, CLI admission helper, CLI entry point and pinned deploy use main's integrated versions.
Portal log and mutation comparisons also use main's versions. Baseline updates require reviewing the changed
behavior; do not regenerate these fixtures from the implementation under test.
