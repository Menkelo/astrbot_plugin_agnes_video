import asyncio
import ipaddress
import re
from urllib.parse import urlparse

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Reply, Video
from astrbot.api.star import Context, Star

try:
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:
    GreedyStr = str

_DEFAULT_I2V_PROMPT = "让画面自然运动起来，保持主体、风格和场景一致，电影质感"
_DEFAULT_KF_PROMPT = "在关键帧之间生成平滑自然的过渡，保持视觉一致和自然的镜头运动"


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

    # ============================== 内部工具 ==============================

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config.get('api_key', '')}",
            "Content-Type": "application/json",
        }

    @property
    def _base_url(self) -> str:
        return str(self.config.get("base_url", "https://apihub.agnes-ai.com")).rstrip(
            "/"
        )

    def _api_key_ok(self) -> bool:
        return bool(self.config.get("api_key"))

    def _build_payload(self) -> tuple[dict | None, str | None]:
        """根据插件配置构建公共视频生成参数。

        Returns:
            (payload, error)：成功时 error 为 None，失败时 payload 为 None。
        """
        try:
            num_frames = int(self.config.get("num_frames", 121))
            if num_frames > 441:
                raise ValueError("num_frames 不能超过 441")
            if (num_frames - 1) % 8 != 0:
                raise ValueError("num_frames 必须遵循 8n+1 规则")
        except (TypeError, ValueError) as e:
            return None, str(e)
        params = {
            "model": str(self.config.get("model", "agnes-video-v2.0")),
            "width": int(self.config.get("width", 1152)),
            "height": int(self.config.get("height", 768)),
            "num_frames": int(self.config.get("num_frames", 121)),
            "frame_rate": int(self.config.get("frame_rate", 24)),
        }
        seed = int(self.config.get("seed", -1))
        if seed >= 0:
            params["seed"] = seed
        return params, None

    @staticmethod
    def _looks_public_url(url: str) -> bool:
        """粗略判断 URL 是否可能是公网可访问的（非内网/本机地址）。"""
        try:
            host = (urlparse(url).hostname or "").strip().lower()
        except Exception:  # noqa: BLE001
            return False
        if not host:
            return False
        if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(
            ".local"
        ):
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        return not (ip is not None and ip.is_private)

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

    async def _resolve_image_url(self, img: Image) -> tuple[str | None, bool]:
        """解析单张图片为可供 Agnes 使用的 URL。

        Returns:
            (url, public_likely)：url 为可用链接（None 表示失败）；
            public_likely 表示该链接是否可能被公网服务访问。
        """
        url = self._image_url(img)
        if url:
            return url, self._looks_public_url(url)
        try:
            public_url = await img.register_to_file_service()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[AgnesVideo] register_to_file_service 失败: {e}")
            return None, False
        if public_url and public_url.startswith(("http://", "https://")):
            return public_url, self._looks_public_url(public_url)
        return None, False

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
            url, _ = await self._resolve_image_url(img)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
            elif not url:
                skipped += 1
        return urls, saw_image, skipped

    async def _create_task(self, payload: dict) -> dict:
        """创建视频生成任务。"""
        url = f"{self._base_url}/v1/videos"
        timeout = aiohttp.ClientTimeout(total=60)
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                url, headers=self._headers(), json=payload, timeout=timeout
            ) as resp,
        ):
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {data}")
            return data

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

    async def _safe_send(self, umo: str, chain: MessageChain):
        try:
            await self.context.send_message(umo, chain)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[AgnesVideo] 主动发送消息失败: {e}")

    async def _deliver_video(self, umo: str, video_url: str):
        """先发送文本链接，再尝试直接发送视频消息。"""
        await self._safe_send(
            umo, MessageChain().message(f"视频生成完成！\n下载链接：{video_url}")
        )
        try:
            await self.context.send_message(
                umo, MessageChain(chain=[Video.fromURL(url=video_url)])
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[AgnesVideo] 视频消息发送失败（链接已发送）: {e}")

    async def _poll_and_deliver(self, video_id: str, umo: str):
        """后台轮询任务，完成后将视频推送给用户。"""
        max_poll_time = int(self.config.get("max_poll_time", 600))
        poll_interval = int(self.config.get("poll_interval", 5))
        elapsed = 0
        while elapsed < max_poll_time:
            try:
                data = await self._query_task(video_id)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[AgnesVideo] 查询任务 {video_id} 失败: {e}")
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
                f"任务等待超时（>{max_poll_time}s），视频仍在生成中。\n"
                f"任务 ID：{video_id}，可调大插件配置中的 max_poll_time 以延长等待。"
            ),
        )

    async def _submit(self, event: AstrMessageEvent, payload: dict, mode_desc: str):
        """创建任务、返回任务 ID，并启动后台轮询。"""
        if not self._api_key_ok():
            yield event.plain_result(
                "未配置 Agnes AI API Key，请在插件配置中填写 api_key。"
            )
            return
        try:
            data = await self._create_task(payload)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[AgnesVideo] 创建任务失败: {e}")
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
            f"{mode_desc}任务已创建！任务 ID：{video_id}\n"
            "正在生成中，完成后将自动发送视频，请稍候……"
        )

    # ============================== 命令 ==============================

    @filter.command("vgen")
    async def vgen(self, event: AstrMessageEvent, text: GreedyStr):
        """AI 视频生成。发消息时附带图片或引用含图片的消息，自动切换生成模式。"""
        text = text.strip()

        images, saw_image, skipped = await self._collect_images(event)
        if saw_image and not images:
            yield event.plain_result(
                "已检测到图片，但未能获取可公开访问的图片 URL。\n"
                "当前消息平台未下发图片链接，且未能通过 AstrBot 文件服务解析"
                "（请确认已在 AstrBot 配置中设置公网可达的 callback_api_base）。\n"
                "也可以在命令中直接粘贴可公开访问的图片 URL。"
            )
            return

        text_urls = re.findall(r"https?://\S+", text)
        for u in text_urls:
            if u not in images:
                images.append(u)
        prompt = re.sub(r"https?://\S+", "", text).strip()

        if skipped:
            if images:
                yield event.plain_result(
                    f"提示：有 {skipped} 张图片未能解析出可公开访问的链接，已忽略，"
                    "将使用其余图片/URL 继续。"
                )
            else:
                yield event.plain_result(
                    f"有 {skipped} 张图片未能解析出可公开访问的链接，且没有其他可用图片或 URL。\n"
                    "如需使用这些图片，请直接粘贴其公开 URL。"
                )
                return

        payload, err = self._build_payload()
        if err:
            yield event.plain_result(f"参数错误：{err}")
            return

        if not images:
            if not prompt:
                yield event.plain_result(
                    "用法：/vgen <提示词>\n"
                    "发消息时附带图片可进行图生视频；"
                    "引用含两张及以上图片的消息可进行关键帧动画。"
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
