import os
import re
import time
import base64
import hashlib
import zipfile
import asyncio
from pathlib import Path
import aiohttp

from PIL import Image as PILImage
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Reply

# Upscayl 模型名称映射[cite: 4]
UPSCAYL_MODEL_NAME_MAP = {
    "数字艺术 (digital-art-4x)": "digital-art-4x",
    "高保真 (high-fidelity-4x)": "high-fidelity-4x",
    "Remacri (remacri-4x)": "remacri-4x",
    "超混合平衡 (ultramix-balanced-4x)": "ultramix-balanced-4x",
    "超锐化 (ultrasharp-4x)": "ultrasharp-4x",
    "轻量 (upscayl-lite-4x)": "upscayl-lite-4x",
    "标准 (upscayl-standard-4x)": "upscayl-standard-4x",
}

CACHE_TTL_SEC = 7 * 24 * 3600  # 7 天缓存过期时间[cite: 4]
# GitHub Release 托管环境包地址 (可替换为你仓库上传后的直链)
REMOTE_UPSCAYL_ZIP_URL = "https://github.com/Yuanluoo/astrbot_plugin_image_tool/releases/download/v1.0.0/upscayl-win.zip"


@register("astrbot_plugin_image_tool", "Yuanluoo", "独立高清 AI 升图与 FFmpeg AVIF 格式转换工具", "1.0.0")
class ImageToolPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self.config = context.get_config()
        
        # 根路径与缓存目录初始化[cite: 4]
        self.plugin_dir = Path(__file__).parent
        self.cache_dir = Path(os.getcwd()) / "data" / "cache" / "image_tool"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # 自动判定内置路径
        self.embedded_upscayl_dir = self.plugin_dir / "upscayl"
        self.default_bin_path = self.embedded_upscayl_dir / ("upscayl-bin.exe" if os.name == 'nt' else "upscayl-bin")
        self.default_models_dir = self.embedded_upscayl_dir / "models"

        # 全局并发锁 & 状态追踪变量[cite: 4]
        self.process_lock = asyncio.Lock()
        self.current_task_info: dict | None = None  
        self.waiting_queue_count = 0                
        
        # 启动后台任务：清理过期缓存 & 检查部署运行环境
        asyncio.create_task(self._auto_clean_expired_cache())
        asyncio.create_task(self._ensure_upscayl_env())

    async def _ensure_upscayl_env(self):
        """若配置路径为空且本地无二进制环境，自动下载并解压依赖包"""
        custom_bin = self.config.get("upscayl_settings.bin_path")
        if custom_bin and Path(custom_bin).exists():
            return  # 用户手动指定了有效路径，直接跳过

        if self.default_bin_path.exists() and self.default_models_dir.exists():
            return  # 本地已有环境，直接跳过

        logger.info("📦 [ImageTool] 未检测到 Upscayl 环境，正在自动下载运行库...")
        zip_path = self.plugin_dir / "upscayl_temp.zip"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(REMOTE_UPSCAYL_ZIP_URL, timeout=300) as resp:
                    if resp.status == 200:
                        with open(zip_path, "wb") as f:
                            f.write(await resp.read())
                        logger.info("📦 [ImageTool] 环境包下载完成，开始解压...")
                        
                        def _unzip():
                            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                                zip_ref.extractall(self.embedded_upscayl_dir)
                        
                        await asyncio.to_thread(_unzip)
                        zip_path.unlink(missing_ok=True)
                        logger.info("✅ [ImageTool] Upscayl 自动构建完成！")
                    else:
                        logger.warning("⚠️ [ImageTool] 自动下载环境失败 (HTTP %d)，请手动配置 bin_path", resp.status)
        except Exception as e:
            if zip_path.exists(): zip_path.unlink(missing_ok=True)
            logger.warning("⚠️ [ImageTool] 自动下载或解压失败: %s，请手动在设置中配置路径", str(e))

    async def _auto_clean_expired_cache(self):
        """每 12 小时检查并清理大于 7 天的旧缓存"""[cite: 4]
        while True:
            try:
                now = time.time()
                cleaned = 0
                for f in self.cache_dir.glob("*"):
                    if f.is_file() and (now - f.stat().st_mtime > CACHE_TTL_SEC):
                        f.unlink(missing_ok=True)
                        cleaned += 1
                if cleaned > 0:
                    logger.info("🧹 [ImageTool] 自动清理 %d 个超过 7 天的过期缓存文件", cleaned)
            except Exception as e:
                logger.warning("⚠️ [ImageTool] 清理缓存发生异常: %s", str(e))
            await asyncio.sleep(12 * 3600)

    # region 实时百分比与耗时心跳解析器[cite: 4]
    async def _monitor_process_percentage(self, proc: asyncio.subprocess.Process, stage_prefix: str, task_label: str = "1"):
        start_time = time.time()
        percent_pattern = re.compile(r"(\d+(?:\.\d+)?)\s*%")

        stream = proc.stderr or proc.stdout
        if not stream:
            await proc.wait()
            return

        last_logged_pct = -999.0
        last_heartbeat_sec = 0
        has_percentage = False
        buffer = ""

        while True:
            try:
                chunk_bytes = await asyncio.wait_for(stream.read(256), timeout=1.0)
                if not chunk_bytes:
                    break
                buffer += chunk_bytes.decode('utf-8', errors='ignore')

                while '\r' in buffer or '\n' in buffer:
                    pos_r = buffer.find('\r')
                    pos_n = buffer.find('\n')
                    if pos_r != -1 and (pos_n == -1 or pos_r < pos_n):
                        text = buffer[:pos_r]
                        buffer = buffer[pos_r + 1:]
                    else:
                        text = buffer[:pos_n]
                        buffer = buffer[pos_n + 1:]

                    if not text.strip():
                        continue

                    pct_val = None
                    match_pct = percent_pattern.search(text)
                    if match_pct:
                        try:
                            pct_val = float(match_pct.group(1))
                        except ValueError:
                            pass

                    if pct_val is not None:
                        has_percentage = True
                        elapsed_sec = int(time.time() - start_time)
                        if self.current_task_info is not None:
                            self.current_task_info["stage"] = stage_prefix
                            self.current_task_info["percent"] = f"{pct_val:.1f}% ({elapsed_sec}s)"

                            if abs(pct_val - last_logged_pct) >= 10.0 or pct_val == 100.0 or last_logged_pct < 0:
                                logger.info("%s [%s] %.1f%% (已耗时 %ds)", stage_prefix, task_label, pct_val, elapsed_sec)
                                last_logged_pct = pct_val

            except asyncio.TimeoutError:
                if not has_percentage:
                    elapsed_sec = int(time.time() - start_time)
                    if elapsed_sec >= last_heartbeat_sec + 2 and proc.returncode is None:
                        last_heartbeat_sec = elapsed_sec
                        if self.current_task_info is not None:
                            self.current_task_info["stage"] = stage_prefix
                            self.current_task_info["percent"] = f"已处理 {elapsed_sec}s"
                            logger.info("%s [%s] 已耗时 %ds", stage_prefix, task_label, elapsed_sec)

        await proc.wait()
        if proc.returncode != 0:
            logger.warning("⚠️ %s 执行异常 (returncode=%s)", stage_prefix, proc.returncode)
    # endregion

    # region 图片提取与传输工具[cite: 4]
    async def _extract_image_url(self, event: AstrMessageEvent) -> str | None:
        for comp in event.message_obj.message:
            if isinstance(comp, Reply):
                try:
                    bot = getattr(event, "bot", None)
                    if bot and hasattr(bot, "api") and hasattr(bot.api, "call_action"):
                        res = await bot.api.call_action('get_msg', message_id=comp.id)
                        msg_data = res.get('message', [])
                        if isinstance(msg_data, list):
                            for m in msg_data:
                                if m.get('type') == 'image' and 'url' in m.get('data', {}):
                                    return m['data']['url']
                        elif isinstance(msg_data, str):
                            matches = re.findall(r'\[CQ:image,.*?url=([^,\]]+)', msg_data)
                            if matches: return matches[0]
                except Exception as e:
                    logger.warning("⚠️ [ImageTool] 提取引用消息图片失败: %s", str(e))

        for comp in event.message_obj.message:
            if isinstance(comp, Image) and getattr(comp, "url", None):
                return comp.url

        return None

    async def _download_image(self, url: str) -> tuple[bytes, str]:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as resp:
                resp.raise_for_status()
                buffer = await resp.read()
                md5 = hashlib.md5(buffer).hexdigest()
                return buffer, md5

    async def _send_file_via_onebot_api(self, event: AstrMessageEvent, file_path: Path) -> bool:
        try:
            bot = getattr(event, "bot", None)
            if not bot or not hasattr(bot, "api") or not hasattr(bot.api, "call_action"):
                return False

            def _read_b64():
                with open(file_path, "rb") as f:
                    return base64.b64encode(f.read()).decode("utf-8")

            b64_str = await asyncio.to_thread(_read_b64)
            b64_uri = f"base64://{b64_str}"
            file_name = file_path.name

            group_id = getattr(event.message_obj, 'group_id', None)
            if group_id:
                await bot.api.call_action('upload_group_file', group_id=int(group_id), file=b64_uri, name=file_name)
            else:
                user_id = event.get_sender_id()
                await bot.api.call_action('upload_private_file', user_id=int(user_id), file=b64_uri, name=file_name)
            return True
        except Exception as e:
            logger.error("❌ [ImageTool] API 文件直传失败: %s", str(e))
            return False

    @staticmethod
    def _check_image_dimension(image_path: Path, max_width: int, max_height: int) -> tuple[bool, int, int]:
        try:
            with PILImage.open(image_path) as img:
                w, h = img.width, img.height
                exceeds = (w > max_width) or (h > max_height)
                return exceeds, w, h
        except Exception:
            return False, 0, 0
    # endregion

    # region 升图与 FFmpeg 处理核心[cite: 4]
    async def _upscayl_process(self, input_path: Path, img_md5: str) -> Path:
        out_path = self.cache_dir / f"{img_md5}_upscayl.png"
        
        if out_path.exists() and (time.time() - out_path.stat().st_mtime < CACHE_TTL_SEC):
            logger.info("⚡ [Cache Hit] 命中 AI 升图缓存: %s", out_path.name)
            if self.current_task_info:
                self.current_task_info["stage"] = "⚡ 命中 AI 升图缓存"
                self.current_task_info["percent"] = "100.0%"
            return out_path

        # 动态判定使用配置路径还是自动查找/内置路径
        custom_bin = self.config.get("upscayl_settings.bin_path", "")
        custom_models = self.config.get("upscayl_settings.models_path", "")

        upscayl_bin = str(Path(custom_bin) if custom_bin else self.default_bin_path)
        models_dir = str(Path(custom_models) if custom_models else self.default_models_dir)

        scale = str(self.config.get("upscayl_settings.scale", 2))
        enable_taa = bool(self.config.get("upscayl_settings.enable_taa", True))
        double_pass = bool(self.config.get("upscayl_settings.double_pass", False))
        model_setting = str(self.config.get("upscayl_settings.model_name", "数字艺术 (digital-art-4x)"))
        model_name = UPSCAYL_MODEL_NAME_MAP.get(model_setting, model_setting)

        pass1_path = self.cache_dir / f"{img_md5}_up1.png"

        def _build_cmd(inp: Path, outp: Path):
            cmd = [upscayl_bin, "-i", str(inp.resolve()), "-o", str(outp.resolve()), "-n", model_name, "-s", scale]
            if enable_taa: cmd.append("-x")
            if models_dir and Path(models_dir).exists(): cmd.extend(["-m", models_dir])
            return cmd

        if self.current_task_info:
            self.current_task_info["stage"] = f"🎨 AI 升图进行中 (Pass 1/{'2' if double_pass else '1'})"
            self.current_task_info["percent"] = "0.0%"
            
        cmd1 = _build_cmd(input_path, pass1_path if double_pass else out_path)
        logger.info("🎨 [Upscayl] 执行 1 次升图 (%sx/%s): %s", scale, model_name, input_path.name)
        proc1 = await asyncio.create_subprocess_exec(*cmd1, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        
        await self._monitor_process_percentage(proc1, "🎨 AI 升图中", "Pass 1")
        await proc1.wait()

        if not double_pass:
            if self.current_task_info: self.current_task_info["percent"] = "100.0%"
            return out_path if out_path.exists() else input_path

        if self.current_task_info:
            self.current_task_info["stage"] = "🎨 AI 升图进行中 (Pass 2/2 双重升图)"
            self.current_task_info["percent"] = "0.0%"

        cmd2 = _build_cmd(pass1_path, out_path)
        logger.info("🎨 [Upscayl] 执行 2 次升图 (双重): %s", pass1_path.name)
        proc2 = await asyncio.create_subprocess_exec(*cmd2, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        
        await self._monitor_process_percentage(proc2, "🎨 AI 升图中", "Pass 2")
        await proc2.wait()
        
        pass1_path.unlink(missing_ok=True)
        if self.current_task_info: self.current_task_info["percent"] = "100.0%"

        return out_path if out_path.exists() else input_path

    async def _ffmpeg_avif_process(self, input_path: Path, img_md5: str) -> Path:
        out_path = self.cache_dir / f"{img_md5}_libaom.avif"
        
        if out_path.exists() and (time.time() - out_path.stat().st_mtime < CACHE_TTL_SEC):
            logger.info("⚡ [Cache Hit] 命中 AVIF 压缩缓存: %s", out_path.name)
            if self.current_task_info:
                self.current_task_info["stage"] = "⚡ 命中 AVIF 编码缓存"
                self.current_task_info["percent"] = "100.0%"
            return out_path

        if self.current_task_info:
            self.current_task_info["stage"] = "🗜️ FFmpeg AVIF 编码转码中"
            self.current_task_info["percent"] = "0.0%"

        ffmpeg_bin = str(self.config.get("ffmpeg_settings.ffmpeg_bin_path", "ffmpeg"))

        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-i", str(input_path.resolve()),
            "-map", "0:v:0?",
            "-c:v:0", "libaom-av1",
            "-cpu-used:v:0", "1",
            "-crf:v:0", "18",
            "-still-picture", "1",
            "-row-mt", "1",
            str(out_path.resolve())
        ]

        logger.info("🗜️ [FFmpeg] 开始转码 AVIF (libaom-av1 CRF 18): %s", input_path.name)
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        
        await self._monitor_process_percentage(proc, "🗜️ FFmpeg AVIF 压缩中", "AVIF")
        await proc.wait()

        if self.current_task_info: self.current_task_info["percent"] = "100.0%"
        return out_path if out_path.exists() else input_path
    # endregion

    # region 指令入口[cite: 4]
    @filter.command("升图进度")
    @filter.command("avif进度")
    async def cmd_query_status(self, event: AstrMessageEvent):
        if not self.process_lock.locked() and not self.current_task_info:
            yield event.plain_result("🟢 当前显卡与处理器空闲，没有正在执行的升图或转码任务。")
            return

        task = self.current_task_info or {}
        sender = task.get("user", "未知用户")
        cmd_type = task.get("cmd", "/升图")
        stage = task.get("stage", "正在处理")
        percent = task.get("percent", "0.0%")
        start_t = task.get("start_time", time.time())
        elapsed = int(time.time() - start_t)
        waiting = max(0, self.waiting_queue_count)

        status_msg = (
            f"⚙️ 运行状态清单：\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 发起用户：{sender}\n"
            f"📌 指令类型：{cmd_type}\n"
            f"🔄 执行阶段：{stage}\n"
            f"📊 阶段进度：{percent}\n"
            f"⏱️ 已用时间：{elapsed} 秒\n"
            f"⏳ 队尾等待：{waiting} 个任务"
        )
        yield event.plain_result(status_msg)

    @filter.command("升图")
    async def cmd_upscale(self, event: AstrMessageEvent):
        url = await self._extract_image_url(event)
        if not url:
            yield event.plain_result("⚠️ 请引用回复一张图片或在发送图片时附带 /升图 指令。")
            return

        buffer, img_md5 = await self._download_image(url)
        raw_path = self.cache_dir / f"{img_md5}_raw.png"
        if not raw_path.exists():
            raw_path.write_bytes(buffer)

        max_w = int(self.config.get("upscayl_settings.max_image_width", 2160))
        max_h = int(self.config.get("upscayl_settings.max_image_height", 3840))
        is_too_large, w, h = await asyncio.to_thread(self._check_image_dimension, raw_path, max_w, max_h)
        
        if is_too_large:
            logger.warning("⛔ 图片尺寸过大 (%dx%d)，超出上限限制 (%dx%d)，拒绝升图", w, h, max_w, max_h)
            yield event.plain_result(f"⚠️ 图片尺寸过大 ({w}x{h})，超过了设定的最大尺寸限制 (宽≤{max_w}px, 高≤{max_h}px)，拒绝升图。")
            return

        yield event.plain_result("⏳ 已加入处理队列，正在排队进行 AI 升图与 AVIF 转码...（可发送 /升图进度 查看百分比状态）")

        self.waiting_queue_count += 1
        try:
            async with self.process_lock:
                self.waiting_queue_count = max(0, self.waiting_queue_count - 1)
                sender_name = str(event.get_sender_name() or event.get_sender_id())
                self.current_task_info = {
                    "user": sender_name,
                    "cmd": "/升图",
                    "stage": "📥 准备与校验处理资源",
                    "percent": "0.0%",
                    "start_time": time.time()
                }

                start_t = time.perf_counter()

                upscaled_path = await self._upscayl_process(raw_path, img_md5)
                avif_path = await self._ffmpeg_avif_process(upscaled_path, img_md5)

                if self.current_task_info:
                    self.current_task_info["stage"] = "📤 正在上传 Base64 文件"
                    self.current_task_info["percent"] = "100.0%"

                elapsed = time.perf_counter() - start_t
                logger.info("✅ [/升图] 处理完成，耗时: %.2fs", elapsed)

                ok = await self._send_file_via_onebot_api(event, avif_path)
                if not ok:
                    yield event.plain_result("❌ 发送文件失败，请检查底层协议适配器。")
        except Exception as e:
            logger.error("❌ [/升图] 执行异常: %s", str(e))
            yield event.plain_result(f"❌ 处理失败: {str(e)}")
        finally:
            self.current_task_info = None

    @filter.command("avif")
    async def cmd_to_avif(self, event: AstrMessageEvent):
        url = await self._extract_image_url(event)
        if not url:
            yield event.plain_result("⚠️ 请引用回复一张图片或在发送图片时附带 /avif 指令。")
            return

        yield event.plain_result("⏳ 已加入处理队列，正在进行 AVIF 高清转码...（可发送 /avif进度 查看百分比状态）")

        self.waiting_queue_count += 1
        try:
            async with self.process_lock:
                self.waiting_queue_count = max(0, self.waiting_queue_count - 1)
                sender_name = str(event.get_sender_name() or event.get_sender_id())
                self.current_task_info = {
                    "user": sender_name,
                    "cmd": "/avif",
                    "stage": "📥 下载并保存原图",
                    "percent": "0.0%",
                    "start_time": time.time()
                }

                start_t = time.perf_counter()
                buffer, img_md5 = await self._download_image(url)

                raw_path = self.cache_dir / f"{img_md5}_raw.png"
                if not raw_path.exists():
                    raw_path.write_bytes(buffer)

                avif_path = await self._ffmpeg_avif_process(raw_path, img_md5)

                if self.current_task_info:
                    self.current_task_info["stage"] = "📤 正在上传 Base64 文件"
                    self.current_task_info["percent"] = "100.0%"

                elapsed = time.perf_counter() - start_t
                logger.info("✅ [/avif] 转码完成，耗时: %.2fs", elapsed)

                ok = await self._send_file_via_onebot_api(event, avif_path)
                if not ok:
                    yield event.plain_result("❌ 发送文件失败，请检查底层协议适配器。")
        except Exception as e:
            logger.error("❌ [/avif] 执行异常: %s", str(e))
            yield event.plain_result(f"❌ 处理失败: {str(e)}")
        finally:
            self.current_task_info = None