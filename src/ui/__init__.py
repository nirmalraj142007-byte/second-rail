"""Terminal demo surface for Second Rail — the screen a judge actually watches.

The web approval UI was cut on purpose (CLAUDE.md priority discipline); this
package is what replaced it. `src/ui/theme.py` is the one place colours and
glyphs live, `src/ui/live.py` is the scrolling `make demo` log, `src/ui/
approve.py` is the non-interactive JSON-queue fallback (`make approve`).
"""
