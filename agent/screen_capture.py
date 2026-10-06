"""Explicitly started, bounded JPEG screen capture for the agent."""

from io import BytesIO

from PIL import Image

MAX_FRAME_BYTES = 750_000
MAX_FRAME_WIDTH = 1280
MAX_FRAME_HEIGHT = 720
DEFAULT_JPEG_QUALITY = 55


def encode_rgb_frame(
    rgb: bytes,
    width: int,
    height: int,
    *,
    max_width: int = MAX_FRAME_WIDTH,
    max_height: int = MAX_FRAME_HEIGHT,
    quality: int = DEFAULT_JPEG_QUALITY,
    max_bytes: int = MAX_FRAME_BYTES,
) -> tuple[bytes, int, int]:
    if width <= 0 or height <= 0 or len(rgb) != width * height * 3:
        raise ValueError("Dados RGB inválidos")

    max_width = max(320, min(int(max_width), MAX_FRAME_WIDTH))
    max_height = max(180, min(int(max_height), MAX_FRAME_HEIGHT))
    quality = max(30, min(int(quality), 85))
    image = Image.frombytes("RGB", (width, height), rgb)
    scale = min(1.0, max_width / width, max_height / height)
    if scale < 1.0:
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
        image = image.resize(size, Image.Resampling.LANCZOS)

    while True:
        for frame_quality in (quality, max(30, quality - 15), 30):
            output = BytesIO()
            image.save(output, format="JPEG", quality=frame_quality, optimize=True)
            frame = output.getvalue()
            if len(frame) <= max_bytes:
                return frame, image.width, image.height
        if image.width <= 320 or image.height <= 180:
            raise ValueError("Frame JPEG excede o limite de transmissão")
        size = (max(320, image.width // 2), max(180, image.height // 2))
        image = image.resize(size, Image.Resampling.LANCZOS)


class ScreenCapture:
    """Capture the primary display only while an explicit stream is active."""

    def __init__(
        self,
        max_width: int = MAX_FRAME_WIDTH,
        max_height: int = MAX_FRAME_HEIGHT,
        quality: int = DEFAULT_JPEG_QUALITY,
        max_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        import mss

        self._capture = mss.mss()
        if len(self._capture.monitors) < 2:
            self._capture.close()
            raise RuntimeError("Monitor principal indisponível")
        self._monitor = self._capture.monitors[1]
        self._max_width = max_width
        self._max_height = max_height
        self._quality = quality
        self._max_bytes = max_bytes

    @property
    def screen_size(self) -> tuple[int, int]:
        return int(self._monitor["width"]), int(self._monitor["height"])

    def capture_jpeg(self) -> tuple[bytes, int, int]:
        screenshot = self._capture.grab(self._monitor)
        return encode_rgb_frame(
            screenshot.rgb,
            screenshot.width,
            screenshot.height,
            max_width=self._max_width,
            max_height=self._max_height,
            quality=self._quality,
            max_bytes=self._max_bytes,
        )

    def close(self) -> None:
        self._capture.close()