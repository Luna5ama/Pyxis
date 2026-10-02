# Global constraints

- `{{PYXIS:CONSTRAINT_1}}`
- `{{PYXIS:CONSTRAINT_2}}`
- Preserve all unrelated and pre-existing user changes.
- Do not broaden the selected task's scope opportunistically.
- Follow the user's existing authorization for commits, deployment, runtime restarts and external writes; the loop itself grants no additional permission.
- Keep generated files synchronized with their maintained inputs when the selected task owns them.
- Keep raw logs, captures, screenshots, and binaries outside this roadmap unless they are intentional maintained artifacts; record stable paths and hashes instead.
