"""Small public-facing product configuration shared by the Normal UI."""

APP_NAME = "Local AI Companion"
APP_VERSION = "0.1.17"
FIRST_PARTY_YOUTUBE_CHANNEL_URL = "https://www.youtube.com/@ranai-korea"
FIRST_PARTY_YOUTUBE_HANDLE = "@ranai-korea"
# The cached channel ID is not independently verified, so it is not an identity authority.
FIRST_PARTY_YOUTUBE_CHANNEL_ID = ""
PRODUCT_WEBSITE_URL = FIRST_PARTY_YOUTUBE_CHANNEL_URL
FIRST_PARTY_WEBSITE_URL = "https://nestfrontier.com"
FIRST_PARTY_WEBSITE_SOURCES = ()
FIRST_PARTY_WEBSITE_SOURCE_URLS = ()
FIRST_PARTY_INDEX_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60

# Existing Strata service; this application uses its OpenAI-compatible text API.
LOCAL_AI_PROVIDER = "openai_compatible"
LOCAL_AI_BASE_URL = "http://127.0.0.1:8080/v1"
LOCAL_AI_MODEL = "qwen3.8-flash-next-iq2_xs"
ALLOW_CLOUD_AI = False

PUBLIC_SOURCES = {
    "primary": (
        {"name": "GitHub", "url": "https://github.com/"},
        {"name": "Hugging Face", "url": "https://huggingface.co/"},
        {"name": "Civitai", "url": "https://civitai.com/"},
        {"name": "ComfyUI", "url": "https://www.comfy.org/"},
        {"name": "YouTube", "url": "https://www.youtube.com/"},
    ),
    "secondary": (
        {"name": "NestFrontier", "url": FIRST_PARTY_WEBSITE_URL},
        {"name": "X", "url": "https://x.com/"},
        {"name": "Reddit", "url": "https://www.reddit.com/"},
        {"name": "ComfyWorkflows", "url": "https://comfyworkflows.com/"},
        {"name": "OpenArt", "url": "https://openart.ai/"},
        {"name": "RunComfy", "url": "https://www.runcomfy.com/"},
    ),
    "product_website": PRODUCT_WEBSITE_URL,
}
