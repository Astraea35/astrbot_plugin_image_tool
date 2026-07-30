import os
import re
import time
import base64
import hashlib
import asyncio
from pathlib import Path
import aiohttp
import cv2
import numpy as np

from PIL import Image as PILImage
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Reply

# Upscayl 模型名称映射
UPSCAYL_MODEL_NAME_MAP = {
    "数字艺术 (digital-art-4x)": "digital-art-4x",
    "高保真 (high-fidelity-4x)": "high-fidelity-4x",
    "Remacri (remacri-4x)": "remacri-4x",
    "超混合平衡 (ultramix-balanced-4x)": "ultramix-balanced-4x",
    "超锐化 (ultrasharp-4x)": "ultrasharp-4x",
    "轻量 (upscayl-lite-4x)": "upscayl-lite-4x",
    "标准 (upscayl-standard-4x)": "upscayl-standard-4x",
}

CACHE_TTL_SEC = 7 * 24 * 3600  # 7 天缓存过期时间


@register("AI 升图与 AVIF 转换工具", "Yuanluoo", "独立高清 AI 升图与 FFmpeg AVIF 格式转换工具", "1.0.6")
class ImageToolPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config if isinstance(config, dict) else {}
        
        # 缓存目录初始化
        self.cache_dir = Path(os.getcwd()) / "data" / "cache" / "image_tool"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # 全局并发锁 & 状态追踪变量（确保同一时间只运行 1 个任务）
        self.process_lock = asyncio.Lock()
        self.current_task_info: dict | None = None  # 记录当前正在运行的任务信息
        self.waiting_queue_count = 0                # 记录在队列中等待的任务数量
        
        # 启动后台 7 天缓存定期清理任务
        asyncio.create_task(self._auto_clean_expired_cache())

    def _get_cfg(self, section: str, key: str, default=None):
        """安全获取两层嵌套配置项（修改为动态实时拉取，修改保存即刻生效）"""
        current_config = self.config
        sec = current_config.get(section, {}) if isinstance(current_config, dict) else {}
        if isinstance(sec, dict):
            return sec.get(key, default)
        return default

    def _get_upscayl_paths(self) -> tuple[str, str]:
        """获取 Upscayl 可执行文件与模型目录路径：
        优先读取插件自带的 resources 目录，未找到则回退至用户自定义配置项。
        """
        plugin_dir = Path(__file__).parent.resolve()
        
        # 1. 可执行文件路径判定
        local_bin_exe = plugin_dir / "resources" / "bin" / "upscayl-bin.exe"
        local_bin = plugin_dir / "resources" / "bin" / "upscayl-bin"
        
        if local_bin_exe.is_file():
            resolved_bin = str(local_bin_exe)
        elif local_bin.is_file():
            resolved_bin = str(local_bin)
        else:
            resolved_bin = str(self._get_cfg("upscayl_settings", "bin_path", "C:/Program Files/Upscayl/resources/bin/upscayl-bin.exe"))

        # 2. 模型文件夹路径判定
        local_models = plugin_dir / "resources" / "models"
        if local_models.is_dir() and any(local_models.iterdir()):
            resolved_models = str(local_models)
        else:
            resolved_models = str(self._get_cfg("upscayl_settings", "models_path", "C:/Program Files/Upscayl/resources/models"))

        return resolved_bin, resolved_models

    async def _auto_clean_expired_cache(self):
        """每 12 小时检查并清理大于 7 天的旧缓存"""
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

    # region 实时百分比与耗时心跳解析器
    async def _monitor_process_percentage(self, proc: asyncio.subprocess.Process, stage_prefix: str, task_label: str = "1"):
        """实时捕获子进程 (Upscayl / FFmpeg) 进度"""
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

    # region 图片提取与传输工具
    async def _extract_image_url(self, event: AstrMessageEvent) -> str | None:
        """优先从引用回复中提取图片 URL，若无则从当前消息提取"""
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
        """下载图片并计算 MD5 返回 (buffer, md5)"""
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as resp:
                resp.raise_for_status()
                buffer = await resp.read()
                md5 = hashlib.md5(buffer).hexdigest()
                return buffer, md5

    async def _send_file_via_onebot_api(self, event: AstrMessageEvent, file_path: Path) -> bool:
        """使用 OneBot upload_group_file/upload_private_file 接口直传 Base64"""
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
        """检查图片长宽是否超过限制，返回 (是否超限, 宽, 高)"""
        try:
            with PILImage.open(image_path) as img:
                w, h = img.width, img.height
                exceeds = (w > max_width) or (h > max_height)
                return exceeds, w, h
        except Exception:
            return False, 0, 0

    @staticmethod
    def _predict_is_anime(image_path: Path) -> bool:
        """提取 CV 物理特征 (饱和度、平坦度、边缘比) 判断是否为二次元/插画"""
        try:
            # 安全读取，避免 Windows 系统下中文路径报错
            data = np.fromfile(str(image_path.resolve()), dtype=np.uint8)
            img_bgr = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img_bgr is None:
                return True  # 读取失败兜底默认二次元

            hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
            saturation_mean = float(np.mean(hsv[:, :, 1]))

            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 50, 150)
            edge_ratio = float(np.count_nonzero(edges) / edges.size)
            non_edge_mask = (edges == 0)
            flatness_std = float(np.std(gray[non_edge_mask])) if np.any(non_edge_mask) else 50.0

            # 动漫插画显著特征：较高饱和度 OR 明显线稿边缘 OR 平坦填色
            is_anime = (flatness_std < 65.0) or (edge_ratio > 0.02) or (saturation_mean > 65.0)
            return is_anime
        except Exception as e:
            logger.warning("⚠️ CV 检测异常，默认回退至二次元处理: %s", str(e))
            return True
    # endregion

    # region 升图与 FFmpeg 处理核心
    async def _upscayl_process(self, input_path: Path, img_md5: str) -> Path:
        """调用 Upscayl AI 升图（支持百分比实时捕获）"""
        out_path = self.cache_dir / f"{img_md5}_upscayl.png"
        
        # 缓存检查 (7 天)
        if out_path.exists() and (time.time() - out_path.stat().st_mtime < CACHE_TTL_SEC):
            logger.info("⚡ [Cache Hit] 命中 AI 升图缓存: %s", out_path.name)
            if self.current_task_info:
                self.current_task_info["stage"] = "⚡ 命中 AI 升图缓存"
                self.current_task_info["percent"] = "100.0%"
            return out_path

        # 动态判定并优先读取插件目录 resources/
        upscayl_bin, models_dir = self._get_upscayl_paths()
        scale = str(self._get_cfg("upscayl_settings", "scale", 2))
        enable_taa = bool(self._get_cfg("upscayl_settings", "enable_taa", True))
        double_pass = bool(self._get_cfg("upscayl_settings", "double_pass", False))
        
        model_setting = str(self._get_cfg("upscayl_settings", "model_name", "智能判定 (Auto)"))
        
        # 智能判定逻辑
        if model_setting == "智能判定 (Auto)":
            is_anime = await asyncio.to_thread(self._predict_is_anime, input_path)
            model_name = "digital-art-4x" if is_anime else "ultrasharp-4x"
            label = "二次元/插画" if is_anime else "真实照片"
            logger.info("🧠 [CV智能判定] 该图片特征判定为 %s，自动挂载模型: %s", label, model_name)
            
            if self.current_task_info:
                self.current_task_info["stage"] = f"🧠 判定为{label}, 准备升图"
        else:
            model_name = UPSCAYL_MODEL_NAME_MAP.get(model_setting, model_setting)

        pass1_path = self.cache_dir / f"{img_md5}_up1.png"

        def _build_cmd(inp: Path, outp: Path):
            cmd = [upscayl_bin, "-i", str(inp.resolve()), "-o", str(outp.resolve()), "-n", model_name, "-s", scale]
            if enable_taa: cmd.append("-x")
            if models_dir and Path(models_dir).exists(): cmd.extend(["-m", models_dir])
            return cmd

        # 第一次升图
        if self.current_task_info:
            self.current_task_info["stage"] = f"🎨 AI 升图进行中 (Pass 1/{'2' if double_pass else '1'})"
            self.current_task_info["percent"] = "0.0%"
            
        cmd1 = _build_cmd(input_path, pass1_path if double_pass else out_path)
        logger.info("🎨 [Upscayl] 执行 1 次升图 (%sx/%s) [Bin: %s]: %s", scale, model_name, upscayl_bin, input_path.name)
        proc1 = await asyncio.create_subprocess_exec(*cmd1, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        
        await self._monitor_process_percentage(proc1, "🎨 AI 升图中", "Pass 1")
        await proc1.wait()

        if not double_pass:
            if self.current_task_info: self.current_task_info["percent"] = "100.0%"
            return out_path if out_path.exists() else input_path

        # 双重升图
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
        """使用 FFmpeg libaom-av1 压缩为 AVIF"""
        out_path = self.cache_dir / f"{img_md5}_libaom.avif"
        
        # 缓存检查 (7 天)
        if out_path.exists() and (time.time() - out_path.stat().st_mtime < CACHE_TTL_SEC):
            logger.info("⚡ [Cache Hit] 命中 AVIF 压缩缓存: %s", out_path.name)
            if self.current_task_info:
                self.current_task_info["stage"] = "⚡ 命中 AVIF 编码缓存"
                self.current_task_info["percent"] = "100.0%"
            return out_path

        if self.current_task_info:
            self.current_task_info["stage"] = "🗜️ FFmpeg AVIF 编码转码中"
            self.current_task_info["percent"] = "0.0%"

        ffmpeg_bin = str(self._get_cfg("ffmpeg_settings", "ffmpeg_bin_path", "ffmpeg"))

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

    # region 指令入口
    @filter.command("升图进度")
    @filter.command("avif进度")
    async def cmd_query_status(self, event: AstrMessageEvent):
        """查询当前 AI 升图 / AVIF 转码任务进度"""
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
        """回复图片并输入 /升图：直接升图，除非宽度/高度超过限制"""
        url = await self._extract_image_url(event)
        if not url:
            yield event.plain_result("⚠️ 请引用回复一张图片或在发送图片时附带 /升图 指令。")
            return

        buffer, img_md5 = await self._download_image(url)
        raw_path = self.cache_dir / f"{img_md5}_raw.png"
        if not raw_path.exists():
            raw_path.write_bytes(buffer)

        # 尺寸限制检查
        max_w = int(self._get_cfg("upscayl_settings", "max_image_width", 2160))
        max_h = int(self._get_cfg("upscayl_settings", "max_image_height", 3840))
        is_too_large, w, h = await asyncio.to_thread(self._check_image_dimension, raw_path, max_w, max_h)
        
        if is_too_large:
            logger.warning("⛔ 图片尺寸过大 (%dx%d)，超出上限限制 (%dx%d)，拒绝升图", w, h, max_w, max_h)
            yield event.plain_result(f"⚠️ 图片尺寸过大 ({w}x{h})，超过了设定的最大尺寸限制 (宽≤{max_w}px, 高≤{max_h}px)，拒绝升图。")
            return

        yield event.plain_result("⏳ 已加入处理队列，正在排队进行 AI 升图与 AVIF 转码...（可发送 /升图进度 查看百分比状态）")

        # 🔒 增加等待计数并加锁排队
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

                # 1. 运行 Upscayl 升图
                upscaled_path = await self._upscayl_process(raw_path, img_md5)
                # 2. 运行 FFmpeg libaom-av1 压图
                avif_path = await self._ffmpeg_avif_process(upscaled_path, img_md5)

                if self.current_task_info:
                    self.current_task_info["stage"] = "📤 正在上传 Base64 文件"
                    self.current_task_info["percent"] = "100.0%"

                elapsed = time.perf_counter() - start_t
                logger.info("✅ [/升图] 处理完成，耗时: %.2fs", elapsed)

                # 3. 通过 API 直传 AVIF 文件
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
        """回复图片并输入 /avif：直接使用 FFmpeg 转码为 AVIF 文件发送"""
        url = await self._extract_image_url(event)
        if not url:
            yield event.plain_result("⚠️ 请引用回复一张图片或在发送图片时附带 /avif 指令。")
            return

        yield event.plain_result("⏳ 已加入处理队列，正在进行 AVIF 高清转码...（可发送 /avif进度 查看百分比状态）")

        # 🔒 增加等待计数并加锁排队
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

                # 直接调用 FFmpeg 转码
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