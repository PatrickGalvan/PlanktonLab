"""Stateful OpenCV image detection and optional fastai classification utilities.

See ``docs/API_REFERENCE.md`` for method contracts, state changes, preconditions,
and end-to-end examples.
"""

import cv2
import numpy as np
import os
import pathlib
import base64
import io
import threading
import warnings
from contextlib import contextmanager, nullcontext
from PIL import Image
from fastai.learner import load_learner

_DEFAULT_LEARNER = object()
_PATHLIB_PICKLE_LOCK = threading.RLock()


@contextmanager
def native_pathlib_for_pickle():
    """Map pickled concrete paths to the class supported by this OS."""
    foreign_path_name = "PosixPath" if os.name == "nt" else "WindowsPath"
    native_path = pathlib.WindowsPath if os.name == "nt" else pathlib.PosixPath

    # FastAI does not expose an unpickler hook. Keep this module-level compatibility
    # mapping scoped, serialized, and guaranteed to be restored after model loading.
    with _PATHLIB_PICKLE_LOCK:
        foreign_path = getattr(pathlib, foreign_path_name)
        setattr(pathlib, foreign_path_name, native_path)
        try:
            yield
        finally:
            setattr(pathlib, foreign_path_name, foreign_path)


class Image_Classifier:
    """
    A class to handle image processing, thresholding, contour detection,
    and species classification using a pre-trained model.
    """

    def __init__(self):
        """
        Initializes the Image_Classifier with default states and no learner.
        """
        self.meta_id = None
        self.mask = None
        self.orig_img = None
        self.contour_array = []
        self.learner = None
        self.mask_io = None
        self.curr_image_io = None

    def load_learner_from_file(self, model_path, raise_errors=False):
        """
        Attempts to load a fastai learner model from the local file system.

        :param model_path: Path to a trusted, versioned model artifact.
        :param raise_errors: Re-raise loading errors when validating an upload.
        :return: A fastai learner object if successful, else None.
        """
        model_path = pathlib.Path(model_path)

        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"load_learner` uses Python's insecure pickle module",
                    category=UserWarning,
                )
                with native_pathlib_for_pickle():
                    learner = load_learner(model_path, cpu=True)
            return learner
        except Exception as e:
            if raise_errors:
                raise
            print("Model not found or failed to load:", e)
            return None

    def io_image(self):
        """
        Converts the current original image and its mask into base64 encoded
        strings for transmission to a web front-end.
        """
        # Convert original image to base64
        data = io.BytesIO()
        Image.fromarray(self.orig_img).save(data, "JPEG")
        self.curr_image_io = base64.b64encode(data.getvalue())

        # Convert mask to base64
        data = io.BytesIO()
        Image.fromarray(self.mask).save(data, "JPEG")
        self.mask_io = base64.b64encode(data.getvalue())

    def create_mask(self, thresh_1=75, thresh_2=200):
        """
        Applies a binary threshold to the grayscale image to create a mask.

        :param thresh_1: The lower threshold value.
        :param thresh_2: The value assigned to pixels exceeding the threshold.
        """
        _, mask = cv2.threshold(self.orig_img, thresh_1, thresh_2, cv2.THRESH_BINARY)
        self.mask = mask
        self.io_image()

    def read_image(self, path):
        """
        Reads an image from a file path and converts it to grayscale.

        :param path: System path to the image file.
        """
        self.orig_img = cv2.imread(path, cv2.IMREAD_COLOR)
        self.orig_img = cv2.cvtColor(self.orig_img, cv2.COLOR_BGR2GRAY)

    def draw_boxes(self, use_otsu=True, thresh_1=75, thresh_2=200, padding_ratio=0.15):
        """
        Detects contours using a robust adaptive size-filtering heuristic based on the
        median area of biological detections in the frame. Corrected to filter out micro-noise
        before calculating the median to avoid collapsing the size thresholds.
        """

        self.contour_array = []
        tmp_img = self.orig_img
        height, width = tmp_img.shape[:2]

        # 1. Apply a light Gaussian Blur to smooth out high-frequency sensor noise
        blurred = cv2.GaussianBlur(tmp_img, (3, 3), 0)

        # 2. Automated vs Manual Thresholding (Otsu active by default)
        if use_otsu:
            _, mask = cv2.threshold(
                blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
            )
        else:
            _, mask = cv2.threshold(blurred, thresh_1, thresh_2, cv2.THRESH_BINARY)

        # 3. Morphological Operations to seal transparent shells
        kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_close)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel_open)

        self.mask = mask
        self.io_image()

        # 4. Find external contours
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
        )

        # --- FIRST PASS: Gather biological candidates ---
        # We set a baseline floor of 150 pixels *before* calculating the median.
        # This prevents the hundreds of microscopic dust particles from skewing our statistics.
        candidates = []
        biological_areas = []

        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = w * h

            # If the object is at least 150 pixels, it enters our biological pool
            if area >= 150:
                candidates.append((contour, x, y, w, h, area))
                biological_areas.append(area)

        if not candidates:
            return

        # --- SECOND PASS: Calculate dynamic thresholds ---
        # Extract the median of the actual biological organisms
        median_area = np.median(biological_areas)

        # Biological Size Heuristics:
        # A. Dynamic Min: 15% of the median size (or a safety floor of 150px)
        dynamic_min = max(150, int(median_area * 0.15))

        # B. Dynamic Max: 20x the median size (or a safety ceiling of 40,000px)
        dynamic_max = max(40000, int(median_area * 20))

        # --- THIRD PASS: Filter and apply padding ---
        for contour, x, y, w, h, area in candidates:

            # A. Dynamic Area Constraint (safely scales to resolution and zoom)
            if not (dynamic_min < area < dynamic_max):
                continue

            # B. Aspect Ratio Filter (Rejects scratches, fibers, and hairs)
            aspect_ratio = float(w) / h
            if aspect_ratio < 0.15 or aspect_ratio > 6.5:
                continue

            # C. Extent/Solidity Check (Verifies the object is compact and biological)
            contour_area = cv2.contourArea(contour)
            if contour_area > 0:
                extent = float(contour_area) / area
                if extent < 0.22:
                    continue

            # D. Dynamic Padding (Ensures margins are preserved for the CNN)
            if padding_ratio > 0:
                pad_w = int(w * padding_ratio)
                pad_h = int(h * padding_ratio)

                padded_x = max(0, x - pad_w)
                padded_y = max(0, y - pad_h)
                padded_w = min(width, x + w + pad_w) - padded_x
                padded_h = min(height, y + h + pad_h) - padded_y

                self.contour_array.append([padded_x, padded_y, padded_w, padded_h])
            else:
                self.contour_array.append([x, y, w, h])

    def numpy_to_base64(self, img_np, format="JPEG"):
        """
        Utility to convert a NumPy image array to a base64 UTF-8 string.

        :param img_np: Image data in NumPy array format.
        :param format: Image format for encoding (e.g., "JPEG", "PNG").
        :return: Base64 encoded string.
        """
        buffer = io.BytesIO()
        Image.fromarray(img_np).save(buffer, format=format)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

    def coords_to_base64_no_classification(self, cont):
        """
        Crops an image based on coordinates and returns the base64 string
        without running ML inference.

        :param cont: List of coordinates [x, y, w, h].
        :return: Base64 encoded string of the cropped image.
        """
        img_class = self.orig_img[
            cont[1] : cont[1] + cont[3], cont[0] : cont[0] + cont[2]
        ]
        img_base64 = self.numpy_to_base64(img_class)
        return img_base64

    def coords_to_base64_with_classification(
        self,
        cont,
        has_learner=True,
        learner=_DEFAULT_LEARNER,
    ):
        """
        Crops an image, runs the ML classifier on the crop, and returns
        a dictionary structured for database insertion.

        :param cont: List of coordinates [x, y, w, h].
        :param has_learner: Boolean flag checking if model is loaded.
        :param learner: Optional runtime-selected learner. Defaults to the learner
                        stored on this classifier instance.
        :return: Dictionary containing metadata, coordinates, species,
                 confidence, and the base64 image crop.
        """
        # Crop the original image to the bounding box
        img_class = self.orig_img[
            cont[1] : cont[1] + cont[3],
            cont[0] : cont[0] + cont[2],
        ]

        species = "Unlabeled"
        confidence = 0.0

        # Predict using fastai learner if available
        if has_learner:
            selected_learner = self.learner if learner is _DEFAULT_LEARNER else learner
            no_bar = getattr(selected_learner, "no_bar", None)
            progress_context = no_bar() if callable(no_bar) else nullcontext()
            with progress_context:
                cat, _, probs = selected_learner.predict(img_class)
            species = str(cat)
            confidence = round(probs.max().item(), 2)

        return {
            "sampling_metadata_id": self.meta_id,
            "x": cont[0],
            "y": cont[1],
            "w": cont[2],
            "h": cont[3],
            "species": species,
            "confidence": confidence,
            "image_base64": self.numpy_to_base64(img_class),
        }
