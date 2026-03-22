import asyncio
import base64
import os
import random
import threading
from io import BytesIO
import torch
import numpy as np
from PIL import Image
import folder_paths


class SendToPS:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("IMAGE",)}}

    RETURN_TYPES = ()
    OUTPUT_NODE = True
    FUNCTION = "execute"
    CATEGORY = "PS Bridge"

    def execute(self, image):
        # Convert tensor to PIL Image
        # image shape: (batch, H, W, 3), values 0-1
        img_tensor = image[0]  # Take first image from batch
        img_array = (img_tensor.cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
        pil_image = Image.fromarray(img_array, "RGB")

        h, w = img_array.shape[:2]

        # Encode as base64 PNG
        buffer = BytesIO()
        pil_image.save(buffer, format="PNG")
        image_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

        # Send to Photoshop via bridge WebSocket (async, scheduled on ComfyUI's event loop)
        def _send():
            try:
                import bridge
                future = asyncio.run_coroutine_threadsafe(
                    bridge.send_result_to_ps(image_base64, w, h), bridge._loop
                )
                sent = future.result(timeout=10)
                if not sent:
                    print("[PS Bridge] WARNING: No Photoshop client connected. Result was not delivered.")
            except Exception as e:
                print(f"[PS Bridge] Error sending to PS: {e}")

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()
        thread.join(timeout=10)

        # Save preview to temp directory
        preview_results = []
        try:
            temp_dir = folder_paths.get_temp_directory()
            prefix = "_psbs_" + ''.join(random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(5))
            full_path, filename, counter, subfolder, _ = folder_paths.get_save_image_path(prefix, temp_dir, w, h)
            file = f"{filename}_{counter:05}_.png"
            pil_image.save(os.path.join(full_path, file), compress_level=1)
            preview_results.append({"filename": file, "subfolder": subfolder, "type": "temp"})
        except Exception as e:
            print(f"[PS Bridge] Preview save error: {e}")

        return {"ui": {"images": preview_results}}
