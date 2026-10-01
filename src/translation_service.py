"""Configurable OpenAI-compatible translation API for IDOLY PRIDE CSV rows."""

import json
from http.client import RemoteDisconnected
import random
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class TranslationService:
    def __init__(self, api_base: str, api_key: str, model: str, prompt: str,
                 temperature: float = 0.2, max_tokens: int = 4096,
                 thinking: str = "disabled"):
        if not api_base or not api_key or not model:
            raise ValueError("API base URL, key, and model are required")
        self.url = api_base.rstrip("/")
        if not self.url.endswith("/chat/completions"):
            self.url += "/chat/completions"
        address = urlparse(self.url)
        if not address.hostname or (address.scheme != "https" and not (
                address.scheme == "http" and address.hostname in
                {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("API URL must use HTTPS, except for localhost testing")
        self.api_key = api_key
        self.model = model
        self.prompt = prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        if thinking not in {"disabled", "enabled"}:
            raise ValueError("Thinking mode must be enabled or disabled")
        self.thinking = thinking

    def translate_batch(self, lines: list[dict[str, str]],
                        glossary: dict[str, str]) -> dict[str, str]:
        context = "\n".join(line["source"] + " " + line["speaker"] for line in lines)
        relevant_names = {source: target for source, target in glossary.items()
                          if source in context}
        task = {"glossary": relevant_names, "lines": lines,
                "output_format": {"translations": [{"id": "original field ID",
                                                    "translation": "Simplified Chinese"}]}}
        if any(line.get("term_preferences") for line in lines):
            task["terminology_instruction"] = (
                "Apply each line's term_preferences only to that line. "
                "retain means preserve the original spelling; prefer means use the target "
                "when the source denotes that term in context. Do not replace substrings blindly.")
        body = {"model": self.model, "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "thinking": {"type": self.thinking},
                "reasoning_effort": "none" if self.thinking == "disabled" else "high",
                "messages": [{"role": "system", "content": self.prompt},
                             {"role": "user", "content": json.dumps(task, ensure_ascii=False)}]}
        request = Request(self.url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": f"Bearer {self.api_key}",
                                   "Content-Type": "application/json"}, method="POST")
        for attempt in range(6):
            try:
                with urlopen(request, timeout=120) as response:
                    payload = json.load(response)
                break
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 5:
                    raise RuntimeError(f"Translation API returned HTTP {exc.code}") from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = max(0, min(float(retry_after), 60)) if retry_after else 0
                except ValueError:
                    delay = 0
            except (URLError, RemoteDisconnected, TimeoutError) as exc:
                if attempt == 5:
                    raise RuntimeError("Translation API connection failed") from exc
                delay = 0
            time.sleep(max(delay, min(2 ** attempt + random.random(), 60)))
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("Model did not return text")
            if choice.get("finish_reason") == "length":
                raise ValueError("Model used the full output token limit before finishing")
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            if not content.startswith("{") and "{" in content and "}" in content:
                content = content[content.find("{"):content.rfind("}") + 1]
            output = json.loads(content)
            entries = output["translations"]
            if not isinstance(entries, list):
                raise ValueError("translations must be an array")
            result = {}
            for entry in entries:
                identifier = entry["id"]
                translation = entry.get("translation")
                if translation is None:
                    # Some OpenAI-compatible models rename this key while still
                    # returning the translated text and the correct field ID.
                    translation = entry["source"]
                if not isinstance(identifier, str) or not isinstance(translation, str):
                    raise ValueError("Translation IDs and values must be strings")
                if identifier in result:
                    raise ValueError(f"Duplicate translation ID: {identifier}")
                result[identifier] = translation
            return result
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Model response is not the required translation JSON") from exc
