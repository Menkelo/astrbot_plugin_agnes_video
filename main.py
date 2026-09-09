import asyncio
import base64
import io
import json
import os
import re

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Reply, Video
from astrbot.api.star import Context, Star

try:
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:
    GreedyStr = str

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

_DEFAULT_I2V_PROMPT = "让画面自然运动起来，保持主体、风格和场景一致，电影质感"
_DEFAULT_KF_PROMPT = "在两帧之间生成平滑自然的过渡，保持视觉一致和自然的镜头运动"
_DEFAULT_REF_PROMPT = "结合附带的参考图片内容与风格生成连贯的视频，保持主体外观与风格一致，电影质感"

_DEFAULT_MODEL = "agnes-video-2.5-flash"
_DEFAULT_BASE_URL = "https://apihub.agnes-ai.com"
_SIZE = "720P"

_VIDEO_RATIOS: set[str] = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
_RATIO_DIMS: dict[str, tuple[int, int]] = {
    "21:9": (2100, 900),
    "16:9": (1920, 1080),
    "4:3": (1200, 900),
    "1:1": (1024, 1024),
    "3:4": (900, 1200),
    "9:16": (1080, 1920),
}
_MIN_SECONDS = 4
_MAX_SECONDS = 12
_MAX_REF_IMAGES = 5

_RATIO_RE = re.compile(
    r"(?<!\d)(21\s*[:：]\s*9|16\s*[:：]\s*9|9\s*[:：]\s*16|1\s*[:：]\s*1|4\s*[:：]\s*3|3\s*[:：]\s*4)(?!\d)",
    re.IGNORECASE,
)
_RATIO_ALIASES = [
    (re.compile(r"横屏|宽屏", re.IGNORECASE), "16:9"),
    (re.compile(r"竖屏|竖图", re.IGNORECASE), "9:16"),
    (re.compile(r"方图|方形", re.IGNORECASE), "1:1"),
]
_DURATION_RE = re.compile(r"(?<!\d)(\d{1,3})\s*(?:s|秒)(?!\d)", re.IGNORECASE)


def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


