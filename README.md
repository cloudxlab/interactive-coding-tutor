# Interactive Python Coding Tutor

Chat tutor (left) + JupyterLab (right). The tutor asks about your Python level, then serves one problem at a time
with auto-generated test cases (validated against a reference solution before you see them). Solve it in the
notebook, click **I've solved it** to run the tests. Pass → harder problem; stuck → easier one.
`beginning_knowledge` is stored once; `current_knowledge` is updated after every attempt (see "My knowledge").

## Run
    ./setup.sh                      # once
    ./launch.sh                     # opens http://localhost:8765

LLM backend (auto-detected in this order, or force with `TUTOR_LLM=anthropic|openai|claude-cli`):
`ANTHROPIC_API_KEY` → `OPENAI_API_KEY` (needs `pip install openai`) → the `claude` command (uses your Claude Code login, no key).

Env: `TUTOR_PORT` (8765), `TUTOR_JUPYTER_PORT` (8766), `TUTOR_MODEL` (optional model override).
Progress is saved in `state.json`; notebooks live in `workspace/`. Jupyter autosaves every 5s, and tests run on
the saved notebook (all code cells).
Note: your code runs locally in a subprocess with no sandboxing — this is a single-user local tool.
