# Runtime Migration Checklist (MuMu -> Docker Headless Emulator)

This project is now normalized for runtime portability.

## 1) Appium/ADB Runtime Config (Environment Variables)

Set these variables in host or container:

- `APPIUM_AUTOSTART=1`
- `APPIUM_HOST=127.0.0.1`
- `APPIUM_PORT=4723`
- `APPIUM_BASE_PATH=/wd/hub` (or `/` for some Appium 2 setups)
- `APPIUM_URL=http://127.0.0.1:4723/wd/hub` (optional override)
- `APPIUM_START_CMD=appium --host 0.0.0.0 --port 4723 --base-path /wd/hub` (optional override)

ADB / emulator:

- `ANDROID_SERIAL=emulator-5554` (recommended in Docker)
- `EMULATOR_START_CMD=<your emulator boot command>` (optional)
- `EMULATOR_WAIT_SECONDS=35` (optional)

## 2) What Was Decoupled

- No hardcoded MuMu device requirement in runner.
- Appium server can auto-start from Python process (no extra terminal required).
- If target `ANDROID_SERIAL` is unavailable, runner auto-fallbacks to first online ADB device.
- Driver connection is configurable through `APPIUM_URL`.

## 3) Containerization Recommendations

- Keep image immutable with preinstalled Java/Android SDK/Appium.
- Boot emulator in entrypoint script, then run benchmark command.
- Use one container per task-run (destroy after run) for strict reproducibility.
- Mount only required workspace paths (read-only `data/base_src` if possible).
- Persist outputs to dedicated volume (`results/`).

## 4) Commands

Batch run:

```bash
python scripts/Experiment_Launcher.py --model deepseek-r1 --strategy direct ReAct tool_planning --tasks app_foodyou --direct-repeats 3
```

Single task:

```bash
python scripts/global_evaluator.py FoodYou_Thm_01 --model deepseek-r1 --strategy ReAct
```

## 5) CI/CD Notes

- Validate `adb devices` before launching benchmark.
- Ensure Appium health endpoint responds before tests.
- Record environment fingerprint per run (Java, SDK, Appium versions).
