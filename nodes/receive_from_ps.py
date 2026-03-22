import os
import json
import hashlib
import random
import numpy as np
import torch
from PIL import Image, ImageFile
import folder_paths

ImageFile.LOAD_TRUNCATED_IMAGES = True

_data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "uploads")


class ReceiveFromPS:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("IMAGE", "MASK", "INT", "INT")
    RETURN_NAMES = ("image", "mask", "width", "height")
    FUNCTION = "execute"
    CATEGORY = "PS Bridge"

    def execute(self):
        default_size = (24, 24)

        # Load metadata
        meta_path = os.path.join(_data_dir, "meta.json")
        meta = {}
        if os.path.exists(meta_path):
            with open(meta_path, "r") as f:
                meta = json.load(f)

        mode = meta.get("mode", "mask")
        crop_bounds = meta.get("crop_bounds", None)

        # Load image
        image_path = os.path.join(_data_dir, "ps_image.png")
        if os.path.exists(image_path):
            try:
                img = Image.open(image_path)
                img.load()

                # Handle alpha channel
                if img.mode in ("RGBA", "LA"):
                    alpha = img.split()[-1]
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    background.paste(img, mask=alpha)
                    img = background
                elif img.mode != "RGB":
                    img = img.convert("RGB")

                # Crop if in crop mode with bounds
                if mode == "crop" and crop_bounds:
                    left = crop_bounds.get("left", 0)
                    top = crop_bounds.get("top", 0)
                    right = crop_bounds.get("right", img.width)
                    bottom = crop_bounds.get("bottom", img.height)
                    img = img.crop((left, top, right, bottom))

                w, h = img.size
                image_array = np.array(img).astype(np.float32) / 255.0
                image_tensor = torch.from_numpy(image_array).unsqueeze(0)  # (1, H, W, 3)
            except Exception as e:
                print(f"[PS Bridge] Error loading image: {e}")
                import traceback
                traceback.print_exc()
                w, h = default_size
                image_tensor = torch.zeros((1, h, w, 3), dtype=torch.float32)
        else:
            w, h = default_size
            image_tensor = torch.zeros((1, h, w, 3), dtype=torch.float32)

        # Load mask
        mask_path = os.path.join(_data_dir, "ps_mask.png")
        if os.path.exists(mask_path):
            try:
                mask_img = Image.open(mask_path)
                mask_img.load()
                mask_img = mask_img.convert("L")

                # Crop mask to match if in crop mode
                if mode == "crop" and crop_bounds:
                    left = crop_bounds.get("left", 0)
                    top = crop_bounds.get("top", 0)
                    right = crop_bounds.get("right", mask_img.width)
                    bottom = crop_bounds.get("bottom", mask_img.height)
                    mask_img = mask_img.crop((left, top, right, bottom))

                # Resize mask to match image if needed
                if mask_img.size != (w, h):
                    mask_img = mask_img.resize((w, h), Image.LANCZOS)

                mask_array = np.array(mask_img).astype(np.float32) / 255.0
                mask_tensor = torch.from_numpy(mask_array).unsqueeze(0)  # (1, H, W)
            except Exception as e:
                print(f"[PS Bridge] Error loading mask: {e}")
                mask_tensor = torch.ones((1, h, w), dtype=torch.float32)
        else:
            mask_tensor = torch.ones((1, h, w), dtype=torch.float32)

        # Save preview to temp directory
        preview_results = []
        try:
            temp_dir = folder_paths.get_temp_directory()
            prefix = "_psbr_" + ''.join(random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(5))
            full_path, filename, counter, subfolder, _ = folder_paths.get_save_image_path(prefix, temp_dir, w, h)
            for i in range(image_tensor.shape[0]):
                arr = (image_tensor[i].cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
                Image.fromarray(arr).save(os.path.join(full_path, f"{filename}_{counter:05}_.png"), compress_level=1)
                preview_results.append({"filename": f"{filename}_{counter:05}_.png", "subfolder": subfolder, "type": "temp"})
                counter += 1
        except Exception as e:
            print(f"[PS Bridge] Preview save error: {e}")

        return {"ui": {"images": preview_results}, "result": (image_tensor, mask_tensor, w, h)}

    @classmethod
    def IS_CHANGED(cls):
        """Return hash of uploaded files so ComfyUI re-executes when data changes."""
        paths = [
            os.path.join(_data_dir, "ps_image.png"),
            os.path.join(_data_dir, "ps_mask.png"),
            os.path.join(_data_dir, "meta.json"),
        ]

        hash_parts = []
        for path in paths:
            if os.path.exists(path):
                hash_parts.append(str(os.path.getmtime(path)))
            else:
                hash_parts.append("missing")

        return hashlib.sha256("|".join(hash_parts).encode()).hexdigest()
