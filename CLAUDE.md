# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A ComfyUI custom node that bridges Adobe Photoshop (UXP plugin) and ComfyUI. Photoshop sends the canvas image and selection mask to ComfyUI for AI processing, ComfyUI runs the workflow, and the result is placed back as a new Photoshop smart object layer.

No build step exists. All dependencies (Pillow, numpy, torch, aiohttp) ship with ComfyUI.

## Running / Reloading

- **ComfyUI server:** restart `run_nvidia_gpu.bat` (or equivalent) from the ComfyUI portable root to pick up any Python changes.
- **Photoshop plugin:** reload the panel via the UXP Developer Tool (`Window > Utilities > UXP Developer Tool`) — use "Load" pointing to `photoshop/manifest.json`, or reload an already-loaded plugin.
- **Manifest changes** (sizes, icons, permissions): must fully remove and re-add the plugin in UXP Developer Tool. Reload alone does not pick up manifest changes.
- **ComfyUI JS extension** (`js/progress_relay.js`): reloads with a browser refresh of the ComfyUI web UI (no server restart needed).

## Architecture

### Three-layer system

```
Photoshop UXP Plugin          Bridge Server                   ComfyUI
  photoshop/main.js    ←HTTP→  server/bridge.py   ←events→   js/progress_relay.js
                       ←WS──→  (aiohttp routes)              nodes/receive_from_ps.py
                                                              nodes/send_to_ps.py
```

**Upload flow (PS → ComfyUI):**
1. `main.js` captures the canvas via `batchPlay` save-as-PNG into a UXP temp file, builds a raw-bytes mask (grayscale, 1 byte/pixel), POSTs both as multipart to `/ps-bridge/upload`.
2. `bridge.py` saves `ps_image.png`, `ps_mask.png`, and `meta.json` into `data/uploads/`.
3. `main.js` then POSTs to `/ps-bridge/queue`; `bridge.py` fires a `ps_bridge_queue` event via `PromptServer.send_sync`.
4. `progress_relay.js` listens for that event and calls `app.queuePrompt(0, 1)` to trigger workflow execution.
5. `ReceiveFromPS.execute()` reads the saved files and returns `(IMAGE, MASK, width, height)` tensors.

**Result flow (ComfyUI → PS):**
1. `SendToPS.execute()` encodes the output tensor as base64 PNG, then schedules `send_result_to_ps()` on ComfyUI's main event loop via `asyncio.run_coroutine_threadsafe(coro, bridge._loop)`. The loop is captured at `bridge.py` module load time (which runs on the main thread); `asyncio.get_event_loop()` must NOT be called from the worker thread (fails in Python 3.10+).
2. `bridge.py`'s `send_result_to_ps` sends `{type: "result", image: base64, width, height}` over the persistent WebSocket to Photoshop. Returns `True`/`False` to indicate delivery success.
3. `main.js` shows the result as a preview. When the user clicks the preview image, it uses `batchPlay placeEvent` to insert it as a smart object, scales/positions it to match the original selection bounds, then restores the selection. All operations are grouped into a single history state via `historyStateInfo` so Ctrl+Z undoes the entire placement.

**Progress/status relay:**
`progress_relay.js` listens for ComfyUI's `progress`, `execution_start`, `executing`, and `execution_error` events, then POSTs them to `/ps-bridge/progress` and `/ps-bridge/status`. `bridge.py` relays them over the WebSocket to Photoshop.

### Module loading critical detail

`__init__.py` loads `bridge.py` with `importlib` (not a standard import) and registers it as `sys.modules["bridge"]` **before** `exec_module` runs. This is required so that `send_to_ps.py`'s `from bridge import send_result_to_ps` resolves to the same instance that holds the live `_connected_ws` WebSocket reference.

### Key state

- `_connected_ws` in `bridge.py` — the single active aiohttp WebSocket to Photoshop. Only one connection is kept; new connections replace old ones.
- `data/uploads/` — shared file handoff between `bridge.py` (writer) and the ComfyUI nodes (reader). Contains `ps_image.png`, `ps_mask.png`, `meta.json`.
- `meta.json` schema: `{width, height, mode: "mask"|"crop", crop_bounds?: {left, top, right, bottom}}`.

### Modes

- **Mask mode** (default): full canvas sent, selection converted to a white-inside/black-outside grayscale mask. `ReceiveFromPS` returns the full image + mask tensor.
- **Crop mode**: full canvas sent with `crop_bounds`. `ReceiveFromPS` crops both image and mask to those bounds before returning. `SendToPS` result is placed at `selectionBounds` coordinates.

## Photoshop Plugin Notes

- Uses Adobe UXP (manifest v6), requires Photoshop ≥ 23.0.0.
- All document-modifying `batchPlay` calls must run inside `executeAsModal`. HTTP fetch calls must be **outside** `executeAsModal`.
- `batchPlay` errors are plain objects `{number, description}`, not `Error` instances — always use `e?.message || e?.description || String(e)` in catch blocks.
- UXP requires `fs.createSessionToken(tempFile)` for batchPlay `_path` arguments — raw `nativePath` strings cause "invalid file token" errors.
- Server URL is persisted in `localStorage` as `ps_bridge_url`. WebSocket auto-reconnects every 5 s.
- Ping/pong keepalive fires every 30 s to prevent the WebSocket from timing out.
- CSS uses `--uxp-host-background-color`, `--uxp-host-text-color`, `--uxp-host-border-color` variables for Photoshop theme compatibility. Keep fallback values for all variables.
- Generate button doubles as Cancel during generation (POSTs to `/interrupt`). A 5-minute safety timeout resets the UI if ComfyUI never responds.
