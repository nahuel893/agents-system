# PR template

## Scope
Implement GitHub issue #16 by adding the repository PR template and updating the repository delivery records that previously stated the template was missing.

## Tasks
- [x] Record scope and acceptance criteria.
- [x] Add `.github/pull_request_template.md` with the four delivery questions and short standing-rules checklist.
- [x] Update current-state delivery documentation to state the template exists and replace archive issue `#55` with `#16`.
- [x] Run required validation, commit, push, open the PR, and observe CI. (in progress: commit and PR delivery remain)

## Acceptance criteria
- The template uses the four questions verbatim from the delivery documentation.
- The checklist covers `Closes #NN`, EN/ES documentation twins, no AI attribution, and Guard 1 for predefined-role tool or permission changes.
- Current-state documentation no longer says the PR template is missing; the archive reference points to #16.
- Required validation commands pass and the PR is not merged.

## Evidence
- `ruff check .`: passed.
- `ruff format --check .`: passed (407 files already formatted).
- `mypy src/`: passed (73 source files; one existing unused configuration-section note).
- `pytest -q`: passed (2285 passed, 184 deselected, 17 xfailed; 7 warnings).
- No Spanish delivery-document twins exist under `docs/`.
