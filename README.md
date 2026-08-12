# astrbot_plugin_agnes_video

基于 [Agnes AI Video V2.0](https://www.agnes-ai.com/zh-Hans/docs/agnes-video-v20) 与 [TokenDance（MiniMax H3）](https://tokendance.space/models/minimax-h3) 的 AstrBot 视频生成插件，支持文生视频、图生视频和关键帧动画。

## 功能特性

- 双提供商：通过 `provider` 配置在 Agnes 与 TokenDance（MiniMax H3）之间切换。
- 文生视频：通过文本提示词直接生成电影级视频。
- 图生视频：消息中附带或引用一张图片，将静态图片转化为动态视频。
- 关键帧动画：引用两张及以上图片，在多个关键帧之间生成流畅过渡。
- 自动分流：单个 `/vgen` 命令根据消息中的图片数量自动选择生成模式。
- 异步任务管理：创建任务后立即返回，后台自动轮询，生成完成自动推送视频。
- 多 Key 轮询：配置多个 API Key 时自动轮换使用，规避单 Key 限流。
- 适配 QQ 两种图片调用方式：消息中直接附带图片，以及引用/回复一条含图片的消息。

## 安装

1. 将插件目录 `astrbot_plugin_agnes_video` 放入 AstrBot 的 `data/plugins/` 目录。
2. 在 AstrBot WebUI 的「插件管理」中启用插件。
3. 进入插件配置：
   - `provider` 选择 `agnes` 或 `tokendance`；
   - `api_keys` 列表填写对应服务的 API Key（Agnes：官网申请；TokenDance：在 https://tokendance.space 控制台生成）。

依赖：`aiohttp`、`Pillow`（requirements.txt 已声明；安装插件时若自动安装失败，请在 AstrBot 环境中执行 `pip install aiohttp Pillow`）。

## 命令

| 命令 | 触发条件 | 生成模式 |
| --- | --- | --- |
| `/vgen <提示词>` | 无图片 | 文生视频 |
| `/vgen <提示词>` | 消息附带一张图片，或引用含一张图片的消息 | 图生视频 |
| `/vgen <提示词>` | 引用含两张及以上图片的消息 | 关键帧动画 |

说明：

- 图片来源优先级：消息中直接附带的图片 > 引用消息中的图片 > 提示词中粘贴的图片 URL。
- 若只发图片、不写提示词，图生视频 / 关键帧动画会使用内置的默认提示词。
- 提示词中粘贴的多个图片 URL 同样会触发图生视频 / 关键帧动画。
- 图片来源可为本机已解析的公开 URL，也可由插件自动转换。插件按以下顺序尝试解析图片：
  1. 消息平台下发的图片链接（`Image.url` / `Image.file`）；
  2. 向协议端请求 `get_image`（NapCat / Lagrange 等 OneBot 协议端）；
  3. AstrBot 文件服务 `callback_api_base`（需配置公网可达地址）；
  4. 将本地图片转换为 Data URI Base64（NapCat 未下发链接时会自动使用）。
  全部失败时才会提示你直接粘贴图片 URL。
- QQ 平台（aiocqhttp）引用图片时，NapCat 通常不随事件下发图片链接，但会给出本地文件路径，插件会自动将其转换为 base64 后提交。

## 配置项

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `provider` | `agnes` | 视频生成服务提供商（agnes / tokendance） |
| `api_keys` | `[]` | API Key 列表（必填），每项一个 Key，多个 Key 自动轮询使用 |
| `base_url` | `https://apihub.agnes-ai.com` | Agnes API 网关地址 |
| `tokendance_base_url` | `https://tokendance.space/gateway/minimax` | TokenDance API 网关地址 |
| `model` | `agnes-video-v2.0` | Agnes 视频生成模型 |
| `tokendance_model` | `minimax-h3` | TokenDance 视频生成模型（MiniMax H3） |
| `resolution` | `768P` | TokenDance 视频分辨率（768P / 2K） |
| `aspect_ratio` | `4:3` | 默认宽高比（下拉选择：16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9） |
| `duration_seconds` | `5` | 默认视频时长（下拉选择：3 / 5 / 10 / 18 秒） |
| `seed` | `-1` | 随机种子，-1 表示随机（仅 Agnes 支持） |
| `poll_interval` | `5` | 轮询间隔（秒） |
| `max_poll_time` | `600` | 后台最大等待时间（秒） |

## 参数说明

- **宽高比**：文生视频默认使用配置的 `aspect_ratio`；图生视频 / 关键帧动画会自动用 Pillow 识别参考图比例并映射到最接近档位。提示词中指定比例（如 `16:9`、`9:16`、`1:1`、`4:3`、`3:4`、`21:9` 或中文「横屏 / 竖屏 / 方形」）可覆盖以上规则。
- **时长**：默认使用配置的 `duration_seconds`；提示词中指定秒数（如 `10s`、`10秒`）可覆盖。
  - Agnes：自动映射到最近档位（3 / 5 / 10 / 18 秒）。
  - TokenDance（MiniMax H3）：支持 4-15 秒整数，超出范围自动钳制；图生视频 / 参考生视频模式下宽高比由图片决定（`adaptive`）。
- **分辨率**：TokenDance 提供商可选 768P / 2K（默认 768P），2K 更清晰但生成更慢。
- **MiniMax H3 图片限制**：图生视频使用首帧（1 张图）；两张图使用首尾帧；三张及以上按参考图（reference_image）处理，单次最多 9 张，超出自动截断。
- 提示词推荐结构：`[主体] + [动作] + [场景] + [镜头运动] + [光线] + [风格]`。

## 使用示例

```
# 文生视频（默认 4:3，约 5 秒）
/vgen 一只猫在沙滩上散步，海浪轻拍，夕阳金色光线，电影感运镜

# 文生视频：指定宽高比与时长
/vgen 城市夜景航拍，霓虹灯光，赛博朋克风格 16:9 10s

# 图生视频：发 /vgen 时在消息里附带一张图片（比例自动按图匹配）
/vgen 人物缓缓转身看向镜头，自然表情

# 图生视频：引用一条含一张图片的消息
/vgen 让图中的人物抬头微笑 3:4 5s

# 关键帧动画：引用一条含两张及以上图片的消息
/vgen 在两个关键帧之间平滑过渡，保持角色一致
```

## 注意事项

- 本插件仅支持 aiocqhttp（OneBot / NapCat / Lagrange）平台。
- 关键帧动画要求协议端能同时提供多张图片的链接；引用图片时若协议端未下发链接，插件会尝试 `get_image` 与文件服务。
- 生成完成直接发送视频消息；发送失败会重试一次，仍失败则提示重试（不再发送下载链接消息）。
- Agnes 限制每个 API Key 每分钟最多创建 1 个视频任务（HTTP 429）。配置多个 Key 时插件会轮询使用并自动在限流时切换；单 Key 时请间隔约 1 分钟再生成。
- TokenDance 网关对请求频率有 RPM 限制（默认用户与 Key 各 500 RPM），MiniMax H3 上游高峰可能返回 429，插件会轮换 Key 重试，仍失败则提示稍后重试。
- 部分消息平台对视频大小与时长有限制，长视频可能发送失败，此时可从链接下载。
