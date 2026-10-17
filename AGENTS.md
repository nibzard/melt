# Melt development

- Read `SPEC.md` before implementation. It defines the `melt/v0.1` contract.
- Develop on Linux first. Use a command-line harness before native macOS integration.
- Implement the schema, compiler, workspace service, and Python execution loop first.
- Prove the sales workflow, revision checks, atomic publication, and restart recovery.
- Then implement `docs/rfcs/001-semantic-interactions.md` as a separate language extension.
- Keep v0.1 behavior unchanged when adding the extension. Require explicit migration.
- Keep compilation, execution, publication, and rendering separate.
- Use `note.md` for design context; `SPEC.md` takes precedence.
- Run checks relevant to each change. Do not claim untested integration works.
- Use `python3` for Python commands.
