import json
import logging
import os
import base64
import asyncio
import math
from io import BytesIO
from aiohttp import web, WSMsgType
from server import PromptServer
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ps-bridge")

# Module-level state
_connected_ws = None
_loop = asyncio.get_event_loop()  # Captured at startup on the main thread; used by worker threads
_data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "uploads")

# Ensure upload directory exists
os.makedirs(_data_dir, exist_ok=True)


@PromptServer.instance.routes.get("/ps-bridge/ping")
@PromptServer.instance.routes.get("/api/ps-bridge/ping")
async def ping_handler(request):
    return web.json_response({"status": "ok"})


@PromptServer.instance.routes.get("/ps-bridge/ws")
@PromptServer.instance.routes.get("/api/ps-bridge/ws")
async def websocket_handler(request):
    global _connected_ws

    ws = web.WebSocketResponse(max_msg_size=100 * 1024 * 1024)
    await ws.prepare(request)

    # Replace any existing connection
    old_ws = _connected_ws
    _connected_ws = ws
    logger.info("Photoshop client connected via WebSocket")

    if old_ws and not old_ws.closed:
        await old_ws.close()

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
                except json.JSONDecodeError:
                    pass
            elif msg.type == WSMsgType.ERROR:
                logger.error(f"WebSocket error: {ws.exception()}")
                break
    except Exception as e:
        logger.error(f"WebSocket handler error: {e}")
    finally:
        if _connected_ws is ws:
            _connected_ws = None
        logger.info("Photoshop client disconnected")

    return ws


@PromptServer.instance.routes.post("/ps-bridge/upload")
@PromptServer.instance.routes.post("/api/ps-bridge/upload")
async def upload_handler(request):
    try:
        reader = await request.multipart()
        width = 0
        height = 0
        mode = "mask"
        raw_mask_data = None
        crop_bounds = None

        async for part in reader:
            if part.name == "image":
                data = await part.read()
                image_path = os.path.join(_data_dir, "ps_image.png")
                with open(image_path, "wb") as f:
                    f.write(data)
                logger.info(f"Saved image: {len(data)} bytes")

            elif part.name == "mask":
                data = await part.read()
                if data[:4] == b'\x89PNG':
                    # Valid PNG, save directly
                    mask_path = os.path.join(_data_dir, "ps_mask.png")
                    with open(mask_path, "wb") as f:
                        f.write(data)
                    logger.info(f"Saved mask as PNG: {len(data)} bytes")
                else:
                    # Raw grayscale bytes — convert to PNG after we have dimensions
                    raw_mask_data = data
                    logger.info(f"Received raw mask: {len(data)} bytes")

            elif part.name == "width":
                width = int(await part.text())

            elif part.name == "height":
                height = int(await part.text())

            elif part.name == "mode":
                mode = await part.text()

            elif part.name == "crop_bounds":
                crop_bounds = json.loads(await part.text())

        # Convert raw mask bytes to PNG if needed
        mask_warning = None
        if raw_mask_data is not None and width > 0 and height > 0:
            mask_path = os.path.join(_data_dir, "ps_mask.png")
            expected_size = width * height
            if len(raw_mask_data) >= expected_size:
                import numpy as np
                mask_array = np.frombuffer(raw_mask_data[:expected_size], dtype=np.uint8).reshape((height, width))
                mask_img = Image.fromarray(mask_array, mode="L")
                mask_img.save(mask_path, "PNG")
                logger.info(f"Converted raw mask to PNG: {width}x{height}")
            else:
                # Size mismatch — create a white (no mask) fallback
                mask_img = Image.new("L", (width, height), 255)
                mask_img.save(mask_path, "PNG")
                mask_warning = f"Mask size mismatch ({len(raw_mask_data)} vs {expected_size}), using white fallback"
                logger.warning(mask_warning)

        # Save metadata (including crop bounds for crop mode)
        meta = {"width": width, "height": height, "mode": mode}
        if crop_bounds:
            meta["crop_bounds"] = crop_bounds
        meta_path = os.path.join(_data_dir, "meta.json")
        with open(meta_path, "w") as f:
            json.dump(meta, f)

        resp = {"status": "ok", "width": width, "height": height, "mode": mode}
        if mask_warning:
            resp["warning"] = mask_warning
        return web.json_response(resp)

    except Exception as e:
        logger.error(f"Upload error: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({"status": "error", "message": str(e)}, status=500)


@PromptServer.instance.routes.post("/ps-bridge/queue")
@PromptServer.instance.routes.post("/api/ps-bridge/queue")
async def queue_handler(request):
    try:
        # Signal the ComfyUI frontend JS extension to call app.queuePrompt()
        PromptServer.instance.send_sync("ps_bridge_queue", {})
        logger.info("Sent queue signal to ComfyUI frontend")
        return web.json_response({"status": "queued"})
    except Exception as e:
        logger.error(f"Queue error: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


@PromptServer.instance.routes.post("/ps-bridge/progress")
@PromptServer.instance.routes.post("/api/ps-bridge/progress")
async def progress_handler(request):
    """Internal endpoint: receives progress from JS extension, relays to PS."""
    try:
        data = await request.json()
        if _connected_ws and not _connected_ws.closed:
            value = data.get("value", 0)
            max_val = data.get("max", 100)
            progress = int((value / max_val) * 100) if max_val > 0 else 0
            await _connected_ws.send_json({"type": "progress", "value": progress})
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error"}, status=500)


@PromptServer.instance.routes.post("/ps-bridge/status")
@PromptServer.instance.routes.post("/api/ps-bridge/status")
async def status_handler(request):
    """Internal endpoint: receives execution status from JS extension, relays to PS."""
    try:
        data = await request.json()
        if _connected_ws and not _connected_ws.closed:
            await _connected_ws.send_json({
                "type": "status",
                "status": data.get("status", "unknown"),
                "error": data.get("error", "")
            })
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error"}, status=500)


async def send_result_to_ps(image_base64, width, height):
    """Called by SendToPS node to push result image to Photoshop.
    Returns True if sent successfully, False if no client connected."""
    if _connected_ws and not _connected_ws.closed:
        await _connected_ws.send_json({
            "type": "result",
            "image": image_base64,
            "width": width,
            "height": height
        })
        logger.info(f"Sent result to PS: {width}x{height}")
        return True
    else:
        logger.warning("No Photoshop client connected, cannot send result")
        return False
