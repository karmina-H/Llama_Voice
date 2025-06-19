"""
This module defines the application settings using the Pydantic library.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from torch import cuda


class Settings(BaseSettings):
    """
    Application settings class.

    This class inherits from the Pydantic BaseSettings class and defines the configuration settings for the
    application. Default values can be overridden by setting environment variables.

    Attributes:
        HF_TOKEN: The Hugging Face token for accessing models and resources.
        TORCH_DEVICE: The device to use for PyTorch computations (e.g. 'cuda' or 'cpu').
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    HF_TOKEN: str = "" #기존의 .env파일에 있는 허깅페이스 토큰 가져오는 부분(여기에 직접x, .env파일에 할당)
    TORCH_DEVICE: str = "cuda" if cuda.is_available() else "cpu"


settings = Settings()


default_config = {
    "llm": {"disable_chat_history": False, "model": "llama3.2"},
    "stt": {
        "device": settings.TORCH_DEVICE,
        "generation_args": {"batch_size": 8},
        "model": "openai/whisper-small.en",
    },
    "tts": {"device": settings.TORCH_DEVICE, "model": "tts_models/en/ljspeech/glow-tts"},
}
