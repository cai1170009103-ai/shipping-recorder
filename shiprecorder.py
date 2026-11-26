# -*- coding: utf-8 -*-
"""
凯博助手 v1.3 —— 免费自用版
功能：摄像头取景 + 扫码单号触发录制 + 时间戳/单号水印烧进视频 + 按单号命名保存 + 今日计数
用法：双击 run.bat 启动；把摄像头架在打包台正上方，扫码枪扫快递单号后开始录制，
     打包完成后点“停止”或直接扫下一单（自动保存上一段并开始新一段）。
依赖：Python 3.10+ / opencv-python / numpy / pillow（install.bat 会自动装）
系统：Windows 10 / 11
"""
import os
import sys
import json
import time
import queue
import threading
import datetime
import re
from collections import deque

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

APP_NAME = "凯博助手"
VERSION = "v1.3"
if getattr(sys, "frozen", False):
    # PyInstaller onefile 打包后：__file__ 指向每次启动都重建的临时解压目录，
    # 配置写在那里，关掉软件就没了，下次启动又得重选摄像头。
    # 打包版改放 exe 所在目录，设置才能记住。
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
COUNT_PATH = os.path.join(BASE_DIR, "count.json")

DEFAULT_CONFIG = {
    "camera_index": 0,
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "mjpg": True,   # MJPG 模式：走压缩格式，省 USB 带宽；很多 4K 摄像头默认 YUY2
                    # 不压缩，1080p 就占满 USB 2.0 带宽，出帧只剩几帧/秒
    "save_dir": os.path.join(BASE_DIR, "videos"),
    "terminal_id": "10001",
    "mode": "打包发货",          # 打包发货 / 进出库管理 / 退件拆包
    "mirror": False,
    "guide_box": True,
    "auto_crop_black": True,  # 自动裁黑边：某些摄像头驱动会把画面只放在帧的一半，另一半留黑
    "motion_box": False,
    "auto_stop_seconds": 0,   # 0 = 不自动停；>0 时无动作 N 秒后自动停止并保存
    "font_size": 30,
    # 下面是按 MFEC 设置页对齐的功能
    "pre_roll_seconds": 5,    # 前置预录制：扫码前 N 秒画面也存进视频
    "zoom_after_scan": {"enabled": True, "delay": 2, "duration": 3, "factor": 2.0},
    "voice_prompt": True,     # 扫码后放大时语音播报（Windows 自带语音，无需联网）
    "voice_text": "凯博好帅",   # 语音播报的文字（设置里可改）
    "max_record_seconds": 200,  # 单段最长录制秒数，到时自动保存
    "segment_seconds": 300,   # 每 N 秒分一段（0 = 不分段）
    "save_days": 40,          # 视频保留天数，到期自动删除（0 = 不删）
    "barcode_filter": "",     # 条码过滤正则，空 = 不过滤
    "global_scan": False,     # 扫码枪全局监听（需 keyboard 库；窗口没焦点时也能扫码触发）
    "photo_only": False,      # 仅拍照模式：扫码只拍一张照片，不录视频
    "codec": "auto",          # 视频编码：auto（优先 H.264，打不开回落 mp4v）/ h264 / mp4v
    "disk_min_free_mb": 500,  # 磁盘保护：录制中剩余空间低于此值（MB）自动停录保存，0 = 关
    "reconnect_timeout": 30,  # 断线重连：录制中断线超过此值（秒）自动停录保存，0 = 不停
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _play_mp3_windows(path):
    """Windows 下用系统 winmm 播放 mp3（只用标准库，无需任何第三方库）。"""
    try:
        import ctypes
        winmm = ctypes.windll.winmm
        winmm.mciSendStringW("close shiprec_mp3", None, 0, None)
        cmd_open = 'open "%s" type mpegvideo alias shiprec_mp3' % path.replace('"', '')
        if winmm.mciSendStringW(cmd_open, None, 0, None) != 0:
            return False
        try:
            winmm.mciSendStringW("play shiprec_mp3 wait", None, 0, None)
        finally:
            winmm.mciSendStringW("close shiprec_mp3", None, 0, None)
        return True
    except Exception:
        return False


def _voice_cache_path(text):
    """播报语音缓存文件：按文字算 md5，存在 exe 旁边，启动时提前合成好，
    扫码时直接放缓存，不用每次都等联网合成。"""
    import hashlib
    key = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    return os.path.join(BASE_DIR, "voice_%s.mp3" % key)


def _edge_synthesize(text, out_path):
    """Edge 在线合成 mp3 存到 out_path。成功返回 True。"""
    try:
        import asyncio
        import edge_tts

        async def _gen(path):
            communicate = edge_tts.Communicate(
                text, "zh-CN-XiaoxiaoNeural", rate="+10%")
            await asyncio.wait_for(communicate.save(path), timeout=10)

        asyncio.run(_gen(out_path))
        return os.path.isfile(out_path) and os.path.getsize(out_path) > 1024
    except Exception:
        return False


def _speak_edge(text):
    """Edge 在线甜美女声（晓晓）。返回 True 表示播报成功。
    先放缓存（启动时已提前合成好），没有缓存才现合成并存下来再放。"""
    cache = _voice_cache_path(text)
    try:
        if os.path.isfile(cache) and os.path.getsize(cache) > 1024:
            if _play_mp3_windows(cache):
                return True
    except Exception:
        pass
    try:
        # 合成到缓存同目录的临时文件再改名：和缓存同一磁盘，改名一定成功
        # （用系统临时目录的话，跨盘改名会失败，缓存就存不下来）
        tmp = cache + ".tmp"
        try:
            if _edge_synthesize(text, tmp):
                try:
                    os.replace(tmp, cache)
                except Exception:
                    pass
                play_path = cache if os.path.isfile(cache) else tmp
                return _play_mp3_windows(play_path)
        finally:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass
    except Exception:
        pass
    return False


def presynth_voice(text):
    """后台提前把播报语音合成好存进缓存：扫码时直接放缓存，秒播报，
    不用等联网合成。启动时调一次，改了播报文字后也调一次。"""
    def _run():
        try:
            cache = _voice_cache_path(text)
            if os.path.isfile(cache) and os.path.getsize(cache) > 1024:
                return
            tmp = cache + ".tmp"
            if _edge_synthesize(text, tmp):
                try:
                    os.replace(tmp, cache)
                except Exception:
                    pass
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def _speak_sapi(text):
    """Windows 自带语音兜底：优先挑晓晓等年轻女声。"""
    try:
        import pyttsx3
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        engine = pyttsx3.init()

        def _score(v):
            name = getattr(v, "name", "") or ""
            low = name.lower()
            if "xiaoxiao" in low or "晓晓" in name:
                return 0
            if "xiaoyi" in low or "晓伊" in name:
                return 1
            if "xiaochen" in low or "晓辰" in name:
                return 2
            if "huihui" in low or "xiaohan" in low or "晓涵" in name:
                return 3
            langs = " ".join(getattr(v, "languages", []) or [])
            if "zh" in langs.lower() or "chinese" in low or "中文" in name:
                return 4
            return 9

        try:
            voices = engine.getProperty("voices") or []
            if voices:
                best = min(voices, key=_score)
                if _score(best) < 9:
                    engine.setProperty("voice", best.id)
        except Exception:
            pass
        try:
            engine.setProperty("rate", 210)
        except Exception:
            pass
        engine.say(text)
        engine.runAndWait()
        try:
            engine.stop()
        except Exception:
            pass
    except Exception:
        pass


def speak(text):
    """语音播报：优先 Edge 甜美女声，失败回落 Windows 自带语音。
    后台线程发声，绝不阻塞界面和录制；全部失败就静默跳过。
    _speak_edge 先放启动时预合成好的缓存（秒出声），没有缓存才现合成。"""
    def _run():
        if _speak_edge(text):
            return
        _speak_sapi(text)
    threading.Thread(target=_run, daemon=True).start()


def load_count():
    today = datetime.date.today().isoformat()
    try:
        with open(COUNT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") == today:
            return int(data.get("count", 0))
    except Exception:
        pass
    return 0


def save_count(n):
    with open(COUNT_PATH, "w", encoding="utf-8") as f:
        json.dump({"date": datetime.date.today().isoformat(), "count": n},
                  f, ensure_ascii=False)


def clean_number(s):
    """单号只保留字母数字下划线和 -，其余字符去掉，避免文件名非法。"""
    return re.sub(r"[^\w\-]", "", (s or "").strip())


# ---------------- 文本水印（PIL，可显示中文） ----------------
_FONT_CACHE = {}
_LABEL_CACHE = {}


def get_font(size):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    candidates = []
    if os.name == "nt":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        candidates += [os.path.join(windir, "Fonts", "msyh.ttc"),
                       os.path.join(windir, "Fonts", "simhei.ttf"),
                       os.path.join(windir, "Fonts", "simsun.ttc")]
    candidates += ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    font = None
    for p in candidates:
        try:
            if os.path.exists(p):
                font = ImageFont.truetype(p, size)
                break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def render_label(text, size=30, rgb=(255, 0, 0), stroke=2):
    """把一行文字预渲染成带黑边的 RGBA 小图（缓存起来，每帧直接贴，省 CPU）。"""
    key = (text, size, rgb, stroke)
    if key in _LABEL_CACHE:
        return _LABEL_CACHE[key]
    font = get_font(size)
    # 先量尺寸
    tmp = Image.new("RGBA", (8, 8))
    d = ImageDraw.Draw(tmp)
    bbox = d.textbbox((0, 0), text, font=font, stroke_width=stroke)
    w = bbox[2] - bbox[0] + 4
    h = bbox[3] - bbox[1] + 4
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((2 - bbox[0], 2 - bbox[1]), text, font=font, fill=rgb + (255,),
           stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    if len(_LABEL_CACHE) > 64:
        _LABEL_CACHE.clear()
    _LABEL_CACHE[key] = img
    return img


def draw_text_cn(frame_bgr, text, xy, size=30, color=(0, 0, 255), stroke=2):
    """在 BGR 帧上贴一行中文文本（红字黑边）。color 为 BGR。"""
    rgb = (color[2], color[1], color[0])
    label = render_label(text, size=size, rgb=rgb, stroke=stroke)
    img = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)).convert("RGBA")
    img.alpha_composite(label, xy)
    return cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)


_LABEL_NP_CACHE = {}


def _label_np(text, size, rgb, stroke=2):
    """render_label 的 numpy 版：预渲染一次，缓存 RGB + alpha（float32），
    每帧贴文字时只混合标签包围盒那一小块，不用整帧转格式。"""
    key = (text, size, rgb, stroke)
    if key in _LABEL_NP_CACHE:
        return _LABEL_NP_CACHE[key]
    label = render_label(text, size=size, rgb=rgb, stroke=stroke)
    arr = np.array(label)  # RGBA
    rgb_arr = arr[:, :, :3].astype(np.float32)
    alpha = arr[:, :, 3:4].astype(np.float32) / 255.0
    if len(_LABEL_NP_CACHE) > 64:
        _LABEL_NP_CACHE.clear()
    _LABEL_NP_CACHE[key] = (rgb_arr, alpha)
    return _LABEL_NP_CACHE[key]


