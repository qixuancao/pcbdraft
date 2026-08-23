# 迁移 BoardBench 测评产物到项目本地

## Goal

将现有 BoardBench campaign/report 测评产物复制到项目 artifacts/boardbench-local/，加入 .gitignore，保持本地可用且不纳入 Git/远端。

## Requirements

- Copy the existing BoardBench campaign directories from
  `/mnt/2T/pcbdraft-boardbench-campaigns/` into the repository-local
  `artifacts/boardbench-local/campaigns/`.
- Copy the private corpus/review inputs from
  `/mnt/2T/pcbdraft-boardbench-private/` into
  `artifacts/boardbench-local/private/` and reports from
  `/mnt/2T/pcbdraft-boardbench-reports/` into
  `artifacts/boardbench-local/reports/`.
- Exclude ephemeral `.pcbdraft-boardbench-locks/` files from the copy.
- Preserve the existing `/artifacts/` `.gitignore` rule (add a narrower rule
  only if that coverage changes) so these large/local/private files are not
  staged or pushed.
- Keep the external source directories unchanged; this task is a recoverable
  local copy, not deletion or remote publication.

## Acceptance Criteria

- [x] Local campaign, private, and report trees exist with matching file
      inventories and usable JSON artifacts.
- [x] `git check-ignore` confirms the whole local artifact tree is ignored by
      the existing `/artifacts/` rule, while the committed `artifacts/`
      directory remains otherwise usable.
- [x] No local BoardBench artifact is staged, committed, or pushed.
- [x] A focused size/inventory check completes without running the full test
      suite.

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
