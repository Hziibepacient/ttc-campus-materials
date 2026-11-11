# Third-party components

EventLens source is Apache-2.0. Nothing below is bundled in this repository; each is
installed or downloaded separately and keeps its own license.

| Component | Use | License | Source |
|---|---|---|---|
| YOLOX-S ONNX weights (`yolox_s.onnx`, sha256 `c5c2d13e…8063`) | person detection | Apache-2.0 | [Megvii YOLOX release 0.1.1rc0](https://github.com/Megvii-BaseDetection/YOLOX/releases/tag/0.1.1rc0) |
| ONNX Runtime | inference | MIT | [microsoft/onnxruntime](https://github.com/microsoft/onnxruntime) |
| OpenCV (`opencv-python-headless`) | image processing | Apache-2.0 | [opencv/opencv-python](https://github.com/opencv/opencv-python) |
| NumPy, SciPy | arrays, assignment | BSD-3-Clause | numpy.org, scipy.org |
| FFmpeg / FFprobe | decoding (external binaries) | LGPL-2.1+ or GPL depending on build | [ffmpeg.org](https://ffmpeg.org/legal.html) |
| Demo clip `Pedestrian_area_Ljubljana.webm` | `eventlens demo` | CC BY 3.0, *Sounds of Changes* | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Pedestrian_area_Ljubljana.webm) |

Your own event videos, frames and analysis data are yours; they are never part of this
repository and are not uploaded anywhere by the tool.
