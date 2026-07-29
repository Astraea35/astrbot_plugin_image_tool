# 🖼️ AI 升图与 AVIF 转换工具

[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-blue?style=flat-square)](https://github.com/Soulter/AstrBot)
[![License: MIT](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)

一个为 AstrBot 设计的高清图像处理插件。支持引用图片一键调用 **Upscayl** 进行 AI 高清放大，并使用 **FFmpeg (libaom-av1)** 转码为极致画质与高压缩比的 `.avif` 文件发送。

---

## ✨ 核心特性

- 🎨 **AI 放大重构 (`/升图`)**：响应 `/升图` 指令，接入 Upscayl 引擎，支持多种模型、自定义放大倍数及 TAA 抗锯齿。
- 📦 **零配置自动构建**：配置路径留空时，首次运行将自动检测并从 GitHub Release 静默下载 Upscayl 依赖环境，开箱即用。
- 📊 **实时进度查询 (`/升图进度`)**：支持随时查询显卡/处理器的实时运行状态、当前任务百分比、已耗时秒数及排队队列。
- 🚫 **长宽单独拦截**：独立设置长/宽像素限制，超限图片自动拒绝升图，有效规避显存与内存溢出风险。
- 🗜️ **AVIF 极致压缩 (`/avif`)**：基于 FFmpeg `libaom-av1` (CRF 18) 预设编码，在保留高画质细节的同时大幅压缩体积。
- 🔒 **显存/运存安全防爆**：内置全局单任务互斥锁（`asyncio.Lock`），多用户并发请求自动安全排队。
- ⚡ **智能 7 天三级缓存**：基于图片 MD5 哈希校验，二次处理相同图片瞬间秒发；后台自动定期清理过期文件。
- 📦 **OneBot 内存直传**：采用 Base64 内存数据直连 OneBot 文件上传 API，完美绕过 Windows 系统下的本地盘符路径解析 Bug。

---

## 🚀 使用方法

在 QQ 群或私聊中**引用回复**带有图片的消息，或者在**发送图片的同时**附带文本指令：

| 指令 | 说明 | 输出格式 |
| --- | --- | --- |
| `/升图` | 调用 Upscayl AI 模型放大图片，随后转码为 AVIF 发送 | `.avif` 文件 |
| `/avif` | 跳过 AI 升图，直接使用 FFmpeg 高品质转码为 AVIF | `.avif` 文件 |
| `/升图进度` / `/avif进度` | 查询当前升图/转码任务的执行阶段、实时百分比、耗时与队列 | 文本状态清单 |

---

## ⚙️ 插件配置项

在 AstrBot 管理面板 -> 插件配置 -> **AI 升图与 AVIF 转换工具** 中可调整以下参数：

### 阿普升图设置 (Upscayl)

| 配置项 | 类型 | 默认值 | 说明与参考 |
| --- | --- | --- | --- |
| `max_image_width` | 整数 | `2160` | 升图最大宽度限制(px)。参考：`1080P(1920)` / `2K(2560)` / `4K(3840)` / `6K(5760)` / `8K(7680)` |
| `max_image_height` | 整数 | `3840` | 升图最大高度限制(px)。参考：`1080P(1080)` / `2K(1440)` / `4K(2160)` / `6K(3240)` / `8K(4320)` |
| `model_name` | 下拉单选 | `数字艺术 (digital-art-4x)` | 选择 AI 升图调用的模型 |
| `scale` | 整数 | `2` | 单次升图放大倍数 (支持 1 - 4 倍) |
| `double_pass` | 开关 | `false` | 是否执行双重升图（开启获得最高精度，关闭速度翻倍） |
| `enable_taa` | 开关 | `true` | 是否启用 TAA 抗锯齿模式以减少图像伪影 |
| `bin_path` | 字符串 | `留空` | 留空则自动检测/下载依赖环境；也可填入本地绝对路径（如 `C:/.../upscayl-bin.exe`） |
| `models_path` | 字符串 | `留空` | 留空则自动检测/下载依赖模型；也可填入本地绝对路径（如 `C:/.../models`） |

### FFmpeg 设置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ffmpeg_bin_path` | 字符串 | `ffmpeg` | FFmpeg 可执行文件路径（加入环境变量 PATH 保持默认即可） |

---

## 📁 目录结构

```text
astrbot_plugin_image_tool/
├── README.md            # 说明文档
├── metadata.yaml        # 插件元信息
├── _conf_schema.json    # 插件配置面板定义
├── main.py              # 核心逻辑
└── upscayl/             # 运行环境目录（首次运行自动下载构建）
🙏 致谢 (Credits)
AI 升图核心引擎来源于开源项目 Upscayl (AGPL-3.0 License)。

外部工具依赖：

FFmpeg：需安装系统环境变量中，或在配置项 ffmpeg_bin_path 中指定绝对路径。

Upscayl：若使用 AI 升图功能，请在系统安装 Upscayl 官方客户端。默认会自动寻找 C:/Program Files/Upscayl/... 路径，非默认路径可在插件设置中自定义。