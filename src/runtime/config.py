import os

STUDENT_ID = os.environ.get("StuId", "")
PASSWORD = os.environ.get("UISPsw", "")

WEBVPN_BASE = "https://webvpn.fudan.edu.cn"
IDP_BASE = "https://id.fudan.edu.cn"
ICOURSE_BASE = "https://icourse.fudan.edu.cn"

WEBVPN_AES_KEY = b"wrdvpnisthebest!"
WEBVPN_AES_IV = b"wrdvpnisthebest!"

TENANT_CODE = "222"
GROUP_CODE = "2095000001"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 模型服务商配置（按列表顺序作为优先级，从前往后尝试）。
# 用户可以在这里随意添加/删除/重排服务商和模型。
# 兼容性：只设置 DASHSCOPE_API_KEY 也能跑（modelscope 项的 api_key 直接读取它）。
# 同名 provider 多次出现 → resolve_model_providers() 把它们的 models 合并到首次出
# 现的那条；这避免 Summarizer 内部按 name 索引 client 字典时被后写覆盖。
MODEL_PROVIDERS: list[dict] = [
    {
        "name": "deepseek",
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "default_base_url": "https://api.deepseek.com",
        "models": [
            "deepseek-v4-pro",
            "deepseek-v4-flash",
        ],
    },
    {
        "name": "modelscope",
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url_env": "DASHSCOPE_BASE_URL",
        "default_base_url": "https://api-inference.modelscope.cn/v1/",
        "models": [
            "deepseek-ai/DeepSeek-V4-Pro",
            "deepseek-ai/DeepSeek-V4-Flash"
        ],
    },
    {
        "name": "modelscope",
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url_env": "DASHSCOPE_BASE_URL",
        "default_base_url": "https://api-inference.modelscope.cn/v1/",
        "models": [
            "deepseek-ai/DeepSeek-V3.2",
            "ZhipuAI/GLM-5",
            "MiniMax/MiniMax-M2.5",
            "Qwen/Qwen3.5-397B-A17B",
        ],
    },
    {
        "name": "gemini",
        "api_key_env": "GEMINI_API_KEY",
        "base_url_env": "GEMINI_BASE_URL",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "models": [
            "gemini-2.5-flash",
            "gemini-3-flash-preview",
        ],
    },
]


def resolve_model_providers() -> list[dict]:
    """Resolve MODEL_PROVIDERS into runtime configs.

    Drops providers whose api_key env var is unset. Same-name entries get
    their model lists merged into the first occurrence (Summarizer's client
    dict keys on name and would otherwise collide).

    Returns:
        list of {name, api_key, base_url, models}.
    """
    resolved: list[dict] = []
    by_name: dict[str, dict] = {}
    for p in MODEL_PROVIDERS:
        api_key = os.environ.get(p["api_key_env"], "").strip()
        if not api_key:
            continue
        base_url = (
            os.environ.get(p.get("base_url_env", ""), "").strip()
            or p.get("default_base_url", "")
        )
        if not base_url:
            continue
        if p["name"] in by_name:
            existing = by_name[p["name"]]
            for m in p["models"]:
                if m not in existing["models"]:
                    existing["models"].append(m)
            continue
        entry = {
            "name": p["name"],
            "api_key": api_key,
            "base_url": base_url,
            "models": list(p["models"]),
        }
        resolved.append(entry)
        by_name[p["name"]] = entry
    return resolved


# Legacy compatibility shims (kept so other modules importing these don't break)
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# QQ SMTP
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "")
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

# Database & Storage
DATA_DIR = os.environ.get("DATA_DIR", "data")
VIDEO_DIR = os.path.join(DATA_DIR, "videos")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")  # ffmpeg-decoded f32le scratch buffers
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "icourse.db"))

# Sherpa-onnx ASR model directory.  Default: SenseVoice (zh+en+ja+ko+yue, int8).
# ASR_MODEL_DIR is the new name; SENSEVOICE_MODEL_DIR is the legacy env var
# kept as a fallback so existing CI cache keys keep working.
ASR_MODEL_DIR = os.environ.get(
    "ASR_MODEL_DIR",
    os.environ.get(
        "SENSEVOICE_MODEL_DIR",
        "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
    ),
)
SENSEVOICE_MODEL_DIR = ASR_MODEL_DIR  # alias for any straggler imports
SILERO_VAD_PATH = os.environ.get("SILERO_VAD_PATH", "silero_vad.onnx")