def paste_label(frame_bgr, text, xy, size=30, color=(0, 0, 255), stroke=2):
    """在 BGR 帧指定位置贴一行中文（红字黑边），只混合标签包围盒区域，
    不做整帧 PIL 转换。原地修改并返回同一帧。color 为 BGR。"""
    rgb = (color[2], color[1], color[0])
    rgb_arr, alpha = _label_np(text, size, rgb, stroke)
    lh, lw = alpha.shape[:2]
    x, y = xy
    fh, fw = frame_bgr.shape[:2]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(fw, x + lw), min(fh, y + lh)
    if x2 <= x1 or y2 <= y1:
        return frame_bgr
    lx1, ly1 = x1 - x, y1 - y
    lx2, ly2 = lx1 + (x2 - x1), ly1 + (y2 - y1)
    # 标签是 RGB，帧是 BGR：通道翻过来再按 alpha 混合
    region = frame_bgr[y1:y2, x1:x2].astype(np.float32)
    lab = rgb_arr[ly1:ly2, lx1:lx2][:, :, ::-1]
    a = alpha[ly1:ly2, lx1:lx2]
    frame_bgr[y1:y2, x1:x2] = (region * (1 - a) + lab * a).astype(np.uint8)
    return frame_bgr


def draw_dashed_rect(frame, pt1, pt2, color=(0, 0, 255), thickness=2, dash=18):
    """画虚线矩形（取景框）。"""
    x1, y1 = pt1
    x2, y2 = pt2
    for x in range(x1, x2, dash * 2):
        cv2.line(frame, (x, y1), (min(x + dash, x2), y1), color, thickness)
        cv2.line(frame, (x, y2), (min(x + dash, x2), y2), color, thickness)
    for y in range(y1, y2, dash * 2):
        cv2.line(frame, (x1, y), (x1, min(y + dash, y2)), color, thickness)
        cv2.line(frame, (x2, y), (x2, min(y + dash, y2)), color, thickness)


def list_cameras(max_index=5):
    """探测 0..max_index 哪个摄像头能打开，返回 [(index, name)]。"""
    found = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY)
        if cap.isOpened():
            found.append((i, "摄像头 %d" % i))
            cap.release()
    return found


def detect_black_borders(frame, thresh=12):
    """检测画面四周整条纯黑边：某些摄像头驱动会把实际画面只放在帧的一半，
    另一半留黑（画面看起来只有右边一小块）。返回 (x1, y1, x2, y2) 裁切区；
    没有黑边返回 None。只认接近纯黑（平均亮度 < thresh）的整条边，
    打包台上正常偏暗的画面不会误裁。"""
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    except Exception:
        return None
    h, w = gray.shape
    col = gray.mean(axis=0)  # 每列平均亮度
    row = gray.mean(axis=1)  # 每行平均亮度
    xs = np.where(col > thresh)[0]
    ys = np.where(row > thresh)[0]
    if len(xs) == 0 or len(ys) == 0:
        return None
    x1, x2 = int(xs[0]), int(xs[-1]) + 1
    y1, y2 = int(ys[0]), int(ys[-1]) + 1
    # 黑边太小不裁；裁完剩下的画面不能太小（防误判）
    if x1 < w * 0.03 and x2 > w * 0.97 and y1 < h * 0.03 and y2 > h * 0.97:
        return None
    if (x2 - x1) < w * 0.4 or (y2 - y1) < h * 0.4:
        return None
    return (x1, y1, x2, y2)


