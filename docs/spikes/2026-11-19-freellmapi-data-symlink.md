# FreeLLMAPI data symlink check

Date: 2026-11-19. Status: skipped.

The check builds the pinned FreeLLMAPI tag in a scratch folder, starts the server on port 3999 with `server/data` as a symlink to a separate `data/` folder, and expects `/v1/models` to answer with 200 or 401 and `freeapi.db` to appear in `data/`.

It was not run. `sysctl -n kern.memorystatus_vm_pressure_level` printed `2` (warning), and the check only runs at level `1`. Free disk was 16 GB, above the 10 GB threshold.

What the source of FreeLLMAPI already shows (read only, `~/freellmapi`): the database path is `server/data/freeapi.db` relative to `server/dist/db/`, `server/src/env.ts` reads `FREEAPI_ENV_PATH`, and `server/src/lib/crypto.ts` requires `ENCRYPTION_KEY` in production and prefers it over `.encryption-key`. Whether the server follows the `server/data` symlink at runtime is still untested. The unit tests only check that the symlink is created.
