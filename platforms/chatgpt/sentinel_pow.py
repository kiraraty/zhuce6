"""Sentinel proof-of-work helpers for OpenAI auth flows."""

from __future__ import annotations

import base64
import json
import random
import time
import uuid

from .constants import OPENAI_USER_AGENT


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, *, device_id: str | None = None, user_agent: str | None = None) -> None:
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or OPENAI_USER_AGENT
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list[object]:
        now_str = time.strftime(
            "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
            time.gmtime(),
        )
        perf_now = random.uniform(1000, 50000)
        time_origin = time.time() * 1000 - perf_now
        nav_prop = random.choice(
            [
                "vendorSub",
                "productSub",
                "vendor",
                "maxTouchPoints",
                "scheduling",
                "userActivation",
                "doNotTrack",
                "geolocation",
                "connection",
                "plugins",
                "mimeTypes",
                "pdfViewerEnabled",
                "webkitTemporaryStorage",
                "webkitPersistentStorage",
                "hardwareConcurrency",
                "cookieEnabled",
                "credentials",
                "mediaDevices",
                "permissions",
                "locks",
                "ink",
            ]
        )
        nav_val = f"{nav_prop}-undefined"
        return [
            "1920x1080",
            now_str,
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            nav_val,
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time_origin,
        ]

    @staticmethod
    def _base64_encode(data: object) -> str:
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _run_check(
        self,
        *,
        start_time: float,
        seed: str,
        difficulty: str,
        config: list[object],
        nonce: int,
    ) -> str | None:
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        data = self._base64_encode(config)
        hash_hex = self._fnv1a_32(seed + data)
        diff_len = len(difficulty)
        if hash_hex[:diff_len] <= difficulty:
            return data + "~S"
        return None

    def generate_token(self, *, seed: str | None = None, difficulty: str | None = None) -> str:
        start_time = time.time()
        config = self._get_config()
        resolved_seed = seed if seed is not None else self.requirements_seed
        resolved_difficulty = str(difficulty or "0")
        for nonce in range(self.MAX_ATTEMPTS):
            result = self._run_check(
                start_time=start_time,
                seed=resolved_seed,
                difficulty=resolved_difficulty,
                config=config,
                nonce=nonce,
            )
            if result:
                return "gAAAAAB" + result
        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))

    def generate_requirements_token(self) -> str:
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._base64_encode(config)
