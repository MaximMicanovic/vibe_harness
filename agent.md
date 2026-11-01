<!-- vibe-harness:agent.md managed -->

# agent.md — rules for agents working in this project

This folder is a **vibe-harness** project. Dashboard cards are specs, and agent
sessions (implement / fix / review / merge-resolve) work in this folder.

Rules:

- **Always keep `route.md` up to date when relevant.** If your change affects
  how work is routed through this project or how tokens are spent — pipeline
  order, model/agent choice, test gates, tool allowlists, guardrails, cost or
  time caps — update `route.md` in this folder to reflect it (create the file
  if it is missing). Keep it short and factual.
- Run the project's test suite before finishing. The harness's test gate runs
  it too, and a red suite rejects the round back to a fixer.
- Work autonomously; when something is ambiguous, make a reasonable choice and
  record it in `NOTES.md`.
