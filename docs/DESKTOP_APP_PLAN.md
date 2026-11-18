# Desktop app plan

Goal: one installable desktop app over a real queue, not a demo.

Where things stand: the queue, threads, reply grammar, a desktop board
(pywebview or browser fallback, PyInstaller `make app`) and a tray/menu-bar
app are on `main`. The board only showed throwaway demo data.

1. **Board opens a real queue (this step).** `hitl_queue app` uses
   `--state-dir` (or `./state` from source, or the per-user data dir in packaged
   builds); `--demo` keeps the synthetic board. The tray's "Open app" opens the
   same state dir. The data-dir helper is shared in `paths.py`.
2. **One bundle (done).** `make bundle` builds `HITL Queue` with the board and
   tray extras. Launch starts the tray; "Open app" runs the same frozen
   executable with `app --state-dir ...` to open the board directly.
3. **First-run setup.** On an empty data dir, offer to create `agents.toml`
   from the example and pick a notifier; no secrets stored in the bundle.
4. **Create and edit tickets from the board,** not only approve/skip.
5. **Distribution.** App icon, codesigning and notarization for macOS, Windows
   installer, CI build artifacts. Needs Matt's decision on signing accounts.
