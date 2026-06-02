from typing import Union

import httpx
from smolagents import tool

from thinking_settings import thinking_settings


def _build_image_content(image_url: str) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": image_url}
    }


@tool
def visualRecognition(image_url: Union[str, list[str]]) -> str:
    """Recognize and describe the content of one or more images. Use this tool when you need
    to understand what is in an image, read text from an image, compare multiple images,
    or get a description of visual content.

    Supports both public image URLs and base64-encoded images.
    For base64 images, use the format: data:{MIME_TYPE};base64,{BASE64_DATA}
    e.g. data:image/png;base64,iVBORw0KGgo...

    Args:
        image_url: A single image URL/base64 string, or a list of image URLs/base64 strings.

    Returns:
        A text description of the image content, including recognized text if any.

    Example:
        visualRecognition("https://example.com/photo.jpg") -> "A cat sitting on a windowsill"
        visualRecognition(["https://example.com/a.jpg", "https://example.com/b.jpg"]) -> "Two images compared..."
        visualRecognition("data:image/png;base64,iVBORw0KGgo...") -> "A screenshot showing..."
    """
    api_url = thinking_settings.VISUAL_RECOGNITION_API_URL.rstrip("/")
    api_key = thinking_settings.VISUAL_RECOGNITION_API_KEY
    model = thinking_settings.VISUAL_RECOGNITION_MODEL

    if not api_url or not api_key or not model:
        return "Error: Visual recognition is not configured. Please set VISUAL_RECOGNITION_API_URL, VISUAL_RECOGNITION_API_KEY, and VISUAL_RECOGNITION_MODEL."

    if not api_url.endswith("/chat/completions"):
        api_url = api_url + "/chat/completions"

    image_urls = [image_url] if isinstance(image_url, str) else image_url

    if not image_urls:
        return "Error: No image URL provided."

    content_parts = [_build_image_content(url) for url in image_urls]

    if len(image_urls) == 1:
        content_parts.append({
            "type": "text",
            "text": "请详细描述这张图片的内容，包括图片中的文字（如果有的话）。"
        })
    else:
        content_parts.append({
            "type": "text",
            "text": "请描述这些图片之间的联系和区别，以及每张图片各自的内容，包括图片中的文字（如果有的话）。"
        })

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": content_parts
            }
        ],
        "max_completion_tokens": 2048,
    }

    client = httpx.Client(timeout=thinking_settings.LLM_REQUEST_TIMEOUT_SECONDS)
    try:
        response = client.post(api_url, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            return "Error: No content returned from visual recognition API."
        return content
    except httpx.HTTPStatusError as e:
        return f"Error: Visual recognition API returned HTTP {e.response.status_code}: {e.response.text[:500]}"
    except httpx.RequestError as e:
        return f"Error: Failed to connect to visual recognition API: {str(e)}"
    except Exception as e:
        return f"Error: Unexpected error during visual recognition: {str(e)}"
    finally:
        client.close()


VISUAL_RECOGNITION_AUTHORIZED_IMPORTS = ["json", "httpx"]
