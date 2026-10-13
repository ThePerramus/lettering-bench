from typing import Any
from .gpt_ocr import GPTOCR


class CustomOCR(GPTOCR):
    """OCR engine using a custom OpenAI-compatible vision API."""

    def initialize(self, settings: Any, expansion_percentage: int = 0) -> None:
        """
        Initialize the custom OCR engine from the "Custom" credentials
        (shared with the Custom translation engine) instead of a fixed
        model/API key.

        Args:
            settings: Settings object with credentials
            expansion_percentage: Percentage to expand text bounding boxes
        """
        credentials = settings.get_credentials(settings.ui.tr("Custom"))
        self.api_key = credentials.get('api_key', '')
        self.model = credentials.get('model', '')
        # GPTOCR.api_base_url is the full chat-completions endpoint (unlike
        # GPTTranslation, which stores just the base and appends this suffix
        # itself at request time) - append it here so the Custom URL field
        # keeps the same "base URL" meaning for both OCR and translation.
        base_url = credentials.get('api_url', '').rstrip('/')
        self.api_base_url = f"{base_url}/chat/completions"
        self.expansion_percentage = expansion_percentage
