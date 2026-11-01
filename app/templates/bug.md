Fix the following bug in the current directory.

Bug description:
<!-- Replace this with: what happens, what you expected to happen, and steps to reproduce. -->

Instructions:
- First, read the code and any existing tests to understand the intended behavior, and write down the suspected root cause in a NOTES.md file.
- Write a minimal failing test (or a small reproducer script) that demonstrates the bug before changing any code.
- Then fix the bug with the smallest change that makes the failing test pass.
- Run the full test suite before and after your change and make sure everything passes.
- Finish by listing the root cause, the fix, and how you verified it in NOTES.md.

Requirements:
- The reproducer you add must fail on the original code and pass after the fix.
- Keep the fix minimal: no refactors or unrelated changes.
- Do not change the external behavior beyond fixing the bug: existing entry points, CLI flags, and outputs must keep working.