# ---------------- 录制核心（采集线程） ----------------
class Recorder:
    def __init__(self, cfg, on_frame, on_status):
        self.cfg = cfg
        self.on_frame = on_frame      # 回调：把最新一帧（已烧水印）交给界面
        self.on_status = on_status    # 回调：状态文字
        self.cap = None
        self.writer = None
        self.running = False
        self.recording = False
        self.tracking_no = ""
        self.record_start = None
        self.last_motion = None
        self.frame_size = (cfg["width"], cfg["height"])
        self._native_size = self.frame_size  # 摄像头实际出帧尺寸（以读到的帧为准）
        self._crop_rect = None              # 黑边裁切区 (x1, y1, x2, y2)，无黑边为 None
        self.fps = cfg["fps"]
        self._stop_event = threading.Event()
        self._thread = None
        self._bg = None               # 运动检测背景模型
        self._lock = threading.Lock()
        self._writer_lock = threading.RLock()  # 写视频只能一个线程碰
        self.test_video = None        # 测试用：用视频文件代替摄像头
        self.latest_frame = None
        self._preroll = None          # 前置预录制环形缓冲
        self._zoom_from = None
        self._zoom_until = None
        self._segment_start = None
        self._part_index = 1
        self._current_path = ""
        self._prev_path = ""
        self._seg_written = 0
        self._seg_start = None
        self._rewrite_threads = []  # 后台时长校准线程，关软件时要等它们写完
        self.loop_fps = 0.0
        self._fps_count = 0
        self._fps_time = time.time()
        self._codec = None            # 本次胜出的视频编码（avc1/mp4v），分段/校准沿用
        self._cam_fail = 0            # 连续读不到帧的次数（断线重连用）
        self._cam_down_since = None   # 摄像头断线起始时间
        self._run_id = 0              # 采集线程代号：防旧线程在重开摄像头后继续跑
        self._disk_check_t = 0

    def open_camera(self):
        # 先放掉旧的摄像头句柄（重连/重开时不留泄漏，否则新开可能失败）
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        if self.test_video:
            self.cap = cv2.VideoCapture(self.test_video)
        else:
            flag = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
            # MJPG 优先：很多 4K 摄像头默认走不压缩的 YUY2，1080p 就要占满
            # USB 2.0 带宽，出帧只剩几帧/秒；MJPG 只要约 1/20 带宽就能跑满 30 帧。
            # MJPG 不支持的摄像头探不到帧，自动重开一次（不设 MJPG）。
            attempts = [True, False] if self.cfg.get("mjpg", True) else [False]
            self.cap = None
            for use_mjpg in attempts:
                cap = cv2.VideoCapture(self.cfg["camera_index"], flag)
                try:
                    if use_mjpg:
                        cap.set(cv2.CAP_PROP_FOURCC,
                                cv2.VideoWriter_fourcc(*"MJPG"))
                    if int(self.cfg.get("width") or 0) > 0:
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg["width"])
                    if int(self.cfg.get("height") or 0) > 0:
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg["height"])
                    if int(self.cfg.get("fps") or 0) > 0:
                        cap.set(cv2.CAP_PROP_FPS, self.cfg["fps"])
                except Exception:
                    pass
                if not cap.isOpened():
                    cap.release()
                    continue
                ok = False
                for _ in range(3):
                    ok, _pf = cap.read()
                    if ok:
                        break
                if ok:
                    self.cap = cap
                    break
                cap.release()
            if self.cap is None:
                return False
        if not self.cap.isOpened():
            return False
        w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or self.cfg["width"]
        h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or self.cfg["height"]
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if fps and fps > 0 else self.cfg["fps"]
        # 实际出帧尺寸以读到的帧为准：有些摄像头驱动上报的尺寸和实际帧不符。
        # 顺便用前几帧测黑边（画面只占帧的一半、另一半留黑的驱动毛病），
        # 测到就裁掉，预览和录像都只保留有画面的部分。
        self._native_size = (w, h)
        self._crop_rect = None
        try:
            probes = []
            for _ in range(3):
                ok, pf = self.cap.read()
                if ok and pf is not None:
                    probes.append(pf)
            if probes:
                self._native_size = (probes[0].shape[1], probes[0].shape[0])
                if self.cfg.get("auto_crop_black", True):
                    avg = np.mean(
                        [cv2.cvtColor(p, cv2.COLOR_BGR2GRAY).astype(np.float32)
                         for p in probes], axis=0).astype(np.uint8)
                    bgr_avg = cv2.cvtColor(avg, cv2.COLOR_GRAY2BGR)
                    self._crop_rect = detect_black_borders(bgr_avg)
        except Exception:
            pass
        if self._crop_rect:
            x1, y1, x2, y2 = self._crop_rect
            self.frame_size = (x2 - x1, y2 - y1)
        else:
            self.frame_size = self._native_size
        # 前置预录制缓冲：按帧率算出要缓存的帧数
        pre = float(self.cfg.get("pre_roll_seconds") or 0)
        self._preroll = deque(maxlen=int(self.fps * pre)) if pre > 0 else None
        if self._stop_event.is_set():
            # 关软件/关摄像头时正好在重连：刚打开的立刻放掉，避免句柄泄漏
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
            return False
        return True

    def start(self):
        if self.running:
            return True
        # 先清停止标志，再开摄像头：open_camera 末尾要靠它判断是不是在关软件
        self._stop_event.clear()
        if not self.open_camera():
            self.on_status("摄像头打不开：请检查摄像头是否已接好，或在设置里换一个摄像头。")
            return False
        self._run_id += 1
        rid = self._run_id
        self.running = True
        self._thread = threading.Thread(target=self._loop, args=(rid,),
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self.stop_recording(save=True)
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self.cap:
            self.cap.release()
            self.cap = None
        self.running = False

    def _make_writer(self, path, fps, size):
        """建 VideoWriter：优先 avc1（H.264，文件约小一半），打不开再回落 mp4v。
        记住胜出的编码，分段/校准时沿用同一个，不混编码。"""
        codec = (self.cfg.get("codec") or "auto").lower()
        if codec == "h264":
            order = ["avc1"]
        elif codec == "mp4v":
            order = ["mp4v"]
        elif getattr(self, "_codec", None):
            # 自动模式且已有胜出编码：先用它，失败再试另一个
            order = [self._codec,
                     "mp4v" if self._codec == "avc1" else "avc1"]
        else:
            order = ["avc1", "mp4v"]
        writer = None
        for fourcc_s in order:
            fourcc = cv2.VideoWriter_fourcc(*fourcc_s)
            w = cv2.VideoWriter(path, fourcc, fps, size)
            if w.isOpened():
                self._codec = fourcc_s
                return w
            w.release()
            writer = w
        # 都打不开：返回最后一个（未打开），调用方按 isOpened() 判断
        self._codec = order[-1]
        return writer

    def _disk_free_mb(self):
        """保存盘剩余空间（MB），拿不到返回 None。"""
        try:
            d = self.cfg.get("save_dir") or ""
            if d and os.path.isdir(d):
                return shutil.disk_usage(d).free / (1024 ** 2)
        except Exception:
            pass
        return None

    def _free_disk_space(self, min_free_mb):
        """磁盘快满时删最旧的本软件视频腾空间，直到剩余 >= 阈值。
        正在录的当前段和上一段不删。返回删了几个。"""
        try:
            save_dir = self.cfg.get("save_dir") or ""
            if not save_dir or not os.path.isdir(save_dir):
                return 0
            pat = re.compile(r"^[\w\-]+_\d{8}_\d{6}(_part\d+)?\.(mp4|jpg)$")
            keep = {os.path.abspath(getattr(self, "_current_path", "") or ""),
                    os.path.abspath(getattr(self, "_prev_path", "") or "")}
            names = [n for n in os.listdir(save_dir) if pat.match(n)]
            names.sort(key=lambda n: os.path.getmtime(
                os.path.join(save_dir, n)))
            n = 0
            for name in names:
                if (self._disk_free_mb() or 0) >= min_free_mb:
                    break
                p = os.path.join(save_dir, name)
                if os.path.abspath(p) in keep:
                    continue
                try:
                    os.remove(p)
                    n += 1
                except Exception:
                    pass
            return n
        except Exception:
            return 0

    def start_recording(self, tracking_no):
        tracking_no = clean_number(tracking_no)
        if not tracking_no:
            return False, "请先输入或扫描单号。"
        if self.recording:
            self.stop_recording(save=True)
        # 磁盘满保护：空间不够先删最旧视频腾空间，还不够就拒绝开始录制
        min_free = int(self.cfg.get("disk_min_free_mb") or 0)
        if min_free > 0:
            free = self._disk_free_mb()
            if free is not None and free < min_free:
                self._free_disk_space(min_free)
                free = self._disk_free_mb()
            if free is not None and free < min_free:
                return False, ("磁盘空间不足（剩余 %.0fMB）：已自动删旧视频也腾不出空间，"
                               "请先清理磁盘或把保存文件夹改到空间大的盘。" % free)
        os.makedirs(self.cfg["save_dir"], exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = "%s_%s.mp4" % (tracking_no, ts)
        path = os.path.join(self.cfg["save_dir"], fname)
        writer = self._make_writer(path, self.fps, self.frame_size)
        if not writer.isOpened():
            return False, "写视频文件失败：请检查保存文件夹是否可写。"
        # 状态和预录制写入都在同一把锁里做，避免采集线程同时写
        with self._writer_lock:
            self.writer = writer
            self.tracking_no = tracking_no
            self.record_start = time.time()
            # 放大窗口以扫码时刻为准（不要等预录制写完才起算，否则高分辨率下会晚好几秒）
            z = self.cfg.get("zoom_after_scan") or {}
            if z.get("enabled"):
                self._zoom_from = self.record_start + float(z.get("delay", 2))
                self._zoom_until = self._zoom_from + float(z.get("duration", 3))
            else:
                self._zoom_from = None
                self._zoom_until = None
            self._segment_start = self.record_start
            self._part_index = 1
            self.last_motion = time.time()
            self.recording = True
            self._current_path = path
            self._prev_path = ""
            self._seg_written = 0
            # 预录制画面先写进去（扫码前几秒也有了）；记下最早一帧的时间，
            # 停录/分段时用它算真实跨度，校准视频帧率
            preroll_items = list(self._preroll) if self._preroll else []
            self._seg_start = preroll_items[0][1] if preroll_items \
                else self.record_start
            for jpg_bytes, _t in preroll_items:
                try:
                    arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
                    f = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if f is not None:
                        self.writer.write(f)
                        self._seg_written += 1
                except Exception:
                    pass
        return True, "开始录制：%s" % fname

    def stop_recording(self, save=True):
        with self._writer_lock:
            if not self.recording:
                return None
            path = getattr(self, "_current_path", "")
            seg_written = getattr(self, "_seg_written", 0)
            if self.writer:
                self.writer.release()
                self.writer = None
            self.recording = False
            self.tracking_no = ""
            self.record_start = None
            self._zoom_from = None
            self._zoom_until = None
            self._segment_start = None
            written = seg_written
            seg_start = getattr(self, "_seg_start", None)
            prev_path = getattr(self, "_prev_path", "")
        # 刚分段就停：最后一段只有几帧，没证据价值，删掉，记上一段
        if path and written < 10 and prev_path:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            return prev_path
        if path and written < 10 and not prev_path:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            return None
        self._maybe_calibrate(path, written, seg_start)
        return path

    def _maybe_calibrate(self, path, written, seg_start):
        """帧率校准：如果实际写盘速度跟不上声明帧率，视频会快放；
        按真实帧数/真实跨度重写一遍帧率，时长就和真实时间对上了。"""
        if not path or written <= 10 or not seg_start:
            return
        span = time.time() - seg_start
        if span <= 1:
            return
        eff = written / span
        if 5 <= eff <= 60 and abs(eff - self.fps) / self.fps > 0.05:
            t = threading.Thread(target=self._rewrite_fps,
                                 args=(path, eff), daemon=True)
            self._rewrite_threads.append(t)
            t.start()

    def wait_for_rewrites(self, timeout=20):
        """关软件前等后台的时长校准写完，否则视频会快放、还留一个 .tmp 文件。"""
        deadline = time.time() + timeout
        for t in list(self._rewrite_threads):
            left = deadline - time.time()
            if left <= 0:
                break
            t.join(timeout=left)
        self._rewrite_threads = [t for t in self._rewrite_threads if t.is_alive()]

    def _rewrite_fps(self, path, target_fps):
        """后台把视频按实际帧率重写一遍（原子替换，不影响已记录的路径）。"""
        try:
            self.on_status("正在校准视频时长：%s" % os.path.basename(path))
            cap = cv2.VideoCapture(path)
            if not cap.isOpened():
                return
            tmp = path + ".tmp.mp4"
            w = self._make_writer(tmp, target_fps, self.frame_size)
            if not w.isOpened():
                cap.release()
                return
            while True:
                ok, f = cap.read()
                if not ok:
                    break
                w.write(f)
            w.release()
            cap.release()
            os.replace(tmp, path)
            self.on_status("已保存（时长已校准 %.1f 帧/秒）。"
                           % target_fps)
        except Exception as ex:
            self.on_status("时长校准失败（不影响已保存的视频）：%s" % ex)

    def extend_recording(self):
        """重复扫码同一单号：延长本次录制（不另存新文件）。"""
        if self.recording:
            self.record_start = time.time()
            self._segment_start = time.time()
            self.last_motion = time.time()
            return True
        return False

    def capture_photo(self, tracking_no):
        """拍一张当前画面存为 JPG。"""
        with self._lock:
            frame = None if self.latest_frame is None else self.latest_frame.copy()
        if frame is None:
            return False, "没有画面：摄像头未启动。"
        tracking_no = clean_number(tracking_no)
        os.makedirs(self.cfg["save_dir"], exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = "%s_%s.jpg" % (tracking_no or "拍照", ts)
        path = os.path.join(self.cfg["save_dir"], name)
        cv2.imwrite(path, frame)
        return True, path

    def zoom_factor(self):
        """放大倍数：设置里可调（默认 2 倍）。"""
        try:
            f = float((self.cfg.get("zoom_after_scan") or {}).get("factor", 2.0))
        except Exception:
            f = 2.0
        return min(4.0, max(1.0, f))

    def guide_rect(self):
        """取景框：画面中央的矩形，也是放大时裁切的区域。
        边长 = 画面边长 / 放大倍数，倍数越大框越小、放得越大。"""
        w, h = self.frame_size
        f = self.zoom_factor()
        gw, gh = max(1, int(w / f)), max(1, int(h / f))
        x1, y1 = (w - gw) // 2, (h - gh) // 2
        return (x1, y1, x1 + gw, y1 + gh)

    def _zoom_active(self):
        return (self.recording and self._zoom_from is not None
                and self._zoom_from <= time.time() <= (self._zoom_until or 0))

    def _zoom_frame(self, frame):
        """数字放大：裁取景框区域再放大回全尺寸。"""
        x1, y1, x2, y2 = self.guide_rect()
        crop = frame[y1:y2, x1:x2]
        return cv2.resize(crop, self.frame_size)

    def _rotate_segment(self):
        """到分段时长：关掉当前文件，另开一段继续录（同单号 _part2）。
        调用时需已持有 _writer_lock（_loop 里已持有；RLock 可重入）。"""
        if not self.recording or not self.writer:
            return
        with self._writer_lock:
            if not self.recording or not self.writer:
                return
            old_path = self._current_path
            old_written = getattr(self, "_seg_written", 0)
            old_start = getattr(self, "_seg_start", None)
            self.writer.release()
            self._prev_path = old_path
            self._part_index += 1
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = "%s_%s_part%d.mp4" % (self.tracking_no, ts, self._part_index)
            path = os.path.join(self.cfg["save_dir"], fname)
            self.writer = self._make_writer(path, self.fps, self.frame_size)
            self._segment_start = time.time()
            self._seg_start = self._segment_start
            self._seg_written = 0
            self._current_path = path
        self._maybe_calibrate(old_path, old_written, old_start)
        self.on_status("分段：已另存一段 %s，继续录制。" % fname)

    def _loop(self, run_id=None):
        while not self._stop_event.is_set() and run_id == self._run_id:
            if self.cap is None:
                # 上次重连失败：等一会儿再试；录制中断线太久就保存已录部分
                if self.recording and self._cam_down_since:
                    timeout = int(self.cfg.get("reconnect_timeout") or 30)
                    if timeout > 0 and \
                            time.time() - self._cam_down_since > timeout:
                        path = self.stop_recording(save=True)
                        if path:
                            self.on_status("AUTO_SAVED::" + path)
                        self.on_status("摄像头断线超过 %d 秒，已停止录制并保存。"
                                       % timeout)
                        self._cam_down_since = None
                time.sleep(1.0)
                if self._stop_event.is_set() or run_id != self._run_id:
                    break
                if self.open_camera():
                    self.on_status("摄像头已自动重连。")
                    self._cam_down_since = None
                    self._cam_fail = 0
                continue
            ok, frame = self.cap.read()
            if not ok:
                if self.test_video:
                    # 测试视频播完，循环重播
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                # 摄像头断线：连续读不到帧就重开摄像头
                if self._cam_fail == 0:
                    self._cam_down_since = time.time()
                    self.on_status("摄像头断开了，正在自动重连…")
                self._cam_fail += 1
                if self._cam_fail >= 10:
                    self._cam_fail = 0
                    try:
                        self.cap.release()
                    except Exception:
                        pass
                    self.cap = None
                    if self.open_camera():
                        self.on_status("摄像头已自动重连。")
                        self._cam_down_since = None
                    # open_camera 失败就留 cap=None，下轮循环顶部再试
                    continue
                # 录制中断线太久：保存已录部分，避免一直空等
                if self.recording and self._cam_down_since:
                    timeout = int(self.cfg.get("reconnect_timeout") or 30)
                    if timeout > 0 and \
                            time.time() - self._cam_down_since > timeout:
                        path = self.stop_recording(save=True)
                        if path:
                            self.on_status("AUTO_SAVED::" + path)
                        self.on_status("摄像头断线超过 %d 秒，已停止录制并保存。"
                                       % timeout)
                        self._cam_down_since = None
                time.sleep(0.5)
                continue
            self._cam_fail = 0
            self._cam_down_since = None
            if frame.shape[1] != self._native_size[0] or frame.shape[0] != self._native_size[1]:
                frame = cv2.resize(frame, self._native_size)
            if self._crop_rect:
                # 驱动黑边：只保留有画面的部分（预览和录像都按裁后的尺寸走）
                x1, y1, x2, y2 = self._crop_rect
                frame = frame[y1:y2, x1:x2]
            if self.cfg.get("mirror"):
                frame = cv2.flip(frame, 1)
            # 扫码后放大：先裁切再烧水印，放大期间时间戳和单号也不会丢
            if self.recording and self._zoom_active():
                frame = self._zoom_frame(frame)
            frame = self._burn_overlay(frame)
            with self._lock:
                self.latest_frame = frame.copy()
            # 预录制环形缓冲（没录制时也在缓存；存 JPG 省内存，4K 也不怕）
            if self._preroll is not None:
                ok_j, buf = cv2.imencode(".jpg", frame,
                                         [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok_j:
                    self._preroll.append((buf.tobytes(), time.time()))
            if self.recording:
                with self._writer_lock:
                    if not self.recording or not self.writer:
                        pass
                    else:
                        self.writer.write(frame)
                        self._seg_written = getattr(self, "_seg_written", 0) + 1
                        now = time.time()
                        # 单段最长录制时长（默认 200 秒）
                        max_sec = int(self.cfg.get("max_record_seconds") or 0)
                        if max_sec > 0 and self.record_start and \
                                now - self.record_start > max_sec:
                            path = self.stop_recording(save=True)
                            if path:
                                self.on_status("AUTO_SAVED::" + path)
                        else:
                            # 分段（默认 300 秒一段）
                            seg_sec = int(self.cfg.get("segment_seconds") or 0)
                            if seg_sec > 0 and self._segment_start and \
                                    now - self._segment_start > seg_sec:
                                self._rotate_segment()
                            # 无动作自动停
                            auto_stop = int(self.cfg.get("auto_stop_seconds") or 0)
                            if auto_stop > 0 and self.last_motion and \
                                    now - self.last_motion > auto_stop:
                                path = self.stop_recording(save=True)
                                if path:
                                    self.on_status("AUTO_SAVED::" + path)
            self.on_frame(frame)
            self._fps_count += 1
            _now = time.time()
            if _now - self._fps_time >= 1.0:
                self.loop_fps = self._fps_count / (_now - self._fps_time)
                self._fps_count = 0
                self._fps_time = _now
        return

    def _burn_overlay(self, frame):
        """把时间戳 / 单号 / 终端号烧进画面（录下来的视频里就有证据）。
        中文标签用预渲染缓存图贴上去，时间戳用 cv2 快画，保证处理速度跟得上帧率。"""
        h, w = frame.shape[:2]
        size = int(self.cfg.get("font_size") or 30)
        small = max(20, size - 6)
        labels = []
        mode = self.cfg.get("mode", "打包发货")
        labels.append((mode, (16, 16), small, (255, 255, 255)))
        tid = "终端 %s" % self.cfg.get("terminal_id", "")
        tw = len(tid) * small
        labels.append((tid, (max(16, w - tw - 16), 16), small, (255, 255, 255)))
        if self.recording and self.tracking_no:
            labels.append(("扫码单号：%s" % self.tracking_no,
                           (16, h - 56), size, (0, 0, 255)))
        # 只混合每行标签的包围盒区域，不做整帧 PIL 转换（省 CPU）
        for text, xy, sz, bgr in labels:
            paste_label(frame, text, xy, size=sz, color=bgr)
        # 时间戳用 cv2 快画（毫秒精确，红字黑底）
        now = datetime.datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (now.microsecond // 1000)
        scale = max(0.7, size / 30.0)
        (tw2, th2), _ = cv2.getTextSize(ts, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        cv2.rectangle(frame, (12, h - 96 - th2 - 6), (12 + tw2 + 12, h - 96 + 6),
                      (0, 0, 0), -1)
        cv2.putText(frame, ts, (18, h - 96), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, (0, 0, 255), 2, cv2.LINE_AA)
        # 取景框（虚线红框）
        if self.cfg.get("guide_box"):
            x1, y1, x2, y2 = self.guide_rect()
            draw_dashed_rect(frame, (x1, y1), (x2, y2))
        # 运动检测框（可选）
        if self.cfg.get("motion_box"):
            self._motion_detect(frame)
        return frame

    def _motion_detect(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        if self._bg is None:
            self._bg = gray.astype(np.float32)
            return
        diff = cv2.absdiff(self._bg.astype(np.uint8), gray)
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=2)
        cnts, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL,
                                    cv2.CHAIN_APPROX_SIMPLE)
        best = None
        best_area = 0
        for c in cnts:
            a = cv2.contourArea(c)
            if a > best_area and a > 800:
                best_area = a
                best = c
        if best is not None:
            x, y, bw, bh = cv2.boundingRect(best)
            cv2.rectangle(frame, (x, y), (x + bw, y + bh), (0, 255, 0), 3)
            self.last_motion = time.time()
        # 背景缓慢更新，避免光线渐变误触发（_bg 保持 float32 原地更新）
        cv2.accumulateWeighted(gray, self._bg, 0.01)

# ---------------- 界面 ----------------
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import ImageTk
import shutil

RECORDS_PATH = os.path.join(BASE_DIR, "records.jsonl")
RESOLUTIONS = ["3840x2160", "1920x1080", "1280x720", "640x480", "摄像头默认"]
MODES = ["打包发货", "进出库管理", "退件拆包"]


def append_record(tracking_no, path, duration):
    try:
        with open(RECORDS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "no": tracking_no,
                "file": os.path.basename(path),
                "path": path,
                "dur": round(duration, 1),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_records(keyword=""):
    rows = []
    try:
        with open(RECORDS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    rows.reverse()
    if keyword:
        rows = [r for r in rows if keyword in r.get("no", "")
                or keyword in r.get("time", "")]
    return rows


def auto_cleanup(save_dir, days):
    """删掉保存天数之外的本软件视频（只删符合命名规则的 mp4）。"""
    if not days or days <= 0:
        return 0
    pat = re.compile(r"^[\w\-]+_\d{8}_\d{6}(_part\d+)?\.(mp4|jpg)$")
    cutoff = time.time() - days * 86400
    n = 0
    try:
        for name in os.listdir(save_dir):
            if not pat.match(name):
                continue
            p = os.path.join(save_dir, name)
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                try:
                    os.remove(p)
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return n


class App:
    def __init__(self, root, cfg, test_video=None, selftest=False):
        self.root = root
        self.cfg = cfg
        self.selftest = selftest
        self.count = load_count()
        self.root.title("%s %s" % (APP_NAME, VERSION))
        # 窗口图标：打包版从 _MEIPASS（随 exe 打包的 kaibo.ico），
        # 源码运行从软件目录找；找不到就用默认图标，不报错。
        try:
            _ip = None
            for _base in (getattr(sys, "_MEIPASS", None), BASE_DIR):
                if _base:
                    _p = os.path.join(_base, "kaibo.ico")
                    if os.path.exists(_p):
                        _ip = _p
                        break
            if _ip:
                self.root.iconbitmap(_ip)
        except Exception:
            pass
        self.root.geometry("1160x840")
        self._frame_queue = queue.Queue(maxsize=2)
        self._status_queue = queue.Queue()
        self._latest_preview = None
        self._cur_no = ""
        self._cur_start = None
        self._last_disk_check = 0
        self._scan_buf = ""
        self._scan_last = 0.0
        self.rec = Recorder(cfg, self._on_frame, self._on_status)
        if test_video:
            self.rec.test_video = test_video
        self._build_ui()
        self._auto_cleanup()
        self._start_global_listener()
        self._start_camera()
        # 启动时后台提前合成播报语音存缓存，扫码时直接放，不用等联网合成
        try:
            presynth_voice(self.cfg.get("voice_text", "凯博好帅") or "凯博好帅")
        except Exception:
            pass
        self._update_preview()
        self._tick_disk()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.selftest:
            self.root.after(1000, self._selftest_step1)

    # ---------- 界面搭建（现代浅色风格） ----------
    # 配色
    C_BG = "#F4F6F9"        # 主背景
    C_HEAD = "#1C2B3A"      # 顶栏深蓝
    C_HEAD_CHIP = "#2A3B4F"  # 顶栏小标签
    C_CARD = "#FFFFFF"      # 卡片白
    C_LINE = "#E3E8EF"      # 分隔线
    C_TEXT = "#1F2937"      # 正文
    C_MUTED = "#6B7280"     # 次要文字
    C_GREEN = "#16A34A"     # 录制绿
    C_GREEN_D = "#15803D"
    C_RED = "#DC2626"       # 停止红
    C_BLUE = "#2563EB"
    F_TITLE = ("Microsoft YaHei", 15, "bold")
    F_BODY = ("Microsoft YaHei", 11)
    F_SMALL = ("Microsoft YaHei", 10)
    F_BIG = ("Microsoft YaHei", 13, "bold")

    @staticmethod
    def _flat_btn(parent, text, bg, fg="white", font=F_BODY, padx=14, pady=6,
                  command=None):
        b = tk.Button(parent, text=text, bg=bg, fg=fg, font=font,
                      relief="flat", bd=0, padx=padx, pady=pady,
                      activebackground=bg, activeforeground=fg,
                      cursor="hand2", command=command)
        return b

    def _build_ui(self):
        C = self
        BG, HEAD = self.C_BG, self.C_HEAD
        self.root.configure(bg=BG)

        # ===== 顶栏（深蓝）=====
        top = self.top_bar = tk.Frame(self.root, bg=HEAD, height=58)
        top.pack(fill=tk.X)
        top.pack_propagate(False)
        # logo 小方块
        tk.Label(top, text="录", bg=self.C_GREEN, fg="white",
                 font=("Microsoft YaHei", 15, "bold"),
                 width=2, height=1).pack(side=tk.LEFT, padx=(14, 10), pady=10)
        tk.Label(top, text="凯博助手", bg=HEAD, fg="white",
                 font=self.F_TITLE).pack(side=tk.LEFT)
        tk.Label(top, text=VERSION, bg=HEAD, fg="#8FA3B8",
                 font=self.F_SMALL).pack(side=tk.LEFT, padx=(6, 0), pady=(6, 0))
        # 终端号小标签
        tk.Label(top, text="终端 %s" % self.cfg.get("terminal_id", ""),
                 bg=self.C_HEAD_CHIP, fg="white", font=self.F_SMALL,
                 padx=10, pady=4).pack(side=tk.LEFT, padx=(16, 0))
        # 今日计数（绿色小标签）
        self.count_label = tk.Label(top, text="今日已录 %d 单" % self.count,
                                    bg=self.C_GREEN, fg="white",
                                    font=("Microsoft YaHei", 10, "bold"),
                                    padx=10, pady=4)
        self.count_label.pack(side=tk.LEFT, padx=8)
        # 顶栏按钮（深色描边风）
        def head_btn(text, cmd):
            return self._flat_btn(top, text, self.C_HEAD_CHIP, font=self.F_SMALL,
                                  padx=12, pady=5, command=cmd)
        self.cam_btn = self._flat_btn(top, "关闭摄像头", self.C_HEAD_CHIP,
                                      font=("Microsoft YaHei", 13, "bold"),
                                      padx=20, pady=9,
                                      command=self._toggle_camera)
        self.cam_btn.pack(side=tk.RIGHT, padx=(0, 12))
        head_btn("设置", self._open_settings).pack(side=tk.RIGHT, padx=4)
        head_btn("扫码记录", self._open_records).pack(side=tk.RIGHT, padx=4)
        head_btn("拍照", self._take_photo).pack(side=tk.RIGHT, padx=4)
        self.fs_btn = head_btn("全屏", self._toggle_fullscreen)
        self.fs_btn.pack(side=tk.RIGHT, padx=4)
        head_btn("打开文件夹", self._open_save_folder).pack(side=tk.RIGHT, padx=4)

        # ===== 模式行（分段选择器）=====
        mode_row = tk.Frame(self.root, bg=BG)
        mode_row.pack(fill=tk.X, padx=14, pady=(12, 0))
        seg = tk.Frame(mode_row, bg="white", highlightbackground=self.C_LINE,
                       highlightthickness=1)
        seg.pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value=self.cfg.get("mode", "打包发货"))
        self._mode_btns = {}
        for m in MODES:
            b = tk.Button(seg, text=m, font=self.F_BODY, relief="flat", bd=0,
                          padx=16, pady=7, cursor="hand2",
                          command=lambda mm=m: self._set_mode(mm))
            b.pack(side=tk.LEFT)
            self._mode_btns[m] = b
        self._restyle_mode_btns()
        self.mirror_var = tk.BooleanVar(value=bool(self.cfg.get("mirror")))
        tk.Checkbutton(mode_row, text="摄像头镜像", variable=self.mirror_var,
                       bg=BG, fg=self.C_MUTED, font=self.F_SMALL,
                       activebackground=BG, command=self._toggle_mirror
                       ).pack(side=tk.LEFT, padx=14)
        tk.Label(mode_row, text="扫码枪扫单号后自动开始录制，扫下一单自动保存上一段",
                 bg=BG, fg=self.C_MUTED, font=self.F_SMALL).pack(side=tk.RIGHT)

        # ===== 当前单号大字条（扫码后一眼看清是哪单）=====
        cur = tk.Frame(self.root, bg=self.C_CARD,
                       highlightbackground=self.C_LINE, highlightthickness=1)
        cur.pack(fill=tk.X, padx=14, pady=(10, 0))
        tk.Label(cur, text="当前单号", bg=self.C_CARD, fg=self.C_MUTED,
                 font=self.F_BODY).pack(side=tk.LEFT, padx=(16, 8), pady=10)
        self.cur_no_label = tk.Label(cur, text="—", bg=self.C_CARD,
                                     fg=self.C_TEXT,
                                     font=("Microsoft YaHei", 22, "bold"))
        self.cur_no_label.pack(side=tk.LEFT, padx=4, pady=6)
        self.cur_time_label = tk.Label(cur, text="未在录制", bg=self.C_CARD,
                                       fg=self.C_MUTED, font=self.F_BIG)
        self.cur_time_label.pack(side=tk.LEFT, padx=14)
        tk.Label(cur, text="扫码后这里显示当前单号和已录时长",
                 bg=self.C_CARD, fg="#9CA3AF",
                 font=self.F_SMALL).pack(side=tk.RIGHT, padx=16)

        # ===== 画面区（黑底卡片）+ 今日已录侧栏 =====
        mid = tk.Frame(self.root, bg=BG)
        mid.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)
        video_wrap = self.video_wrap = tk.Frame(mid, bg="black",
                              highlightbackground=self.C_LINE,
                              highlightthickness=1)
        video_wrap.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.video_label = tk.Label(video_wrap, bg="black")
        self.video_label.pack(fill=tk.BOTH, expand=True)
        # 固定一个小的期望尺寸：让 pack 按剩余空间分配画面区，而不是按
        # 预览图的大小撑高窗口，否则最大化/全屏时底部状态栏会被顶出屏幕。
        self.video_label.config(width=640, height=360)
        self.video_label.bind("<Double-Button-1>",
                              lambda e: self._toggle_fullscreen())
        self.root.bind("<Escape>", lambda e: self._set_fullscreen(False))
        # 今日已录侧栏（双击一行打开视频）
        side = tk.Frame(mid, bg=self.C_CARD,
                        highlightbackground=self.C_LINE, highlightthickness=1,
                        width=260)
        side.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))
        side.pack_propagate(False)
        tk.Label(side, text="今日已录", bg=self.C_CARD, fg=self.C_TEXT,
                 font=("Microsoft YaHei", 12, "bold")).pack(
                     anchor="w", padx=12, pady=(10, 2))
        self.today_count_label = tk.Label(side, text="共 0 单", bg=self.C_CARD,
                                          fg=self.C_MUTED, font=self.F_SMALL)
        self.today_count_label.pack(anchor="w", padx=12)
        self.today_tree = ttk.Treeview(side, columns=("time", "no", "dur"),
                                       show="headings", height=14)
        self.today_tree.heading("time", text="时间")
        self.today_tree.heading("no", text="单号")
        self.today_tree.heading("dur", text="时长")
        self.today_tree.column("time", width=64)
        self.today_tree.column("no", width=118)
        self.today_tree.column("dur", width=44)
        self.today_tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.today_tree.bind("<Double-1>", self._open_today_video)

        # ===== 底部操作卡片 =====
        bottom = tk.Frame(self.root, bg=self.C_CARD,
                          highlightbackground=self.C_LINE, highlightthickness=1)
        bottom.pack(fill=tk.X, padx=14, pady=(0, 12))
        tk.Label(bottom, text="扫码单号", bg=self.C_CARD, fg=self.C_MUTED,
                 font=self.F_BODY).pack(side=tk.LEFT, padx=(16, 6), pady=16)
        self.entry = tk.Entry(bottom, font=("Microsoft YaHei", 16), width=24,
                              relief="flat", highlightthickness=2,
                              highlightbackground=self.C_LINE,
                              highlightcolor=self.C_GREEN, bg="#F8FAFC")
        self.entry.pack(side=tk.LEFT, padx=4, pady=12, ipady=6)
        self.entry.bind("<Return>", lambda e: self.on_scan())
        self.start_btn = self._flat_btn(bottom, "▶  开始录制", self.C_GREEN,
                                        font=self.F_BIG, padx=20, pady=10,
                                        command=self.on_scan)
        self.start_btn.pack(side=tk.LEFT, padx=(10, 6), pady=12)
        self._flat_btn(bottom, "■  停止", self.C_RED, font=self.F_BIG,
                       padx=20, pady=10, command=self.on_stop
                       ).pack(side=tk.LEFT, padx=6, pady=12)
        # 录制状态小药丸（右端）
        self.rec_pill = tk.Frame(bottom, bg="#E5E7EB", padx=12, pady=8)
        self.rec_pill.pack(side=tk.RIGHT, padx=16)
        self.rec_dot = tk.Label(self.rec_pill, text="●", bg="#E5E7EB",
                                fg="#9CA3AF", font=("Microsoft YaHei", 12))
        self.rec_dot.pack(side=tk.LEFT)
        self.rec_label = tk.Label(self.rec_pill, text="待机", bg="#E5E7EB",
                                  fg="#4B5563", font=self.F_BIG)
        self.rec_label.pack(side=tk.LEFT, padx=(6, 0))
        self.entry.focus_set()

        # ===== 底部状态栏 =====
        status_bar = tk.Frame(self.root, bg=BG)
        status_bar.pack(fill=tk.X, padx=14, pady=(0, 8))
        self.status_label = tk.Label(status_bar, text="就绪", anchor="w",
                                     bg=BG, fg=self.C_MUTED, font=self.F_SMALL)
        self.status_label.pack(side=tk.LEFT)
        tk.Label(status_bar, text="免费版 · 视频按单号命名保存",
                 bg=BG, fg="#9CA3AF", font=self.F_SMALL).pack(side=tk.RIGHT)
        # 磁盘 / 帧率放到底部状态栏：顶栏按钮多了之后放不下，
        # 而且帧率文字变长（“（偏低）”）会把最左边的顶栏按钮挤没
        self.fps_label = tk.Label(status_bar, text="", bg=BG,
                                  fg=self.C_MUTED, font=self.F_SMALL)
        self.fps_label.pack(side=tk.RIGHT, padx=10)
        self.disk_label = tk.Label(status_bar, text="", bg=BG,
                                   fg=self.C_MUTED, font=self.F_SMALL)
        self.disk_label.pack(side=tk.RIGHT, padx=2)

    def _set_fullscreen(self, on):
        """全屏开关：铺满整个屏幕，顶栏、扫码区、底部状态栏都还在，
        画面区跟着变大。Esc 或再点“退出全屏”或双击画面都能退出。"""
        try:
            self.root.attributes("-fullscreen", bool(on))
        except Exception:
            pass
        try:
            self.fs_btn.config(text="退出全屏" if on else "全屏")
        except Exception:
            pass

    def _toggle_fullscreen(self, event=None):
        try:
            cur = bool(self.root.attributes("-fullscreen"))
        except Exception:
            cur = False
        self._set_fullscreen(not cur)

    def _set_recording_chrome(self, on):
        """录制状态更醒目：录制时顶栏变红、画面区加粗红框；停录恢复原样。"""
        if on == getattr(self, "_chrome_red", False):
            return
        self._chrome_red = on
        try:
            head_bg = self.C_RED if on else self.C_HEAD
            chip_bg = "#991B1B" if on else self.C_HEAD_CHIP
            self.top_bar.config(bg=head_bg)
            for wgt in self.top_bar.winfo_children():
                try:
                    cur_bg = wgt.cget("bg")
                except Exception:
                    continue
                if on:
                    if cur_bg == self.C_HEAD:
                        wgt.config(bg=head_bg)
                    elif cur_bg == self.C_HEAD_CHIP:
                        wgt.config(bg=chip_bg)
                else:
                    if cur_bg == self.C_RED:
                        wgt.config(bg=self.C_HEAD)
                    elif cur_bg == "#991B1B":
                        wgt.config(bg=self.C_HEAD_CHIP)
            self.video_wrap.config(
                highlightbackground=self.C_RED if on else self.C_LINE,
                highlightthickness=3 if on else 1)
        except Exception:
            pass

    def _restyle_mode_btns(self):
        cur = self.mode_var.get()
        for m, b in self._mode_btns.items():
            if m == cur:
                b.config(bg=self.C_HEAD, fg="white")
            else:
                b.config(bg="white", fg="#374151")

    def _set_mode(self, m):
        self.mode_var.set(m)
        self._restyle_mode_btns()
        self._change_mode()

    # ---------- 回调（采集线程 → 界面） ----------
    def _on_frame(self, frame):
        try:
            self._frame_queue.put_nowait(frame)
        except queue.Full:
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frame_queue.put_nowait(frame)
            except queue.Full:
                pass

    def _on_status(self, msg):
        self._status_queue.put(msg)

    def _set_status(self, msg):
        self.status_label.config(text=msg)

    # ---------- 预览刷新 ----------
    def _placeholder(self):
        w, h = 960, 540
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img = draw_text_cn(img, "摄像头未连接", (w // 2 - 120, h // 2 - 60), size=44,
                           color=(255, 255, 255))
        img = draw_text_cn(img, "请接好摄像头后点“启动摄像头”，或在设置里换一个摄像头",
                           (w // 2 - 330, h // 2 + 10), size=24, color=(200, 200, 200))
        return img

    def _update_preview(self):
        try:
            while True:
                self._latest_preview = self._frame_queue.get_nowait()
        except queue.Empty:
            pass
        # 状态消息
        try:
            while True:
                msg = self._status_queue.get_nowait()
                if msg.startswith("AUTO_SAVED::"):
                    self._on_saved(msg[len("AUTO_SAVED::"):])
                else:
                    self._set_status(msg)
        except queue.Empty:
            pass
        frame = self._latest_preview
        if frame is None:
            frame = self._placeholder()
        else:
            frame = frame.copy()
        # 扫码后放大：预览画面跟着放大显示，并语音播报“放大显示”。
        # 录像帧在 Recorder 里已经放大过，这里只给预览副本压个大字（不进录像）。
        if self.rec.recording and self.rec._zoom_active():
            if not getattr(self, "_zoom_spoken", False):
                self._zoom_spoken = True
                if self.cfg.get("voice_prompt", True):
                    speak(self.cfg.get("voice_text", "凯博好帅") or "凯博好帅")
            fh, fw = frame.shape[:2]
            ts = 72
            tx = max(16, (fw - ts * 4) // 2)
            paste_label(frame, "放大显示", (tx, 24), size=ts,
                        color=(255, 255, 255), stroke=3)
        else:
            self._zoom_spoken = False
        if self.rec.recording and self.rec.record_start:
            el = int(time.time() - self.rec.record_start)
            paste_label(frame, "● 录制中 %02d:%02d" % (el // 60, el % 60),
                        (16, 56), size=30, color=(0, 0, 255))
            # 状态小药丸：红底闪烁圆点 + 录制时长和单号
            blink_on = int(time.time() * 2) % 2 == 0
            self.rec_pill.config(bg="#FEE2E2")
            self.rec_dot.config(bg="#FEE2E2",
                                fg="#DC2626" if blink_on else "#FCA5A5")
            self.rec_label.config(
                text="录制中 %02d:%02d · %s" % (el // 60, el % 60,
                                               self.rec.tracking_no),
                bg="#FEE2E2", fg="#991B1B")
            # 当前单号大字条 + 录制红框红顶栏
            self.cur_no_label.config(text=self.rec.tracking_no)
            self.cur_time_label.config(text="已录 %02d:%02d" % (el // 60, el % 60))
            self._set_recording_chrome(True)
        else:
            self.rec_pill.config(bg="#E5E7EB")
            self.rec_dot.config(bg="#E5E7EB", fg="#9CA3AF")
            self.rec_label.config(text="待机", bg="#E5E7EB", fg="#4B5563")
            self.cur_no_label.config(text="—")
            self.cur_time_label.config(text="未在录制")
            self._set_recording_chrome(False)
        # 缩放到正好铺满画面区（按画面区实际尺寸等比缩放，不撑出窗口）
        h, w = frame.shape[:2]
        lw = self.video_label.winfo_width()
        lh = self.video_label.winfo_height()
        if lw > 50 and lh > 50:
            scale = min(lw / w, lh / h)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            if (nw, nh) != (w, h):
                frame = cv2.resize(frame, (nw, nh))
        elif w > 1040:
            nh = int(h * 1040 / w)
            frame = cv2.resize(frame, (1040, nh))
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        self._tk_img = ImageTk.PhotoImage(img)
        self.video_label.config(image=self._tk_img)
        self.root.after(33, self._update_preview)

    def _tick_disk(self):
        try:
            d = self.cfg.get("save_dir") or ""
            if d and os.path.isdir(d):
                usage = shutil.disk_usage(d)
                free_g = usage.free / (1024 ** 3)
                # 按约 15Mbps 估算：每小时约 6.75GB
                hours = usage.free / (15e6 / 8 * 3600)
                self.disk_label.config(
                    text="磁盘 %.0fG · 可录 %.0f 小时" % (free_g, hours))
        except Exception:
            pass
        # 磁盘满自动保护：录制中剩余空间低于阈值，先删最旧视频腾空间；
        # 还不够就自动停录保存并提醒
        try:
            min_free = int(self.cfg.get("disk_min_free_mb") or 0)
            if min_free > 0 and self.rec.recording:
                d = self.cfg.get("save_dir") or ""
                if d and os.path.isdir(d):
                    free_mb = shutil.disk_usage(d).free / (1024 ** 2)
                    if free_mb < min_free:
                        self.rec._free_disk_space(min_free)
                        free_mb = shutil.disk_usage(d).free / (1024 ** 2)
                    if free_mb < min_free:
                        path = self.rec.stop_recording(save=True)
                        if path:
                            self._on_saved(path)
                        self._set_status(
                            "磁盘空间不足（剩余 %.0fMB），已自动停止录制并保存。"
                            % free_mb)
        except Exception:
            pass
        # 今日已录侧栏每 10 秒刷新一次
        try:
            if time.time() - getattr(self, "_today_refresh_t", 0) >= 10:
                self._today_refresh_t = time.time()
                self._refresh_today()
        except Exception:
            pass
        try:
            fps = self.rec.loop_fps
            if fps > 0:
                low = fps < 20
                self.fps_label.config(
                    text="处理 %.0f 帧/秒%s" % (fps, "（偏低）" if low else ""),
                    fg="#DC2626" if low else self.C_MUTED)
        except Exception:
            pass
        self.root.after(2000, self._tick_disk)

    # ---------- 摄像头 ----------
    def _start_camera(self):
        if self.rec.start():
            self.cam_btn.config(text="关闭摄像头")
            self._set_status("摄像头已启动：%s" % self.cfg.get("mode", "打包发货"))
        else:
            self.cam_btn.config(text="启动摄像头")

    def _toggle_camera(self):
        if self.rec.running:
            self.rec.stop()
            self._latest_preview = None
            self.cam_btn.config(text="启动摄像头")
            self._set_status("摄像头已关闭。")
        else:
            self._start_camera()

    def _toggle_mirror(self):
        self.cfg["mirror"] = bool(self.mirror_var.get())
        save_config(self.cfg)

    def _change_mode(self):
        self.cfg["mode"] = self.mode_var.get()
        save_config(self.cfg)
        self._set_status("已切换到：%s" % self.cfg["mode"])

    # ---------- 扫码 / 录制 ----------
    def on_scan(self):
        no = clean_number(self.entry.get())
        if not no:
            self._set_status("请先扫码或输入单号（扫码枪扫完会自动回车）。")
            return
        pat = (self.cfg.get("barcode_filter") or "").strip()
        if pat:
            try:
                if not re.search(pat, no):
                    self._set_status("单号 %s 不符合条码过滤规则，已忽略。" % no)
                    self.entry.delete(0, tk.END)
                    return
            except re.error:
                pass
        if self.cfg.get("photo_only"):
            ok, msg = self.rec.capture_photo(no)
            if ok:
                self._on_saved(msg, is_photo=True)
                self._set_status("已拍照保存：%s" % os.path.basename(msg))
            else:
                self._set_status(msg)
            self.entry.delete(0, tk.END)
            return
        if self.rec.recording:
            if no == self.rec.tracking_no:
                self.rec.extend_recording()
                self._set_status("重复扫码，已延长本次录制：%s" % no)
                self.entry.delete(0, tk.END)
                self.entry.focus_set()
                return
            path = self.rec.stop_recording(save=True)
            if path:
                self._on_saved(path)
        ok, msg = self.rec.start_recording(no)
        if ok:
            self._cur_no = no
            self._cur_start = time.time()
        self._set_status(msg)
        self.entry.delete(0, tk.END)
        self.entry.focus_set()

    def on_stop(self):
        if not self.rec.recording:
            self._set_status("当前没有在录制。")
            return
        path = self.rec.stop_recording(save=True)
        if path:
            self._on_saved(path)
            self._set_status("已保存：%s" % os.path.basename(path))
        else:
            self._set_status("已停止。")

    def _on_saved(self, path, is_photo=False):
        dur = 0
        if self._cur_start:
            dur = time.time() - self._cur_start
        self.count += 1
        save_count(self.count)
        self.count_label.config(text="今日已录 %d 单" % self.count)
        append_record(self._cur_no or "拍照", path, dur)
        self._cur_no = ""
        self._cur_start = None
        self._refresh_today()

    # ---------- 打开保存文件夹 / 今日已录侧栏 ----------
    def _open_save_folder(self):
        d = self.cfg.get("save_dir") or ""
        if not d:
            return
        os.makedirs(d, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(d)
            else:
                os.system("xdg-open '%s' &" % d)
        except Exception as ex:
            self._set_status("打不开保存文件夹：%s" % ex)

    def _refresh_today(self):
        """刷新主界面右侧“今日已录”侧栏（只显示今天的，最多 12 条）。"""
        try:
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            rows = [r for r in read_records()
                    if r.get("time", "").startswith(today)]
            for i in self.today_tree.get_children():
                self.today_tree.delete(i)
            for r in rows[:12]:
                self.today_tree.insert("", tk.END, values=(
                    r.get("time", "")[11:16], r.get("no", ""),
                    r.get("dur", "")), tags=(r.get("path", ""),))
            self.today_count_label.config(text="共 %d 单" % len(rows))
        except Exception:
            pass

    def _open_today_video(self, _e=None):
        sel = self.today_tree.selection()
        if not sel:
            return
        path = self.today_tree.item(sel[0], "tags")[0]
        if path and os.path.exists(path):
            if os.name == "nt":
                os.startfile(path)
            else:
                os.system("xdg-open '%s' &" % path)

    def _take_photo(self):
        no = clean_number(self.entry.get()) or "拍照"
        ok, msg = self.rec.capture_photo(no)
        if ok:
            self._on_saved(msg, is_photo=True)
            self._set_status("已拍照保存：%s" % os.path.basename(msg))
        else:
            self._set_status(msg)

    # ---------- 自动清理 ----------
    def _auto_cleanup(self):
        try:
            # 先清掉上次异常退出留下的半成品 .tmp.mp4
            save_dir = self.cfg.get("save_dir", "")
            if save_dir and os.path.isdir(save_dir):
                for name in os.listdir(save_dir):
                    if name.endswith(".tmp.mp4"):
                        try:
                            os.remove(os.path.join(save_dir, name))
                        except Exception:
                            pass
            n = auto_cleanup(self.cfg.get("save_dir", ""),
                             int(self.cfg.get("save_days") or 0))
            if n:
                self._set_status("已自动删除 %d 个超过 %d 天的旧视频。" % (
                    n, int(self.cfg.get("save_days") or 0)))
        except Exception:
            pass

    # ---------- 设置窗口（分组卡片，可滚动 + 底部固定保存按钮） ----------
    def _open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.geometry("620x780")
        win.configure(bg=self.C_BG)
        win.transient(self.root)
        win.resizable(False, False)
        cfg = dict(self.cfg)
        zoom = dict(cfg.get("zoom_after_scan") or {})

        style = ttk.Style(win)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TCombobox", font=self.F_BODY, padding=4)

        # 滚动区（内容多时可滚，保存按钮固定在底部）
        main = tk.Frame(win, bg=self.C_BG)
        main.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(main, bg=self.C_BG, highlightthickness=0)
        vsb = ttk.Scrollbar(main, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = tk.Frame(canvas, bg=self.C_BG)
        win_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win_id, width=e.width))
        win.bind("<MouseWheel>",
                 lambda e: canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        scroll_body = body  # section() 里自己的 body 是局部变量，用另一个名字

        def section(title):
            card = tk.Frame(scroll_body, bg=self.C_CARD,
                            highlightbackground=self.C_LINE, highlightthickness=1)
            card.pack(fill=tk.X, padx=16, pady=(12, 0))
            head = tk.Frame(card, bg=self.C_CARD)
            head.pack(fill=tk.X, padx=16, pady=(12, 2))
            tk.Label(head, text="", bg=self.C_GREEN, width=1).pack(
                side=tk.LEFT, fill=tk.Y, padx=(0, 8))
            tk.Label(head, text=title, bg=self.C_CARD, fg=self.C_TEXT,
                     font=("Microsoft YaHei", 12, "bold")).pack(side=tk.LEFT)
            body = tk.Frame(card, bg=self.C_CARD)
            body.pack(fill=tk.X, padx=16, pady=(0, 10))
            return body

        def row(parent, label, hint=""):
            f = tk.Frame(parent, bg=self.C_CARD)
            f.pack(fill=tk.X, pady=5)
            tk.Label(f, text=label, bg=self.C_CARD, fg=self.C_TEXT,
                     font=self.F_BODY, width=13, anchor="w").pack(side=tk.LEFT)
            if hint:
                tk.Label(f, text=hint, bg=self.C_CARD, fg=self.C_MUTED,
                         font=self.F_SMALL).pack(side=tk.RIGHT)
            return f

        def entry(parent, var, width=22):
            e = tk.Entry(parent, textvariable=var, font=self.F_BODY, width=width,
                         relief="flat", highlightthickness=1,
                         highlightbackground=self.C_LINE,
                         highlightcolor=self.C_GREEN, bg="#F8FAFC")
            e.pack(side=tk.LEFT, ipady=3)
            return e

        def spin(parent, var, frm, to, width=6):
            s = tk.Spinbox(parent, from_=frm, to=to, textvariable=var, width=width,
                           font=self.F_BODY, relief="flat",
                           highlightthickness=1, highlightbackground=self.C_LINE,
                           highlightcolor=self.C_GREEN, bg="#F8FAFC",
                           buttonbackground="#F8FAFC")
            s.pack(side=tk.LEFT, ipady=2)
            return s

        def check(parent, text, var):
            return tk.Checkbutton(parent, text=text, variable=var,
                                  bg=self.C_CARD, fg=self.C_TEXT,
                                  font=self.F_BODY, activebackground=self.C_CARD)

        # --- 摄像头 ---
        sec = section("摄像头")
        cams = list_cameras()
        cam_vals = ["%d" % i for i, _ in cams] or ["0"]
        r = row(sec, "摄像头", "没列出就先插好摄像头再打开设置")
        cam_var = tk.StringVar(value=str(cfg.get("camera_index", 0)))
        ttk.Combobox(r, textvariable=cam_var, values=cam_vals, width=8,
                     state="readonly").pack(side=tk.LEFT)
        r = row(sec, "分辨率", "4K 选 3840x2160；卡就降到 1920x1080")
        cur_res = "%sx%s" % (cfg.get("width"), cfg.get("height")) \
            if int(cfg.get("width") or 0) > 0 else "摄像头默认"
        res_var = tk.StringVar(value=cur_res if cur_res in RESOLUTIONS else "1920x1080")
        ttk.Combobox(r, textvariable=res_var, values=RESOLUTIONS, width=13,
                     state="readonly").pack(side=tk.LEFT)
        r = row(sec, "MJPG 模式", "帧数低时开：走压缩格式，省 USB 带宽")
        mjpg_v = tk.BooleanVar(value=bool(cfg.get("mjpg", True)))
        check(r, "启用", mjpg_v).pack(side=tk.LEFT)

        # --- 保存 ---
        sec = section("保存")
        r = row(sec, "保存文件夹")
        dir_var = tk.StringVar(value=cfg.get("save_dir", ""))
        entry(r, dir_var, width=30)
        self._flat_btn(r, "浏览…", "#E5E7EB", fg=self.C_TEXT, font=self.F_SMALL,
                       padx=10, pady=3,
                       command=lambda: dir_var.set(
                           filedialog.askdirectory() or dir_var.get())
                       ).pack(side=tk.LEFT, padx=6)
        r = row(sec, "终端号", "烧进视频右上角")
        tid_var = tk.StringVar(value=cfg.get("terminal_id", ""))
        entry(r, tid_var, width=14)
        r = row(sec, "视频保留", "天，到期自动删，0 = 不删")
        days_var = tk.IntVar(value=int(cfg.get("save_days") or 40))
        spin(r, days_var, 0, 365)
        r = row(sec, "磁盘保护", "MB：录制中剩余低于此值自动停录，0 = 关")
        disk_var = tk.IntVar(value=int(cfg.get("disk_min_free_mb") or 500))
        spin(r, disk_var, 0, 10000)

        # --- 录制 ---
        sec = section("录制")
        r = row(sec, "前置预录制", "秒，扫码前几秒也存进视频")
        pre_var = tk.IntVar(value=int(cfg.get("pre_roll_seconds") or 0))
        spin(r, pre_var, 0, 10)
        r = row(sec, "扫码后放大")
        zoom_var = tk.BooleanVar(value=bool(zoom.get("enabled")))
        check(r, "启用", zoom_var).pack(side=tk.LEFT)
        tk.Label(r, text="扫码后第", bg=self.C_CARD, fg=self.C_TEXT,
                 font=self.F_BODY).pack(side=tk.LEFT, padx=(10, 2))
        zd_var = tk.IntVar(value=int(zoom.get("delay", 2)))
        spin(r, zd_var, 0, 10, width=4)
        tk.Label(r, text="秒，放大", bg=self.C_CARD, fg=self.C_TEXT,
                 font=self.F_BODY).pack(side=tk.LEFT, padx=2)
        zt_var = tk.IntVar(value=int(zoom.get("duration", 3)))
        spin(r, zt_var, 1, 10, width=4)
        tk.Label(r, text="秒，", bg=self.C_CARD, fg=self.C_TEXT,
                 font=self.F_BODY).pack(side=tk.LEFT, padx=2)
        zf_var = tk.StringVar(value=str(zoom.get("factor", 2.0)))
        ttk.Combobox(r, textvariable=zf_var,
                     values=["1.5", "2", "2.5", "3", "4"], width=4,
                     state="readonly").pack(side=tk.LEFT)
        tk.Label(r, text="倍", bg=self.C_CARD, fg=self.C_TEXT,
                 font=self.F_BODY).pack(side=tk.LEFT, padx=2)
        r = row(sec, "语音播报", "放大时读下面的话")
        voice_v = tk.BooleanVar(value=bool(cfg.get("voice_prompt", True)))
        check(r, "启用", voice_v).pack(side=tk.LEFT)
        voice_t = tk.StringVar(value=cfg.get("voice_text", "凯博好帅"))
        entry(r, voice_t, width=12)
        r = row(sec, "单段最长", "秒，到时自动保存")
        max_var = tk.IntVar(value=int(cfg.get("max_record_seconds") or 200))
        spin(r, max_var, 30, 3600)
        r = row(sec, "分段时长", "秒，0 = 不分段")
        seg_var = tk.IntVar(value=int(cfg.get("segment_seconds") or 300))
        spin(r, seg_var, 0, 3600)
        r = row(sec, "无动作自动停", "秒，0 = 关")
        auto_var = tk.IntVar(value=int(cfg.get("auto_stop_seconds") or 0))
        spin(r, auto_var, 0, 600)
        r = row(sec, "条码过滤", "正则，空 = 不过滤")
        filt_var = tk.StringVar(value=cfg.get("barcode_filter", ""))
        entry(r, filt_var, width=22)
        r = row(sec, "视频编码", "H.264 文件约小一半；打不开自动回落")
        codec_var = tk.StringVar(value=cfg.get("codec", "auto"))
        ttk.Combobox(r, textvariable=codec_var,
                     values=["auto", "h264", "mp4v"], width=8,
                     state="readonly").pack(side=tk.LEFT)
        tk.Label(r, text="auto=自动优先H.264", bg=self.C_CARD, fg=self.C_MUTED,
                 font=self.F_SMALL).pack(side=tk.LEFT, padx=6)
        r = row(sec, "断线重连", "秒：录制中断线超过此值自动停录保存，0 = 不停")
        recon_var = tk.IntVar(value=int(cfg.get("reconnect_timeout") or 30))
        spin(r, recon_var, 0, 300)

        # --- 其他 ---
        sec = section("其他")
        r = row(sec, "画面")
        mirror_v = tk.BooleanVar(value=bool(cfg.get("mirror")))
        check(r, "镜像", mirror_v).pack(side=tk.LEFT)
        crop_v = tk.BooleanVar(value=bool(cfg.get("auto_crop_black", True)))
        check(r, "自动裁黑边", crop_v).pack(side=tk.LEFT, padx=10)
        guide_v = tk.BooleanVar(value=bool(cfg.get("guide_box")))
        check(r, "取景框", guide_v).pack(side=tk.LEFT, padx=10)
        motion_v = tk.BooleanVar(value=bool(cfg.get("motion_box")))
        check(r, "运动检测框", motion_v).pack(side=tk.LEFT, padx=10)
        photo_v = tk.BooleanVar(value=bool(cfg.get("photo_only")))
        check(r, "仅拍照模式", photo_v).pack(side=tk.LEFT, padx=10)
        r = row(sec, "全局扫码监听")
        glob_v = tk.BooleanVar(value=bool(cfg.get("global_scan")))
        check(r, "启用（窗口没焦点时扫码也能触发）", glob_v).pack(side=tk.LEFT)

        def save():
            try:
                self.cfg["camera_index"] = int(cam_var.get())
            except Exception:
                pass
            res = res_var.get()
            if res == "摄像头默认":
                self.cfg["width"] = 0
                self.cfg["height"] = 0
            else:
                try:
                    w, h = res.split("x")
                    self.cfg["width"] = int(w)
                    self.cfg["height"] = int(h)
                except Exception:
                    pass
            self.cfg["save_dir"] = dir_var.get().strip() or self.cfg["save_dir"]
            self.cfg["terminal_id"] = tid_var.get().strip() or "10001"
            self.cfg["pre_roll_seconds"] = int(pre_var.get())
            try:
                zf = float(zf_var.get())
            except Exception:
                zf = 2.0
            self.cfg["zoom_after_scan"] = {"enabled": bool(zoom_var.get()),
                                           "delay": int(zd_var.get()),
                                           "duration": int(zt_var.get()),
                                           "factor": min(4.0, max(1.0, zf))}
            self.cfg["voice_prompt"] = bool(voice_v.get())
            self.cfg["voice_text"] = voice_t.get().strip() or "凯博好帅"
            self.cfg["max_record_seconds"] = int(max_var.get())
            self.cfg["segment_seconds"] = int(seg_var.get())
            self.cfg["auto_stop_seconds"] = int(auto_var.get())
            self.cfg["save_days"] = int(days_var.get())
            self.cfg["disk_min_free_mb"] = int(disk_var.get())
            self.cfg["barcode_filter"] = filt_var.get().strip()
            self.cfg["codec"] = codec_var.get().strip() or "auto"
            self.cfg["reconnect_timeout"] = int(recon_var.get())
            self.cfg["mirror"] = bool(mirror_v.get())
            self.cfg["auto_crop_black"] = bool(crop_v.get())
            self.cfg["mjpg"] = bool(mjpg_v.get())
            self.cfg["guide_box"] = bool(guide_v.get())
            self.cfg["motion_box"] = bool(motion_v.get())
            self.cfg["photo_only"] = bool(photo_v.get())
            self.cfg["global_scan"] = bool(glob_v.get())
            save_config(self.cfg)
            # 播报文字改了就重新提前合成一份缓存
            try:
                presynth_voice(self.cfg.get("voice_text", "凯博好帅") or "凯博好帅")
            except Exception:
                pass
            self.mirror_var.set(self.cfg["mirror"])
            # 摄像头或分辨率变了就重开摄像头
            self.rec.stop()
            self.rec.cfg = self.cfg
            self._latest_preview = None
            self._start_camera()
            self._tick_disk()
            win.destroy()
            self._set_status("设置已保存。")

        self._flat_btn(win, "保存设置", self.C_GREEN, font=self.F_BIG,
                       padx=0, pady=12, command=save).pack(
                           side=tk.BOTTOM, fill=tk.X, padx=16, pady=12)

    # ---------- 扫码记录窗口 ----------
    def _open_records(self):
        win = tk.Toplevel(self.root)
        win.title("扫码记录")
        win.geometry("720x480")
        win.configure(bg=self.C_BG)
        win.transient(self.root)
        style = ttk.Style(win)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", font=self.F_BODY, rowheight=32,
                        background="white", fieldbackground="white",
                        borderwidth=0, relief="flat")
        style.configure("Treeview.Heading",
                        font=("Microsoft YaHei", 11, "bold"),
                        background=self.C_HEAD, foreground="white",
                        relief="flat")
        style.map("Treeview",
                  background=[("selected", "#DBEAFE")],
                  foreground=[("selected", self.C_TEXT)])

        # 搜索卡片
        top = tk.Frame(win, bg=self.C_CARD,
                       highlightbackground=self.C_LINE, highlightthickness=1)
        top.pack(fill=tk.X, padx=16, pady=12)
        tk.Label(top, text="搜索单号 / 日期", bg=self.C_CARD, fg=self.C_MUTED,
                 font=self.F_BODY).pack(side=tk.LEFT, padx=(14, 6), pady=12)
        kw = tk.Entry(top, font=self.F_BODY, width=28, relief="flat",
                      highlightthickness=1, highlightbackground=self.C_LINE,
                      highlightcolor=self.C_GREEN, bg="#F8FAFC")
        kw.pack(side=tk.LEFT, pady=10, ipady=4)
        tk.Label(top, text="双击一行打开视频", bg=self.C_CARD, fg=self.C_MUTED,
                 font=self.F_SMALL).pack(side=tk.RIGHT, padx=14)

        # 列表卡片
        frame = tk.Frame(win, bg=self.C_CARD,
                         highlightbackground=self.C_LINE, highlightthickness=1)
        frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 12))
        tree = ttk.Treeview(frame, columns=("time", "no", "dur", "file"),
                            show="headings")
        tree.heading("time", text="时间")
        tree.heading("no", text="单号")
        tree.heading("dur", text="时长（秒）")
        tree.heading("file", text="文件")
        tree.column("time", width=170)
        tree.column("no", width=160)
        tree.column("dur", width=80)
        tree.column("file", width=260)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        def refresh():
            for i in tree.get_children():
                tree.delete(i)
            for idx, r in enumerate(read_records(kw.get().strip())):
                tree.insert("", tk.END, values=(r.get("time", ""), r.get("no", ""),
                                                r.get("dur", ""),
                                                r.get("file", "")),
                            tags=(r.get("path", ""),
                                  "odd" if idx % 2 else "even"))
            tree.tag_configure("odd", background="#F8FAFC")
            tree.tag_configure("even", background="white")

        kw.bind("<Return>", lambda e: refresh())
        self._flat_btn(top, "搜索", self.C_GREEN, font=self.F_BODY,
                       padx=18, pady=6, command=refresh).pack(side=tk.LEFT,
                                                              padx=10, pady=10)

        def open_file(_e=None):
            sel = tree.selection()
            if not sel:
                return
            path = tree.item(sel[0], "tags")[0]
            if path and os.path.exists(path):
                if os.name == "nt":
                    os.startfile(path)
                else:
                    os.system("xdg-open '%s' &" % path)

        tree.bind("<Double-1>", open_file)
        bot = tk.Frame(win, bg=self.C_BG)
        bot.pack(fill=tk.X, padx=16, pady=(0, 12))
        self._flat_btn(bot, "导出 Excel", self.C_GREEN, font=self.F_BIG,
                       padx=20, pady=9, command=self._export_records).pack(side=tk.LEFT)
        self._flat_btn(bot, "▶  打开选中视频", self.C_BLUE, font=self.F_BIG,
                       padx=20, pady=9, command=open_file).pack(side=tk.RIGHT)
        refresh()

    def _export_records(self):
        """导出扫码记录为 Excel（没装 openpyxl 时提示改存 CSV）。"""
        rows = read_records()
        if not rows:
            messagebox.showinfo("导出 Excel", "暂无扫码记录。")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel 工作簿", "*.xlsx"), ("CSV 文件", "*.csv")],
            initialfile="扫码记录_%s.xlsx"
            % datetime.datetime.now().strftime("%Y%m%d"))
        if not path:
            return

        def _size_mb(p):
            try:
                if p and os.path.exists(p):
                    return round(os.path.getsize(p) / (1024 ** 2), 2)
            except Exception:
                pass
            return ""

        header = ["单号", "扫码时间", "时长（秒）", "文件", "大小（MB）"]
        data = [[r.get("no", ""), r.get("time", ""), r.get("dur", ""),
                 r.get("file", ""), _size_mb(r.get("path", ""))]
                for r in rows]
        if path.lower().endswith(".csv"):
            import csv
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(header)
                    w.writerows(data)
            except Exception as ex:
                messagebox.showerror("导出失败", "导出失败：%s" % ex)
                return
        else:
            try:
                from openpyxl import Workbook
            except ImportError:
                messagebox.showinfo(
                    "导出 Excel",
                    "本机未安装 openpyxl（pip install openpyxl），"
                    "请改选保存为 CSV 格式。")
                return
            try:
                wb = Workbook()
                ws = wb.active
                ws.title = "扫码记录"
                ws.append(header)
                for row in data:
                    ws.append(row)
                wb.save(path)
            except Exception as ex:
                messagebox.showerror("导出失败", "导出失败：%s" % ex)
                return
        self._set_status("已导出 %d 条扫码记录：%s"
                         % (len(rows), os.path.basename(path)))

    # ---------- 扫码枪全局监听（可选） ----------
    def _start_global_listener(self):
        if not self.cfg.get("global_scan"):
            return
        try:
            import keyboard
        except Exception:
            self._set_status("全局扫码监听未启用：缺少 keyboard 库（安装时会自动装）。")
            return

        def on_key(e):
            if e.event_type != "down":
                return
            now = time.time()
            if now - self._scan_last > 0.08:
                self._scan_buf = ""
            self._scan_last = now
            if e.name == "enter":
                buf = self._scan_buf
                self._scan_buf = ""
                if len(buf) >= 8:
                    self.root.after(0, lambda b=buf: self._global_scan(b))
            elif len(e.name) == 1:
                self._scan_buf += e.name

        try:
            keyboard.on_press(on_key)
        except Exception as ex:
            self._set_status("全局扫码监听启动失败：%s" % ex)

    def _global_scan(self, buf):
        # 只有窗口没焦点时才走全局通道；有焦点时输入框自己会收到
        if self.root.focus_get() is not None:
            return
        self.entry.delete(0, tk.END)
        self.entry.insert(0, buf)
        self.on_scan()

    # ---------- 自测 ----------
    def _selftest_step1(self):
        print("SELFTEST: simulate scan", flush=True)
        self.entry.delete(0, tk.END)
        self.entry.insert(0, "TEST1234567890")
        self.on_scan()
        self.root.after(3000, self._selftest_step2)

    def _selftest_step2(self):
        path = self.rec.stop_recording(save=True)
        if path:
            self._on_saved(path)
            print("SELFTEST OK saved:", path, flush=True)
        else:
            print("SELFTEST FAIL: nothing recorded", flush=True)
        # 多等几秒，让后台的时长校准线程跑完再退出（走 _on_close，先停摄像头）
        self.root.after(4000, self._on_close)

    def _on_close(self):
        try:
            self.rec.stop()
        except Exception:
            pass
        try:
            # 等后台的时长校准写完再退出，否则最后一段视频会快放
            self.rec.wait_for_rewrites(timeout=20)
        except Exception:
            pass
        self.root.destroy()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-video", default=None,
                    help="测试用：用视频文件代替摄像头")
    ap.add_argument("--selftest", action="store_true",
                    help="自测：自动扫码录制 3 秒后退出")
    args = ap.parse_args()
    cfg = load_config()
    root = tk.Tk()
    App(root, cfg, test_video=args.test_video, selftest=args.selftest)
    root.mainloop()


if __name__ == "__main__":
    main()