# ASR backend selector — Transcriber dispatches on this.  When changing,
# ASR_MODEL_DIR must point at a matching sherpa-onnx model bundle:
#   sensevoice — sherpa-onnx-sense-voice-* (multi-lang CTC, single model)
#   firered    — sherpa-onnx-fire-red-asr2-ctc-* (CTC, single model.onnx)
#   zipformer  — sherpa-onnx-zipformer-* (transducer, encoder/decoder/joiner)
ASR_BACKEND = os.environ.get("ASR_BACKEND", "sensevoice").strip().lower()
# Inference thread count.  4 fully saturates a 4-vCPU GitHub runner when
# ASR is the only thing on the box.  When OCR overlaps, the Scheduler's
# set_asr_active() caps OCR concurrency at OCR_MAX_TARGET_WHEN_ASR_ACTIVE
# so the two workloads share predictably.
ASR_NUM_THREADS = int(os.environ.get("ASR_NUM_THREADS", "4"))

# ── Scheduler concurrency knobs.  All overridable via env. ────────────────
# image_pool: image downloads are tiny and IO-bound, 20 saturates bandwidth
# without hammering the iCourse server.
IMAGE_WORKERS = int(os.environ.get("IMAGE_WORKERS", "20"))
# ocr_pool: CPU-bound RapidOCR runs.  Pool size = ceiling; the DynamicSemaphore
# controlled by ResourceMonitor decides how many of those workers can actually
# execute at the same instant.
OCR_MAX_WORKERS = int(os.environ.get("OCR_MAX_WORKERS", "8"))
OCR_INITIAL_TARGET = int(os.environ.get("OCR_INITIAL_TARGET", "3"))
OCR_MIN_TARGET = int(os.environ.get("OCR_MIN_TARGET", "1"))
# RapidOCR is single-threaded CPU-bound — one page fully saturates one
# core.  Raising this beyond 2 yields no throughput gain but creates
# noisy ResourceMonitor target-churn logs every second.
OCR_MAX_TARGET = int(os.environ.get("OCR_MAX_TARGET", "2"))
# When the LectureRunner is mid-ASR, the OCR pool's effective max target
# drops to this value so the ASR thread block (now num_threads=4) doesn't
# contend with a saturated OCR pool.  Workload split: ASR gets ~2 effective
# cores, OCR gets ~2.  Outside ASR phases (resummarize, between lectures)
# OCR is allowed to ramp up to OCR_MAX_TARGET again.
OCR_MAX_TARGET_WHEN_ASR_ACTIVE = int(
    os.environ.get("OCR_MAX_TARGET_WHEN_ASR_ACTIVE", "2")
)
# ResourceMonitor thresholds (percent CPU). Outside [LOW, HIGH] we adjust
# OCR target down/up by 1 each second.
RESOURCE_MONITOR_CPU_HIGH = int(os.environ.get("RESOURCE_MONITOR_CPU_HIGH", "95"))
RESOURCE_MONITOR_CPU_LOW = int(os.environ.get("RESOURCE_MONITOR_CPU_LOW", "75"))
# Two concurrent ffmpeg audio extractions: the current lecture being
# transcribed + one pre-decoded for the next lecture.  Bandwidth-fair sharing
# at 20 MB/s split = ~10 MB/s each.
VIDEO_DOWNLOAD_CONCURRENCY = int(
    os.environ.get("VIDEO_DOWNLOAD_CONCURRENCY", "2")
)

# Resummarize phase opt-in.  Re-running OCR + LLM on every old lecture is
# expensive (each lecture ~ a minute of LLM + 30 OCR calls); we don't want
# every nightly run paying that cost.  Set RESUMMARIZE_OLD=1 to enable for
# a one-shot manual workflow run.  Default off.
RESUMMARIZE_OLD_ENABLED = os.environ.get("RESUMMARIZE_OLD", "").strip() in (
    "1", "true", "yes", "on",
)

# 监控的课程 ID 列表
COURSE_IDS = [
    c.strip()
    for c in os.environ.get("COURSE_IDS", "").split(",")
    if c.strip()
]

# 学期级课程目录爬取。设置后，每次运行会把该学期的全部课程刷入 all_courses
# 表，供前端订阅编辑器搜索/可视化选择。未设置 → 跳过爬取（默认行为）。
# 例：CRAWL_TERM=25
CRAWL_TERM = os.environ.get("CRAWL_TERM", "").strip()
