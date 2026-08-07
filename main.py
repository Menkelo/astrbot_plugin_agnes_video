import asyncio
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
        return None, "none"

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
                "已尝试解析组件 URL、向协议端请求 get_image、以及 AstrBot 文件服务，均未获得可用链接"
                "（详见 AstrBot 日志中的 AgnesVideo 提示）。\n"
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
