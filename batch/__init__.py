"""
batch/
======
Headless batch optimization engine — independent of the Streamlit app and the
LLM chat loop. Imports only from `tools/` and the standard library. Never
imports `app.py` or `agent/tool_registry.py`.

Modules
-------
headless_driver.py   HeadlessSession — owns a RobotBridge in headless mode
                     (always its own Robot instance, never attach).
test_headless_driver.py  Phase 1 validation (baseline parity, visibility,
                     5x connect/build/solve/close loop, one-seat license probe).
storage.py           SQLite persistence (Phase 2).
design_space.py      Design-space schema + grid-search candidate generator
                     (Phase 4).
runner.py            Batch runner with checkpoints and crash recovery (Phase 5).
pareto.py            Pareto frontier + reporting (Phase 6).
"""