class AgnesVideo(Star):
    """基于 Agnes AI Video 2.5 Flash 的 AI 视频生成插件。

    根据消息中附带或引用的图片数量自动选择生成模式：
    - 无图片：文生视频（mode=text）
    - 一张图片：图生视频（mode=keyframe，first_frame）
    - 两张图片：首尾帧关键帧动画（mode=keyframe，first_frame + last_frame）
    - 三张及以上：多图参考生成（mode=reference，最多 5 张）
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._tasks: dict[str, asyncio.Task] = {}
        self._keys: list[str] = self._resolve_keys()
        self._key_index = 0

    def _resolve_keys(self) -> list[str]:
        """解析 API Key 列表（`api_keys`，每项一个 Key）。

        多个 Key 时插件会轮询使用，规避单 Key 限流。
        """
        keys: list[str] = []
        for k in self.config.get("api_keys") or []:
            if isinstance(k, str) and k.strip():
                keys.append(k.strip())
        return keys or []

    def _next_key(self) -> str:
        """按顺序轮询返回下一个 API Key。"""
        if not self._keys:
            return ""
        key = self._keys[self._key_index % len(self._keys)]
        self._key_index += 1
        return key

    # ============================== 内部工具 ==============================

    def _headers(self, api_key: str = "") -> dict:
        key = api_key or (self._keys[0] if self._keys else "")
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    @property
    def _base_url(self) -> str:
        return str(self.config.get("base_url", _DEFAULT_BASE_URL)).rstrip("/")

    @property
    def _model(self) -> str:
        return str(self.config.get("model", _DEFAULT_MODEL) or _DEFAULT_MODEL).strip()

    def _api_key_ok(self) -> bool:
        return bool(self._keys and self._keys[0])

    def _build_payload(
        self, mode: str, aspect_ratio: str, seconds: int
    ) -> dict:
        """构建 Agnes Video 2.5 Flash 的任务请求公共参数。

        Args:
            mode: text / keyframe / reference。
            aspect_ratio: 输出宽高比，如 16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9。
            seconds: 目标时长（秒），会被钳制到 4-12。

        Returns:
            公共参数 dict（不含媒体与 prompt，由调用方补充）。
        """
        params = {
            "model": self._model,
            "mode": mode,
            "seconds": str(max(_MIN_SECONDS, min(_MAX_SECONDS, seconds))),
            "size": _SIZE,
            "aspect_ratio": aspect_ratio if aspect_ratio in _VIDEO_RATIOS else "16:9",
        }
        seed = int(self.config.get("seed", -1))
        if seed >= 0:
            params["seed"] = seed
        return params

    @staticmethod
    def _normalize_ratio(value: str) -> str:
        return re.sub(r"\s+", "", value).replace("：", ":").lower()

    @classmethod
    def _extract_prompt_meta(cls, prompt: str) -> tuple[str, str | None, int | None]:
        """从提示词中提取并移除比例与时长指定。

        Returns:
            (clean_prompt, aspect_ratio, duration_seconds)：均从提示词中解析，
            未指定时为 None。
        """
        text = str(prompt or "")
        aspect = None
        duration = None

        m = _RATIO_RE.search(text)
        if m:
            aspect = cls._normalize_ratio(m.group(1))
            text = text[: m.start()] + " " + text[m.end() :]

        if not aspect:
            for pattern, ar in _RATIO_ALIASES:
                mm = pattern.search(text)
                if mm:
                    aspect = ar
                    text = pattern.sub(" ", text, count=1)
                    break

        m = _DURATION_RE.search(text)
        if m:
            duration = int(m.group(1))
            text = text[: m.start()] + " " + text[m.end() :]

        text = re.sub(r"\s+", " ", text).strip()
        return text, aspect, duration

    @staticmethod
    def _map_ratio(ratio: float) -> str | None:
        """将图片宽高比映射到最接近的支持比例。"""
        if not ratio or ratio <= 0:
            return None
        return min(
            _RATIO_DIMS,
            key=lambda ar: abs((_RATIO_DIMS[ar][0] / _RATIO_DIMS[ar][1]) - ratio),
        )

    async def _detect_image_ratio(self, ref: str) -> float | None:
        """获取图片引用（URL / Data URI / 本地路径）的宽高比。

        返回宽高比 w/h；无法读取时返回 None。
        """
        if PILImage is None:
            return None
        data: bytes | None = None
        if ref.startswith("data:"):
            try:
                data = base64.b64decode(ref.split(",", 1)[1])
            except Exception:  # noqa: BLE001
                return None
        elif ref.startswith(("http://", "https://")):
            try:
                timeout = aiohttp.ClientTimeout(total=20)
                async with (
                    aiohttp.ClientSession() as session,
                    session.get(ref, timeout=timeout) as resp,
                ):
                    data = await resp.read()
            except Exception:  # noqa: BLE001
                return None
        else:
            path = ref.removeprefix("file://")
            if not (path and os.path.isfile(path)):
                return None
            try:
                data = await asyncio.to_thread(_read_file, path)
            except Exception:  # noqa: BLE001
                return None
        if not data:
            return None

        def _ratio() -> float | None:
            with io.BytesIO(data) as buf:
                img = PILImage.open(buf)
                w, h = img.size
                if w <= 0 or h <= 0:
                    return None
                return w / h

        try:
            return await asyncio.to_thread(_ratio)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _image_url(img: Image) -> str:
        """从 Image 组件中提取可公开访问的 URL。"""
        url = (img.url or "").strip()
        if url.startswith(("http://", "https://")):
            return url
        file_ = (img.file or "").strip()
        if file_.startswith(("http://", "https://")):
            return file_
        return ""

    async def _resolve_via_onebot(
        self, event: AstrMessageEvent, img: Image
    ) -> str | None:
        """通过 OneBot 协议端 get_image API 解析图片 URL。

        当消息平台未直接下发图片链接时，可凭 file/file_id/image 等标识向协议端
        请求图片的下载地址。适用于 aiocqhttp（NapCat / Lagrange 等）平台。
        """
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
        if not callable(call_action):
            return None
        refs = []
        for field in (img.file, img.url, img.path):
            if isinstance(field, str) and field.strip():
                refs.append(field.strip())
        if not refs:
            return None
        for ref in refs:
            for params in (
                {"file": ref},
                {"file_id": ref},
                {"image": ref},
                {"id": ref},
            ):
                try:
                    ret = await call_action("get_image", **params)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[AgnesVideo] get_image({params}) 失败: {e}")
                    continue
                data = (ret or {}).get("data") or {}
                url = data.get("url") or data.get("file")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    return url
        return None

    async def _resolve_image_url(
        self, event: AstrMessageEvent, img: Image
    ) -> tuple[str | None, str]:
        """解析单张图片为可供 Agnes 使用的 URL。

        解析顺序：组件自带 URL → 协议端 get_image → AstrBot 文件服务。

        Returns:
            (url, source)：url 为可用链接（None 表示失败），source 说明解析来源。
        """
        url = self._image_url(img)
        if url:
            return url, "component"
        url = await self._resolve_via_onebot(event, img)
        if url:
            return url, "onebot_api"
        try:
            public_url = await img.register_to_file_service()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[AgnesVideo] register_to_file_service 失败: {e}")
            public_url = None
        if public_url and public_url.startswith(("http://", "https://")):
            return public_url, "file_service"
        data_uri = await self._to_data_uri(img)
        if data_uri:
            return data_uri, "base64"
        return None, "none"

    async def _to_data_uri(self, img: Image) -> str | None:
        """将图片转换为 Data URI Base64，作为无法获得公开 URL 时的兜底。

        适用于 aiocqhttp 协议端未下发图片链接、仅提供本地文件路径的场景。
        Agnes Video 2.5 的图片参数接受公开 URL 或 Data URI Base64。
        """
        try:
            b64 = await img.convert_to_base64()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[AgnesVideo] convert_to_base64 失败: {e}")
            return None
        if not b64:
            return None
        return f"data:image/jpeg;base64,{b64}"

    async def _collect_images(
        self, event: AstrMessageEvent
    ) -> tuple[list[str], bool, int]:
        """收集事件中的图片 URL。

        覆盖两种调用方式：
        - 消息中附带图片（Image 组件）。
        - 引用/回复一条含图片的消息（Reply 组件的 chain 中嵌套的 Image 组件）。

        Returns:
            (urls, saw_image, skipped)：urls 为可用的图片 URL 列表；
            saw_image 表示消息中是否出现了图片组件；
            skipped 表示检测到但未能解析出 URL 的图片数量。
        """
        urls: list[str] = []
        seen: set[str] = set()
        saw_image = False
        skipped = 0

        message = getattr(event.message_obj, "message", None) or []
        candidates: list[Image] = []
        for comp in message:
            if isinstance(comp, Image):
                saw_image = True
                candidates.append(comp)
            elif isinstance(comp, Reply):
                for sub in comp.chain or []:
                    if isinstance(sub, Image):
                        saw_image = True
                        candidates.append(sub)

        for img in candidates:
            url, _ = await self._resolve_image_url(event, img)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
            elif not url:
                skipped += 1
                logger.warning(
                    f"[AgnesVideo] 图片解析失败，组件字段: "
                    f"url={img.url!r} file={img.file!r} path={img.path!r}"
                )
            elif url in seen:
                logger.debug(f"[AgnesVideo] 图片 URL 重复，已忽略: {url}")
        return urls, saw_image, skipped

    async def _read_json(self, resp: aiohttp.ClientResponse) -> dict:
        """读取响应体并解析为 dict，兼容空体 / 非 JSON 响应。

        返回解析后的 dict；解析失败或 HTTP >= 400 时抛出带状态码的 RuntimeError，
        避免 aiohttp 的 JSONDecodeError（如 "Expecting value"）直接暴露给用户。
        """
        try:
            text = await resp.text()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"HTTP {resp.status}: 读取响应体失败: {e}") from e
        if not text.strip():
            raise RuntimeError(f"HTTP {resp.status}: 空响应体")
        try:
            data = json.loads(text)
        except Exception as e:  # noqa: BLE001
            snippet = text.strip()[:200]
            raise RuntimeError(
                f"HTTP {resp.status}: 响应非 JSON（{e}）：{snippet}"
            ) from e
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}: {data}")
        return data

    async def _create_task(self, payload: dict) -> dict:
        """创建视频生成任务（使用轮询选出的 API Key）。"""
        url = f"{self._base_url}/v1/videos"
        timeout = aiohttp.ClientTimeout(total=60)
        api_key = self._next_key()
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                url, headers=self._headers(api_key), json=payload, timeout=timeout
            ) as resp,
        ):
            return await self._read_json(resp)

    async def _create_task_retry(self, payload: dict) -> dict:
        """创建视频任务；遇到 429 限流时自动切换到下一个 Key 重试。

        单个 Key 时只尝试一次；多个 Key 时逐个轮换尝试。
        """
        attempts = max(len(self._keys), 1)
        last_err: Exception | None = None
        for i in range(attempts):
            try:
                return await self._create_task(payload)
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning(
                    f"[AgnesVideo] 创建任务第 {i + 1} 次尝试失败（Key #{i % max(len(self._keys), 1)}）: {e}"
                )
                if "HTTP 429" not in str(e):
                    break
        if last_err is None:
            raise RuntimeError("创建视频任务失败")
        raise last_err

    async def _query_task(self, video_id: str) -> dict:
        """查询视频生成任务状态。

        GET {base}/agnesapi?video_id=...&model_name=agnes-video-2.5-flash
        （携带 model_name 以兼容 keyframe / reference 模式的任务查询）。
        """
        url = f"{self._base_url}/agnesapi"
        params = {"video_id": video_id, "model_name": self._model}
        timeout = aiohttp.ClientTimeout(total=60)
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                url, headers=self._headers(), params=params, timeout=timeout
            ) as resp,
        ):
            return await self._read_json(resp)

    @staticmethod
    def _extract_video_url(data: dict) -> str:
        """从任务响应中提取最终视频 URL（completed 后 metadata.url）。"""
        return ((data.get("metadata") or {}).get("url")) or data.get("url") or ""

    @staticmethod
    def _message_id(event: AstrMessageEvent) -> str:
        """从事件中提取被触发消息的 ID，用于构造 Reply 引用。"""
        try:
            mid = getattr(event.message_obj, "message_id", "")
            return str(mid) if mid else ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _quoted_chain(text: str, reply_id: str = "") -> MessageChain:
        """构造文本消息链；reply_id 非空时在消息首部插入 Reply 引用。"""
        chain = MessageChain().message(text)
        if reply_id:
            chain.chain.insert(0, Reply(id=reply_id))
        return chain

    def _quoted_result(self, event: AstrMessageEvent, text: str):
        """构造同步回复结果（yield 用），自动引用触发原消息。"""
        return event.chain_result(self._quoted_chain(text, self._message_id(event)).chain)

    async def _safe_send(self, umo: str, chain: MessageChain):
        try:
            await self.context.send_message(umo, chain)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[AgnesVideo] 主动发送消息失败: {e}")

    async def _deliver_video(self, umo: str, video_url: str, reply_id: str = ""):
        """直接发送视频消息（带引用）；失败重试一次，仍失败则只给简短提示。"""
        chain = MessageChain(chain=[Video.fromURL(url=video_url)])
        if reply_id:
            chain.chain.insert(0, Reply(id=reply_id))
        for attempt in range(2):
            try:
                await self.context.send_message(umo, chain)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[AgnesVideo] 视频消息发送失败（第 {attempt + 1} 次）: {e}"
                )
                if attempt == 0:
                    await asyncio.sleep(1)
        logger.info(f"[AgnesVideo] 视频发送失败，下载链接（仅供排查）: {video_url}")
        await self._safe_send(
            umo,
            self._quoted_chain(
                "视频生成完成，但当前消息平台发送视频失败，请稍后重试。",
                reply_id,
            ),
        )

    async def _poll_and_deliver(self, video_id: str, umo: str, reply_id: str = ""):
        """后台轮询任务，完成后将视频推送给用户。"""
        max_poll_time = int(self.config.get("max_poll_time", 600))
        poll_interval = int(self.config.get("poll_interval", 5))
        elapsed = 0
        while elapsed < max_poll_time:
            try:
                data = await self._query_task(video_id)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                logger.error(f"[AgnesVideo] 查询任务 {video_id} 失败: {err}")
                if re.search(r"HTTP [45]\d\d", err):
                    await self._safe_send(
                        umo,
                        self._quoted_chain(
                            f"视频生成任务查询失败，原始错误：\n{err}", reply_id
                        ),
                    )
                    return
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                continue
            status = data.get("status")
            if status == "completed":
                video_url = self._extract_video_url(data)
                if video_url:
                    await self._deliver_video(umo, video_url, reply_id)
                else:
                    await self._safe_send(
                        umo,
                        self._quoted_chain(
                            "视频生成完成，但响应中未找到视频链接。", reply_id
                        ),
                    )
                return
            if status == "failed":
                err = (data.get("error") or {}).get("message") or "任务已终止"
                await self._safe_send(
                    umo,
                    self._quoted_chain(f"视频生成失败：{err}", reply_id),
                )
                return
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        await self._safe_send(
            umo,
            self._quoted_chain(
                f"视频生成时间超过预期（>{max_poll_time}s），仍在后台生成中，"
                "完成后将自动发送视频，请稍候。",
                reply_id,
            ),
        )

    async def _submit(self, event: AstrMessageEvent, payload: dict, mode_desc: str):
        """创建任务并启动后台轮询。"""
        if not self._api_key_ok():
            yield self._quoted_result(
                event, "未配置 Agnes API Key，请在插件配置中填写 api_keys 列表。"
            )
            return
        try:
            data = await self._create_task_retry(payload)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[AgnesVideo] 创建任务失败: {e}")
            if "HTTP 429" in str(e):
                hint = (
                    "触发了 Agnes API 限流（HTTP 429），"
                    "若配置了多个 Key 已自动轮换，请稍后重试。"
                )
                yield self._quoted_result(event, f"{mode_desc}任务创建失败：{hint}")
            else:
                yield self._quoted_result(
                    event, f"{mode_desc}任务创建失败：{e}"
                )
            return
        video_id = data.get("video_id") or data.get("task_id") or data.get("id")
        if not video_id:
            yield self._quoted_result(
                event, f"创建任务失败：响应中缺少任务 ID。{data}"
            )
            return
        umo = event.unified_msg_origin
        reply_id = self._message_id(event)
        self._tasks[video_id] = asyncio.get_running_loop().create_task(
            self._poll_and_deliver(video_id, umo, reply_id)
        )
        yield self._quoted_result(
            event,
            f"{mode_desc}任务已创建，正在生成中，完成后将自动发送视频，请稍候……",
        )

    # ============================== 命令 ==============================

    @filter.command("vgen")
    async def vgen(self, event: AstrMessageEvent, text: GreedyStr):
        """AI 视频生成（Agnes Video 2.5 Flash）。发消息时附带图片或引用含图片的消息，自动切换生成模式。"""
        text = text.strip()

        images, saw_image, skipped = await self._collect_images(event)
        if saw_image and not images:
            yield self._quoted_result(
                event,
                "已检测到图片，但未能解析出可供视频生成使用的图片（URL 或 base64）。\n"
                "已尝试组件 URL、协议端 get_image、AstrBot 文件服务以及本地 base64 转换，均未成功"
                "（详见 AstrBot 日志中的 AgnesVideo 提示）。\n"
                "也可以在命令中直接粘贴可公开访问的图片 URL。",
            )
            return

        text_urls = re.findall(r"https?://\S+", text)
        for u in text_urls:
            if u not in images:
                images.append(u)
        clean_text = re.sub(r"https?://\S+", "", text).strip()
        prompt, prompt_aspect, prompt_duration = self._extract_prompt_meta(clean_text)

        if len(images) > _MAX_REF_IMAGES:
            yield self._quoted_result(
                event,
                f"提示：agnes-video-2.5-flash 单次最多使用 {_MAX_REF_IMAGES} 张参考图片，"
                f"已截断至前 {_MAX_REF_IMAGES} 张，将使用其余参数继续。",
            )
            images = images[:_MAX_REF_IMAGES]

        if skipped:
            if images:
                yield self._quoted_result(
                    event,
                    f"提示：有 {skipped} 张图片未能解析出可供视频生成使用的图片，已忽略，"
                    "将使用其余图片/URL 继续。",
                )
            else:
                yield self._quoted_result(
                    event,
                    f"有 {skipped} 张图片未能解析出可供视频生成使用的图片，且没有其他可用图片或 URL。\n"
                    "如需使用这些图片，请直接粘贴其公开 URL。",
                )
                return

        aspect = prompt_aspect
        if not aspect and images:
            ratio = await self._detect_image_ratio(images[0])
            aspect = self._map_ratio(ratio) if ratio else None
        if not aspect:
            aspect = str(self.config.get("aspect_ratio", "16:9"))
        if aspect not in _VIDEO_RATIOS:
            aspect = "16:9"

        duration = prompt_duration or int(self.config.get("duration_seconds", 5))

        if not images:
            if not prompt:
                yield self._quoted_result(
                    event,
                    "用法：/vgen <提示词>\n"
                    "发消息时附带一张图片可进行图生视频；"
                    "两张图片进行首尾帧关键帧动画；"
                    "三张及以上进行多图参考生成。\n"
                    "提示词中可指定宽高比（如 16:9）与时长（如 10s，支持 4-12 秒）。",
                )
                return
            payload = self._build_payload("text", aspect, duration)
            payload["prompt"] = prompt
            async for result in self._submit(event, payload, "文生视频"):
                yield result
            return

        if len(images) == 1:
            payload = self._build_payload("keyframe", aspect, duration)
            payload["prompt"] = prompt or _DEFAULT_I2V_PROMPT
            payload["first_frame"] = images[0]
            async for result in self._submit(event, payload, "图生视频"):
                yield result
            return

        if len(images) == 2:
            payload = self._build_payload("keyframe", aspect, duration)
            payload["prompt"] = prompt or _DEFAULT_KF_PROMPT
            payload["first_frame"] = images[0]
            payload["last_frame"] = images[1]
            async for result in self._submit(event, payload, "首尾帧关键帧动画"):
                yield result
            return

        payload = self._build_payload("reference", aspect, duration)
        payload["prompt"] = prompt or _DEFAULT_REF_PROMPT
        payload["images"] = images
        async for result in self._submit(event, payload, "多图参考生成"):
            yield result

    async def terminate(self):
        """插件卸载时取消后台轮询任务。"""
        for video_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
