import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "30"))
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_TEXT_MODEL = os.getenv("OLLAMA_TEXT_MODEL", "qwen2.5:3b")
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "qwen2.5vl:3b")
OLLAMA_NUM_PREDICT = max(128, min(8192, int(os.getenv("OLLAMA_NUM_PREDICT", "3072"))))
OLLAMA_NUM_CTX = max(2048, min(32768, int(os.getenv("OLLAMA_NUM_CTX", "8192"))))
OCR_ENABLED = os.getenv("OCR_ENABLED", "true").lower() in {"1", "true", "yes"}
OCR_CROSSCHECK = os.getenv("OCR_CROSSCHECK", "true").lower() in {"1", "true", "yes"}
OCR_LANGUAGE = os.getenv("OCR_LANGUAGE", "eng")
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "")
APP_API_KEY = os.getenv("APP_API_KEY", "")
APP_ENV = os.getenv("APP_ENV", "development").lower()
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/docintel.sqlite3"))
DATA_ENCRYPTION_KEY = os.getenv("DATA_ENCRYPTION_KEY", "")
