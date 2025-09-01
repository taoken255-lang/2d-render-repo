import torch
from loguru import logger
import numpy as np
from torchvision import transforms
from transformers import AutoModelForImageSegmentation
from PIL import Image

device = 'cuda' if torch.cuda.is_available() else 'cpu'
# Load the BiRefNet model
model = AutoModelForImageSegmentation.from_pretrained(
    'zhengpeng7/BiRefNet-portrait', trust_remote_code=True)
model.to(device)
model.eval()
autocast_ctx = torch.amp.autocast(device_type='cuda', dtype=[
                                  torch.float16, torch.bfloat16][0])

transform_image = transforms.Compose([
    transforms.Resize((1024, 1024)),
    transforms.ToTensor(),
    transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
])


class OfflineAlphaService:
    def segment_person_from_pil_image(self, frame: Image):  # frame: ImageObject
        pred_image = transform_image(frame).unsqueeze(0).to(device)
        with autocast_ctx, torch.no_grad():
            preds = model(pred_image)[-1].sigmoid().to(torch.float32).cpu()
        pred = preds[0].squeeze()

        pred_resized = transforms.functional.resize(
            pred.unsqueeze(0), frame.size[::-1])[0]

        alpha_np = (pred_resized.numpy() * 255).astype(np.uint8)

        rgb_np = np.array(frame)

        # Stack RGB and alpha to get RGBA
        rgba_np = np.dstack([rgb_np, alpha_np])
        rgba_pil = Image.fromarray(rgba_np, mode="RGBA")
        rgba_pil.save(f"./frames/frames_.png")
        return rgba_np.tobytes()
