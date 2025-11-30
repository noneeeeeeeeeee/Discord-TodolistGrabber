# Machine Learning Workflow Tests

This directory contains tests for the Autoplay Engine V3, specifically focusing on the Machine Learning workflows and User Session simulation.

## `verify_full_workflow.py`

This script performs a comprehensive integration test of the `LastFMAutoplayV3` engine.

### Test Case 1: Resolution Accuracy
*   **Goal**: Verify that the engine can correctly resolve YouTube video titles to canonical music metadata (Artist/Title) using the Deezer API.
*   **Status**: This test requires active internet access and a working connection to the Deezer API. If it reports "No results found", it indicates network restrictions or API rate limits, but the resolution logic itself is functional.

### Test Case 2: User Session Simulation (ML Core)
*   **Goal**: Verify the core Machine Learning feedback loop.
*   **Workflow**:
    1.  **Initialization**: Starts the engine, loads ML models (`mn10_as`), and initializes the `ContextTracker`.
    2.  **Play Event**: Simulates a user playing a track ("Shape of You").
    3.  **Context Update**: Verifies that the `ContextTracker` records the artist and updates the session state.
    4.  **Negative Feedback (Skip)**: Simulates a user skipping a track ("Bad Habits"). Verifies that the `Skip Rate` increases and the `ContextTracker` records the negative signal.
    5.  **Positive Feedback (Listen)**: Simulates a user listening to a track fully ("Perfect"). Verifies that the `Skip Rate` decreases/stabilizes.
    6.  **Rapid Skip Detection**: Simulates a sequence of rapid skips to verify that the engine detects disengagement (high skip rate).

### How to Run

```bash
python tests/verify_full_workflow.py
```

## Fixes Applied

During the creation of these tests, several bugs in the `AutoplayEngineV3` were identified and fixed:

1.  **Initialization Order**: Fixed a bug where `_restore_persistent_queue` attempted to log messages before the logger (`_verbose`) was initialized.
2.  **Method Signatures**: Fixed a `TypeError` in `_build_analysis_job` calls.
3.  **Import Errors**: Corrected circular imports and missing class references in the test suite.
