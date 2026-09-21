# LiveNote Product And Development Rules

## Product principle

- Start from the user's desired outcome, not from the internal task model.
- The default path must be the shortest reliable path to that outcome.
- The system owns repetitive, deterministic, recoverable work; the user should not be a manual bridge between internal steps.
- Expose only decisions that require human judgment, approval, or authorization.

## LiveNote default flow

- Phone: bind identity once, record, save, and upload automatically.
- Computer: detect ready work, process it automatically, recover transient failures, and present only the review or exception state.
- During an active upload, the server may process durable contiguous Chunk windows automatically and expose only the real uploaded/recognized watermark; partial output is provisional and cannot be published.
- Administrator: review content, approve publication, and handle exceptional cases.
- Publishing and destructive cleanup remain explicit user decisions.

## Automation boundary

- Code owns task state transitions, retries, validation, result persistence, and recovery from recoverable failures.
- The normal UI must not require users to copy prompts, choose result JSON files, read local result directories, manage leases, release tasks, or understand worker-inbox/session internals.
- Internal controls may remain as compatibility or diagnostic capabilities, but they must stay out of the primary user flow.
- Each state should have one clear primary action; background work should surface progress and actionable failure recovery.

## Change discipline

- When a requested feature adds manual steps, first check whether the system can perform them safely in code.
- Prefer root-cause fixes and state-driven automation over UI instructions or repetitive operator procedures.
- Keep this file synchronized when the product workflow or automation boundary changes.

## UI design source of truth

- Frontend changes must follow `DESIGN.md`.
- `DESIGN.md` is the canonical source for UI hierarchy, interaction, state copy, responsive behavior, accessibility, and visual simplification.
- When a UI request conflicts with the product automation boundary, prefer implementing the automation in code; keep any temporary compatibility control out of the primary flow and document the exception.

## Summary writing source of truth

- When preparing a LiveNote summary from a transcript, use `.agents/skills/livenote-elder-friendly-summary/SKILL.md`.
- The primary content is Chinese livestream health Q&A involving folk remedies and personal experiences. Preserve attribution and uncertainty; never turn an anecdote or opinion into a proven treatment.
- User-facing summaries are for older readers: use plain spoken Chinese and do not mention the source format or internal processing terms.
