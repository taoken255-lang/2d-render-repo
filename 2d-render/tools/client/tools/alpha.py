import cv2
import torch
import numpy as np
from PIL import Image
from transformers import AutoImageProcessor, UperNetForSemanticSegmentation

processor = AutoImageProcessor.from_pretrained(
    "openmmlab/upernet-convnext-tiny")
model = UperNetForSemanticSegmentation.from_pretrained(
    "openmmlab/upernet-convnext-tiny")
model.eval()
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

model.to(device)


def segment_person_from_pil_image(pil_image: Image.Image):
    # Convert PIL Image to numpy array in BGR format
    frame = cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)

    # Optional: run on GPU if available

    def preprocess_frame(frame):
        inputs = processor(images=frame,  # BGR to RGB
                           return_tensors="pt").to(device)
        return inputs

    inputs = preprocess_frame(frame)

    with torch.no_grad():
        outputs = model(**inputs)

    logits = outputs.logits
    upsampled_logits = torch.nn.functional.interpolate(
        logits,
        size=frame.shape[:2],
        mode="bilinear",
        align_corners=False,
    )
    segmentation = upsampled_logits.argmax(dim=1)[0].cpu().numpy()

    # print("Unique segmentation values:", np.unique(segmentation))

    # ADE20K "person" class = 12
    person_class_id = 12
    mask = (segmentation == person_class_id).astype(np.uint8) * 255

    if mask.sum() == 0:
        print("⚠️ No person pixels found in this image")

    # mask_colored = cv2.applyColorMap(mask, cv2.COLORMAP_INFERNO)
    # blended = cv2.addWeighted(frame, 0.7, mask_colored, 0.3, 0)

    bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = mask
    return bgra
