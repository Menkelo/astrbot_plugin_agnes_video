import asyncio
import base64
import io
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
_DEFAULT_KF_PROMPT = "在关键帧之间生成平滑自然的过渡，保持视觉一致和自然的镜头运动"

_AGNES_RATIOS: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (1024, 1024),
    "4:3": (1024, 768),
    "3:4": (768, 1024),
}
_DURATION_FRAMES: dict[int, int] = {3: 81, 5: 121, 10: 241, 18: 441}
_DURATION_TIERS = sorted(_DURATION_FRAMES)
_FRAME_RATE = 24

_RATIO_RE = re.compile(
    r"(?<!\d)(16\s*[:：]\s*9|9\s*[:：]\s*16|1\s*[:：]\s*1|4\s*[:：]\s*3|3\s*[:：]\s*4)(?!\d)",
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
    """Agnes AI Video V2.0 视频生成插件。

    根据消息中附带或引用的图片自动选择生成模式：
    - 无图片：文生视频
    - 一张图片：图生视频
    - 两张及以上图片：关键帧动画
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._tasks: dict[str, asyncio.Task] = {}
        self._keys: list[str] = self._resolve_keys()
        self._key_index = 0

    def _resolve_keys(self) -> list[str]:
        """解析 API Key 列表（`api_keys`，每项一个 Key）。

        多个 Key 时插件会轮询使用，绕过每分钟 1 个视频任务的限流。
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
        return str(self.config.get("base_url", "https://apihub.agnes-ai.com")).rstrip(
            "/"
        )

    def _api_key_ok(self) -> bool:
        return bool(self._keys and self._keys[0])

    def _build_payload(self, aspect_ratio: str, duration_seconds: int) -> dict:
        """根据宽高比与时长构建视频生成公共参数。

        Args:
            aspect_ratio: 宽高比，如 16:9 / 9:16 / 1:1 / 4:3 / 3:4。
            duration_seconds: 目标时长（秒），须为支持档位 3/5/10/18。

        Returns:
            公共参数 dict。
        """
        width, height = _AGNES_RATIOS.get(aspect_ratio, _AGNES_RATIOS["4:3"])
        num_frames = _DURATION_FRAMES.get(duration_seconds, 121)
        params = {
            "model": str(self.config.get("model", "agnes-video-v2.0")),
            "width": width,
            "height": height,
            "num_frames": num_frames,
            "frame_rate": _FRAME_RATE,
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
    def _map_duration(seconds: int) -> int:
        """将目标秒数映射到最接近的支持时长档位。"""
        return min(_DURATION_TIERS, key=lambda t: abs(t - seconds))

    @staticmethod
    def _map_ratio(ratio: float) -> str | None:
        """将图片宽高比映射到最接近的支持比例。"""
        if not ratio or ratio <= 0:
            return None
        return min(
            _AGNES_RATIOS,
            key=lambda ar: abs((_AGNES_RATIOS[ar][0] / _AGNES_RATIOS[ar][1]) - ratio),
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
        Agnes 图生视频 / 关键帧的 image 参数接受可公开访问的 URL 或
        Data URI Base64（Image API 明确支持，Video API 兼容该约定）。
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
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {data}")
            return data

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
        """查询视频生成任务状态（推荐方式，使用 video_id）。"""
        url = f"{self._base_url}/agnesapi"
        params = {"video_id": video_id}
        timeout = aiohttp.ClientTimeout(total=60)
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                url, headers=self._headers(), params=params, timeout=timeout
            ) as resp,
        ):
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {data}")
            return data

    @staticmethod
    def _extract_video_url(data: dict) -> str:
        """从任务响应中提取最终视频 URL。"""
        return ((data.get("metadata") or {}).get("url")) or data.get("url") or ""

    @staticmethod
    def _extract_error_message(err: str) -> str:
        """从错误字符串中提取简短的错误信息。"""
        m = re.search(r"'message':\s*'([^']*)'", err)
        if m:
            return m.group(1).strip()[:200]
        m = re.search(r"HTTP \d+: (.+)", err)
        if m:
            return m.group(1).strip()[:200]
        return err.strip()[:200]

    async def _safe_send(self, umo: str, chain: MessageChain):
        try:
            await self.context.send_message(umo, chain)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[AgnesVideo] 主动发送消息失败: {e}")

    async def _deliver_video(self, umo: str, video_url: str):
        """直接发送视频消息；失败重试一次，仍失败则只给简短提示。"""
        for attempt in range(2):
            try:
                await self.context.send_message(
                    umo, MessageChain(chain=[Video.fromURL(url=video_url)])
                )
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
            MessageChain().message(
                "视频生成完成，但当前消息平台发送视频失败，请稍后重试。"
            ),
        )

    async def _poll_and_deliver(self, video_id: str, umo: str):
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
                    message = self._extract_error_message(err)
                    await self._safe_send(
                        umo,
                        MessageChain().message(
                            f"视频生成失败：{message or '任务已终止'}，请修改提示词后重试。"
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
                    await self._deliver_video(umo, video_url)
                else:
                    await self._safe_send(
                        umo,
                        MessageChain().message(
                            "视频生成完成，但响应中未找到视频链接。"
                        ),
                    )
                return
            if status == "failed":
                await self._safe_send(
                    umo,
                    MessageChain().message(f"视频生成失败：{data.get('error')}"),
                )
                return
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        await self._safe_send(
            umo,
            MessageChain().message(
                f"视频生成时间超过预期（>{max_poll_time}s），仍在后台生成中，"
                "完成后将自动发送视频，请稍候。"
            ),
        )

    async def _submit(self, event: AstrMessageEvent, payload: dict, mode_desc: str):
        """创建任务并启动后台轮询。"""
        if not self._api_key_ok():
            yield event.plain_result(
                "未配置 Agnes API Key，请在插件配置中填写 api_keys 列表。"
            )
            return
        try:
            data = await self._create_task_retry(payload)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[AgnesVideo] 创建任务失败: {e}")
            if "HTTP 429" in str(e):
                yield event.plain_result(
                    f"{mode_desc}任务创建失败：触发了 Agnes 接口限流。\n"
                    "Agnes 限制每个 API Key 每分钟最多创建 1 个视频任务，"
                    "若配置了多个 Key 已自动轮换，请等待约 1 分钟后再试。"
                )
            else:
                yield event.plain_result(f"{mode_desc}任务创建失败：{e}")
            return
        video_id = data.get("video_id") or data.get("task_id") or data.get("id")
        if not video_id:
            yield event.plain_result(f"创建任务失败：响应中缺少任务 ID。{data}")
            return
        umo = event.unified_msg_origin
        self._tasks[video_id] = asyncio.get_running_loop().create_task(
            self._poll_and_deliver(video_id, umo)
        )
        yield event.plain_result(
            f"{mode_desc}任务已创建，正在生成中，完成后将自动发送视频，请稍候……"
        )

    # ============================== 命令 ==============================

    @filter.command("vgen")
    async def vgen(self, event: AstrMessageEvent, text: GreedyStr):
        """AI 视频生成。发消息时附带图片或引用含图片的消息，自动切换生成模式。"""
        text = text.strip()

        images, saw_image, skipped = await self._collect_images(event)
        if saw_image and not images:
            yield event.plain_result(
                "已检测到图片，但未能解析出可供 Agnes 使用的图片（URL 或 base64）。\n"
                "已尝试组件 URL、协议端 get_image、AstrBot 文件服务以及本地 base64 转换，均未成功"
                "（详见 AstrBot 日志中的 AgnesVideo 提示）。\n"
                "也可以在命令中直接粘贴可公开访问的图片 URL。"
            )
            return

        text_urls = re.findall(r"https?://\S+", text)
        for u in text_urls:
            if u not in images:
                images.append(u)
        clean_text = re.sub(r"https?://\S+", "", text).strip()
        prompt, prompt_aspect, prompt_duration = self._extract_prompt_meta(clean_text)

        if skipped:
            if images:
                yield event.plain_result(
                    f"提示：有 {skipped} 张图片未能解析出可供 Agnes 使用的图片，已忽略，"
                    "将使用其余图片/URL 继续。"
                )
            else:
                yield event.plain_result(
                    f"有 {skipped} 张图片未能解析出可供 Agnes 使用的图片，且没有其他可用图片或 URL。\n"
                    "如需使用这些图片，请直接粘贴其公开 URL。"
                )
                return

        aspect = prompt_aspect
        if not aspect and images:
            ratio = await self._detect_image_ratio(images[0])
            aspect = self._map_ratio(ratio) if ratio else None
        if not aspect:
            aspect = str(self.config.get("aspect_ratio", "4:3"))
        if aspect not in _AGNES_RATIOS:
            aspect = "4:3"

        duration = prompt_duration or int(self.config.get("duration_seconds", 5))
        duration = self._map_duration(duration)
        payload = self._build_payload(aspect, duration)

        if not images:
            if not prompt:
                yield event.plain_result(
                    "用法：/vgen <提示词>\n"
                    "发消息时附带图片可进行图生视频；"
                    "引用含两张及以上图片的消息可进行关键帧动画。\n"
                    "提示词中可指定宽高比（如 16:9）与时长（如 10s）。"
                )
                return
            payload["prompt"] = prompt
            async for result in self._submit(event, payload, "文生视频"):
                yield result
            return

        if len(images) == 1:
            payload["prompt"] = prompt or _DEFAULT_I2V_PROMPT
            payload["image"] = images[0]
            async for result in self._submit(event, payload, "图生视频"):
                yield result
            return

        payload["prompt"] = prompt or _DEFAULT_KF_PROMPT
        payload["extra_body"] = {"image": images, "mode": "keyframes"}
        async for result in self._submit(event, payload, "关键帧动画"):
            yield result

    async def terminate(self):
        """插件卸载时取消后台轮询任务。"""
        for video_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
