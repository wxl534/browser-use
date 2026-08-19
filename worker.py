import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'core'))
sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))

from dotenv import load_dotenv

from browser_use import Agent, Browser, ChatOpenAI
from browser_use.browser import ProxySettings
from browser_use.llm.exceptions import ModelAuthBlockedError
from browser_use.llm.messages import UserMessage
from idp_page_progress import select_next_page
from task_parse import (
    extract_search_keyword,
    extract_target_image_count,
    keyword_title_prefix,
    sanitize_run_folder_name,
)
from cache_layout import (
    cache_has_content,
    cache_is_locked,
    count_downloaded_records,
    process_is_running,
)

# 从独立的工具注册模块导入
from tools_registry import (
    DownloadCurrentIdpSearchPageImagesParams,
    NavigateIdpSearchPageParams,
    _attempt_cloudflare_autoclick,
    _detect_human_verification,
    configure_runtime_paths,
    download_current_idp_search_page_images,
    format_download_validation_report,
    navigate_idp_search_page,
    tools,
    validate_download_artifacts,
)

# 项目根目录(基于脚本位置,不再硬编码)
BASE_DIR = Path(__file__).resolve().parent

# 全局标志:用于控制是否退出
should_quit = False

IMAGE_EXTENSIONS = ('*.tif', '*.tiff', '*.png', '*.jpg', '*.jpeg', '*.gif', '*.webp')
LOG_ROTATE_THRESHOLD_BYTES = 50 * 1024 * 1024
CACHE_BASE_NAME = 'ImagesCache'


def _build_proxy_from_env() -> ProxySettings | None:
    """从环境变量构造代理配置(攻击 Cloudflare 的 L1 IP 信誉层).

    住宅/移动代理可在 IP 被 Cloudflare 标记后换一个干净 IP,使信誉计数器清零.
    仅当设置了 IDP_PROXY_SERVER 时启用,否则返回 None(直连,保持现有行为).
    """
    server = os.environ.get('IDP_PROXY_SERVER', '').strip()
    if not server:
        return None
    return ProxySettings(
        server=server,
        bypass=os.environ.get('IDP_PROXY_BYPASS', '').strip() or None,
        username=os.environ.get('IDP_PROXY_USERNAME', '').strip() or None,
        password=os.environ.get('IDP_PROXY_PASSWORD', '').strip() or None,
    )


def build_browser(image_dir) -> Browser:
    """构造 Browser 实例,优先使用真实 Chrome profile 以绕过 Cloudflare 人机验证.

    Cloudflare Turnstile 的判定主要发生在后台指纹 + IP 信誉层,而非"点击复选框"本身.
    用真实 Chrome(带真实 cookie/历史/cf_clearance 的 user_data_dir)出场,指纹与信誉
    双过关后,目标网站通常会静默放行,甚至不弹验证页--这是不花钱,最可能见效的方案.

    通过环境变量配置(不设置则完全回退到原 Chromium + 本地 browser_profile 行为):
      - IDP_CHROME_EXECUTABLE:真实 Chrome 可执行文件路径
          (Windows 默认: C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe)
      - IDP_CHROME_USER_DATA_DIR:真实 Chrome 用户数据目录
          (Windows 默认: %LOCALAPPDATA%\\Google\\Chrome\\User Data)
      - IDP_CHROME_PROFILE_DIRECTORY:profile 子目录名(默认 'Default')
      - IDP_PROXY_SERVER 等:可选住宅代理(见 _build_proxy_from_env)

    注意:使用真实 Chrome 的 user_data_dir 前,必须先完全关闭 Chrome,否则会因
    profile 被占用而启动失败.建议为爬虫单独建一个已登录过目标站点的 profile 目录.
    """
    proxy = _build_proxy_from_env()

    # 主方案:注入 Scrapling 取得的 cf_clearance 等 cookie(storage_state),
    # 让浏览器揣着"已通过人机验证的通行证"进站,跳过 Cloudflare Turnstile.
    # 由 fetch_cf_cookie.py 产出;未设置或文件不存在则忽略(零破坏回退).
    storage_state = None
    cf_user_agent = None
    storage_state_path = os.environ.get('IDP_STORAGE_STATE', '').strip()
    if storage_state_path:
        if Path(storage_state_path).exists():
            storage_state = storage_state_path
            print(f"🍪 注入 cf_clearance 通行证(storage_state):{storage_state_path}")
            print("⚠️  cf_clearance 与签发它的出口 IP 绑定,请确保本次出口 IP 与取证时一致.")
            # cf_clearance 轻度绑 UA:复用取证时的 User-Agent,避免 Cloudflare 拒收通行证.
            try:
                _meta = json.loads(Path(storage_state_path).read_text(encoding='utf-8')).get('_meta', {})
                cf_user_agent = (_meta or {}).get('user_agent') or None
            except (json.JSONDecodeError, OSError):
                cf_user_agent = None
            if cf_user_agent:
                print(f"🧬 对齐取证 User-Agent:{cf_user_agent}")
        else:
            print(f"⚠️  IDP_STORAGE_STATE 指向的文件不存在：{storage_state_path}，本次不注入 cookie")

    executable_path = os.environ.get('IDP_CHROME_EXECUTABLE', '').strip()
    user_data_dir = os.environ.get('IDP_CHROME_USER_DATA_DIR', '').strip()

    use_real_chrome = bool(executable_path and user_data_dir)
    if use_real_chrome and not Path(executable_path).exists():
        print(f"⚠️  IDP_CHROME_EXECUTABLE 指向的文件不存在：{executable_path}，回退到内置 Chromium")
        use_real_chrome = False

    if use_real_chrome:
        profile_directory = os.environ.get('IDP_CHROME_PROFILE_DIRECTORY', 'Default').strip() or 'Default'
        print(
            "🛡️  使用真实 Chrome profile 以绕过人机验证:"
            f"\n     executable_path={executable_path}"
            f"\n     user_data_dir={user_data_dir}"
            f"\n     profile_directory={profile_directory}"
            + (f"\n     proxy={proxy.server}" if proxy else "")
        )
        print("⚠️  请确保已完全关闭该 Chrome,否则 profile 被占用会启动失败.")
        return Browser(
            executable_path=executable_path,
            user_data_dir=user_data_dir,
            profile_directory=profile_directory,
            channel='chrome',
            headless=False,
            enable_default_extensions=False,
            downloads_path=str(image_dir),
            proxy=proxy,
            storage_state=storage_state,
            user_agent=cf_user_agent,
        )

    # 回退:内置 Chromium + 项目内 browser_profile(与原行为一致)
    if proxy:
        print(f"🌐 使用代理（内置 Chromium）：{proxy.server}")
    return Browser(
        args=[
            f'--user-data-dir={BASE_DIR / "browser_profile"}'
        ],
        headless=False,
        enable_default_extensions=False,
        downloads_path=str(image_dir),  # 下载文件保存到 image 目录
        proxy=proxy,
        storage_state=storage_state,
        user_agent=cf_user_agent,
    )


def archive_cache(images_root: Path, cache_dir: Path, keyword: str) -> Path | None:
    if not cache_has_content(cache_dir):
        return None
    archive_name = sanitize_run_folder_name(keyword)
    target_dir = images_root / archive_name
    if target_dir.exists():
        target_dir = images_root / f'{archive_name}_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
    shutil.move(str(cache_dir), str(target_dir))
    print(f"🗃️ 已归档上一次运行缓存：{cache_dir} -> {target_dir}")
    return target_dir


def select_active_cache_dir(
    base_dir: Path, *, resume_run: bool, keyword: str, explicit_run_dir: Path | None = None
) -> Path:
    """
    选择本次运行的 ImagesCache.新流程会先把旧 ImagesCache 归档为搜索词目录.
    如果已有运行中的 lock,则使用 ImagesCache_01 / _02.

    当外部 supervisor(auto_run_until_target.py)通过 BROWSER_USE_RUN_DIR 显式指定目录时,
    直接使用它:supervisor 已经负责归档旧缓存/选择续跑目录,main.py 不应再独立挑目录,
    否则两套系统可能各算各的,把图片下到不同的 ImagesCache_xx 而导致进度对不上.
    """
    images_root = base_dir / 'Images'
    images_root.mkdir(parents=True, exist_ok=True)

    if explicit_run_dir is not None:
        run_dir = Path(explicit_run_dir).expanduser()
        if not run_dir.is_absolute():
            run_dir = (base_dir / run_dir).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    base_cache = images_root / CACHE_BASE_NAME

    if not resume_run and base_cache.exists() and not cache_is_locked(base_cache):
        archive_cache(images_root, base_cache, keyword)

    candidates = [base_cache, *[images_root / f'{CACHE_BASE_NAME}_{index:02d}' for index in range(1, 100)]]
    if resume_run:
        for candidate in candidates:
            if candidate.exists() and not cache_is_locked(candidate):
                return candidate

    for candidate in candidates:
        if not cache_is_locked(candidate):
            if not resume_run and cache_has_content(candidate):
                archive_cache(images_root, candidate, keyword)
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
    raise RuntimeError('没有可用的 ImagesCache 目录;请检查并关闭其他运行中的任务.')


def write_run_lock(run_dir: Path, keyword: str, target_image_count: int, resume_run: bool) -> None:
    lock = {
        'pid': os.getpid(),
        'keyword': keyword,
        'target_count': target_image_count,
        'resume': resume_run,
        'started_at': datetime.now().isoformat(),
        'run_dir': str(run_dir),
    }
    (run_dir / 'run.lock').write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding='utf-8')
    (run_dir / 'run_config.json').write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding='utf-8')


def read_task_file(task_file: Path) -> str:
    """
    同步读取 task 文件内容,避免在 async main 中直接做阻塞文件 I/O.
    """
    return task_file.read_text(encoding='utf-8').strip()


def build_agent_run_limits(target_image_count: int) -> tuple[int, int, int]:
    """
    根据目标下载数量设置运行限制.下载任务主要靠批量工具完成,
    不应允许 LLM 超时/浏览器异常累计到上千次才停止.
    """
    safe_target = max(1, target_image_count)
    max_failures = min(80, max(20, safe_target // 20))
    max_actions_per_step = 4 if safe_target >= 25 else 3
    max_steps = max(1000, safe_target * 20)
    return max_failures, max_actions_per_step, max_steps


def backup_runtime_state(run_dir: Path, files: list[Path]) -> Path | None:
    """
    清理运行状态前先完整备份 browseruse_agent_data,并补充备份旧版 image/rename_record.txt.
    """
    existing_files = [path for path in files if path.exists() and path.is_file()]
    if not cache_has_content(run_dir) and not existing_files:
        return None

    backup_dir = run_dir.parent / f'{run_dir.name}_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
    backup_dir.mkdir(parents=True, exist_ok=True)
    if cache_has_content(run_dir):
        shutil.copytree(run_dir, backup_dir / run_dir.name, dirs_exist_ok=True)
    for path in existing_files:
        relative_path = path.relative_to(run_dir)
        target_path = backup_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target_path)
    return backup_dir


def prepare_runtime_state(run_dir: Path, reset_state: bool = True) -> tuple[Path, Path]:
    """
    初始化本次运行需要的目录和状态文件,避免旧结果污染长任务.
    """
    image_dir = run_dir
    agent_data_dir = run_dir
    rename_record_file = run_dir / 'rename_record.txt'
    image_record_file = run_dir / 'image_record.jsonl'
    temple_info_file = run_dir / 'temple_photo_info.md'
    idp_progress_file = run_dir / 'idp_progress.json'

    run_dir.mkdir(parents=True, exist_ok=True)

    if reset_state:
        backup_dir = backup_runtime_state(
            run_dir,
            [rename_record_file, image_record_file, temple_info_file, idp_progress_file],
        )
        if backup_dir:
            print(f"🛟 已完整备份旧 ImagesCache 到：{backup_dir}")

    if reset_state and rename_record_file.exists():
        rename_record_file.unlink()

    if reset_state:
        for path in list(run_dir.iterdir()):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    return image_dir, agent_data_dir


def count_existing_image_files(image_dir: Path) -> int:
    """
    统计 image 目录中的本地图片文件数量,用于判断是否更适合默认续跑.
    """
    if not image_dir.exists():
        return 0
    total = 0
    for pattern in IMAGE_EXTENSIONS:
        total += len([path for path in image_dir.glob(pattern) if path.is_file()])
    return total


def ask_resume_from_checkpoint(base_dir: Path) -> bool:
    """
    启动时询问是否保留上次记录并从断点续跑.
    未设置 BROWSER_USE_RESUME_RUN 时必须明确输入 y 或 n,避免误按回车走错流程.
    """
    env_value = os.environ.get('BROWSER_USE_RESUME_RUN', '').strip().lower()
    if env_value in {'1', 'true', 'yes', 'on'}:
        print("♻️  BROWSER_USE_RESUME_RUN=1:自动选择从上次断点继续")
        return True
    if env_value in {'0', 'false', 'no', 'off'}:
        print("🆕 BROWSER_USE_RESUME_RUN=0:自动选择开启新流程")
        return False

    cache_dir = base_dir / 'Images' / CACHE_BASE_NAME
    record_file = cache_dir / 'image_record.jsonl'
    image_dir = cache_dir
    if not record_file.exists():
        record_file = base_dir / 'browseruse_agent_data' / 'image_record.jsonl'
        image_dir = base_dir / 'image'
    existing_count = 0
    if record_file.exists():
        existing_count = count_downloaded_records(record_file)
        image_count = count_existing_image_files(image_dir)
        print(f"\n📌 检测到上次运行状态：{record_file}（downloaded={existing_count}，本地图片={image_count}）")
    else:
        image_count = count_existing_image_files(image_dir)
        if image_count:
            print(f"\n📌 未检测到上次图片记录，但 image 目录已有 {image_count} 个图片文件")
        else:
            print("\n📌 未检测到上次图片记录")

    while True:
        try:
            answer = input("是否接着上一次的断点运行?输入 y/yes 继续,输入 n/no 开启新流程 [y/n]: ")
        except (EOFError, KeyboardInterrupt):
            print("\n🛑 未收到明确选择,为避免误清理或误续跑,已停止启动")
            raise SystemExit(130)

        normalized_answer = answer.strip().lower()
        if normalized_answer in {'y', 'yes', '是', '继续', 'resume'}:
            return True
        if normalized_answer in {'n', 'no', '否', '新流程', 'new'}:
            return False
        print("⚠️ 请输入 y/yes 继续上次断点,或 n/no 开启新流程.")


def load_downloaded_image_records(record_file: Path) -> list[dict]:
    """
    读取通用图片记录,只保留 status=downloaded 的有效记录.
    """
    if not record_file.exists():
        return []

    records: list[dict] = []
    for line in record_file.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get('status') == 'downloaded':
            records.append(record)

    def sort_key(record: dict) -> tuple[int, str]:
        try:
            sequence = int(record.get('sequence') or 0)
        except (TypeError, ValueError):
            sequence = 0
        return sequence, str(record.get('file_name') or '')

    return sorted(records, key=sort_key)


def load_idp_progress(base_dir: Path) -> dict:
    progress_file = base_dir / 'idp_progress.json'
    if not progress_file.exists():
        progress_file = base_dir / 'browseruse_agent_data' / 'idp_progress.json'
    if not progress_file.exists():
        return {}
    try:
        data = json.loads(progress_file.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def sync_idp_progress_from_page_queue(run_dir: Path, target_image_count: int, search_keyword: str) -> dict:
    """
    使用 idp_page_progress.json 选择续跑页,并同步导出到 idp_progress.json.
    image_record.jsonl 仍是图片级事实来源;idp_page_progress.json 是页级事实来源.
    """
    legacy_progress = load_idp_progress(run_dir)
    try:
        fallback_page = max(1, int(legacy_progress.get('next_page') or legacy_progress.get('current_page') or 1))
    except (TypeError, ValueError):
        fallback_page = 1
    max_page_text = os.environ.get('BROWSER_USE_IDP_MAX_REASONABLE_PAGE', '').strip()
    try:
        max_reasonable_page = max(1, int(max_page_text)) if max_page_text else max(fallback_page, (target_image_count // 25) + 20)
    except ValueError:
        max_reasonable_page = max(fallback_page, (target_image_count // 25) + 20)
    active = select_next_page(
        run_dir,
        keyword=search_keyword,
        target_count=target_image_count,
        fallback_page=fallback_page,
        max_reasonable_page=max_reasonable_page,
    )
    progress = {
        **legacy_progress,
        'keyword': search_keyword,
        'target_count': target_image_count,
        'current_page': active['page'],
        'next_page': active['page'],
        'next_index': active['next_index'],
        'source': 'synced_from_idp_page_progress',
        'page_progress_file': str(run_dir / 'idp_page_progress.json'),
        'updated_at': datetime.now().isoformat(),
    }
    (run_dir / 'idp_progress.json').write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding='utf-8')
    return active


def build_resume_task_context(base_dir: Path, target_image_count: int, search_keyword: str = 'china buddhist') -> str:
    """
    根据 browseruse_agent_data/image_record.jsonl 生成断点续跑说明,附加到 task 给 agent.
    """
    record_file = base_dir / 'image_record.jsonl'
    if not record_file.exists():
        record_file = base_dir / 'browseruse_agent_data' / 'image_record.jsonl'
    records = load_downloaded_image_records(record_file)
    if not records:
        return (
            "\n\n## 断点续跑上下文\n\n"
            "用户选择了从断点继续,但 `browseruse_agent_data/image_record.jsonl` 中没有可用的 downloaded 记录."
            "请从第 1 张开始执行;仍然使用工具的安全序号,不能覆盖已有文件.\n"
        )

    def sequence_of(record: dict) -> int:
        try:
            return int(record.get('sequence') or 0)
        except (TypeError, ValueError):
            return 0

    max_sequence = max(sequence_of(record) for record in records)
    next_sequence = max_sequence + 1
    remaining_by_count = max(0, target_image_count - len(records))
    image_count = count_existing_image_files(base_dir)
    if image_count == 0:
        image_count = count_existing_image_files(base_dir / 'image')
    progress = load_idp_progress(base_dir)
    progress_page = progress.get('next_page') or progress.get('current_page')
    progress_index = progress.get('next_index', 0)
    try:
        suggested_page = max(1, int(progress_page))
    except (TypeError, ValueError):
        suggested_page = max(1, (max_sequence // 50) + 1)
    try:
        suggested_start_index = max(0, int(progress_index))
    except (TypeError, ValueError):
        suggested_start_index = 0
    title_prefix = keyword_title_prefix(search_keyword)
    last_record = records[-1]

    template = (BASE_DIR / 'resume_context_template.md').read_text(encoding='utf-8')
    body = template.format(
        record_file=record_file,
        record_count=len(records),
        image_count=image_count,
        idp_progress_path=base_dir / 'idp_progress.json',
        suggested_page=suggested_page,
        suggested_start_index=suggested_start_index,
        max_sequence=max_sequence,
        next_sequence=next_sequence,
        title_prefix=title_prefix,
        search_keyword=search_keyword,
        target_image_count=target_image_count,
        remaining_by_count=remaining_by_count,
        last_sequence=sequence_of(last_record),
        last_file_name=last_record.get('file_name', ''),
        last_title=last_record.get('collection_title') or last_record.get('title') or '',
        last_page_url=last_record.get('page_url', ''),
    )
    return "\n\n" + body


async def run_idp_resume_preflight(
    *,
    browser: Browser,
    run_dir: Path,
    target_image_count: int,
    search_keyword: str,
) -> None:
    """
    断点续跑时由代码先完成第一组确定性动作,避免依赖 Agent 记住 prompt.
    """
    # 续跑预检在 Agent 启动浏览器之前运行,必须先确保浏览器会话已连接,
    # 否则 navigate/CDP 调用会因 Root CDP client 未初始化而断言失败.start() 幂等.
    await browser.start()
    active = sync_idp_progress_from_page_queue(run_dir, target_image_count, search_keyword)
    page = int(active.get('page') or 1)
    start_index = int(active.get('next_index') or 0)
    print(f"\n♻️ 代码预执行断点首批动作：page={page}, start_index={start_index}")

    navigate_result = await navigate_idp_search_page(
        params=NavigateIdpSearchPageParams(keyword=search_keyword, page=page, limit=50),
        browser_session=browser,
    )
    if navigate_result.error:
        print(f"⚠️ 断点预导航失败，交给 Agent 处理：{navigate_result.error}")
        return
    if navigate_result.extracted_content:
        print(navigate_result.extracted_content)

    # 续跑落到搜索页后常见 Cloudflare 挑战页:先尝试自动点击通过,失败则交给 Agent(可人工解).
    try:
        challenge = await _detect_human_verification(browser)
        if challenge.get('is_challenge'):
            print("🛡️ 检测到 Cloudflare/人机验证页,尝试自动点击通过…")
            solved = await _attempt_cloudflare_autoclick(browser, attempts=3)
            if solved:
                print("✅ 已自动点击通过人机验证,继续预执行批量下载.")
                navigate_result = await navigate_idp_search_page(
                    params=NavigateIdpSearchPageParams(keyword=search_keyword, page=page, limit=50),
                    browser_session=browser,
                )
                if navigate_result.error:
                    print(f"⚠️ 验证通过后重导航失败，交给 Agent 处理：{navigate_result.error}")
                    return
            else:
                print("⚠️ 自动点击未能通过人机验证(可能是交互式挑战),交给 Agent / 人工处理.")
                return
    except Exception as verify_exc:
        print(f"⚠️ 人机验证自动处理出错，交给 Agent 处理：{verify_exc}")
        return

    batch_result = await download_current_idp_search_page_images(
        params=DownloadCurrentIdpSearchPageImagesParams(
            target_count=target_image_count,
            max_items=50,
            start_index=start_index,
            images_per_item=1,
            file_prefix='temple',
            title_prefix=keyword_title_prefix(search_keyword),
            record_filename='image_record.jsonl',
            info_filename='temple_photo_info.md',
        ),
        browser_session=browser,
    )
    if batch_result.error:
        print(f"⚠️ 断点预批量下载失败，交给 Agent 处理：{batch_result.error}")
        return
    if batch_result.extracted_content:
        print(batch_result.extracted_content)


async def finalize_download_run(
    history,
    *,
    should_quit: bool,
    image_dir: Path,
    agent_data_dir: Path,
    target_image_count: int,
) -> None:
    """
    无论正常结束还是用户手动停止,都执行状态重建,验证和自动重命名.
    """
    if should_quit:
        print("\n🛑 程序已被用户手动停止,继续执行收尾验证和重命名")

    print("\n=== 下载结果验证 ===")
    all_image_files = []
    if image_dir.exists():
        found = []
        for ext in IMAGE_EXTENSIONS:
            found.extend(image_dir.glob(ext))
        all_image_files.extend(found)
        print(f"✓ 目录 {image_dir} 中找到 {len(found)} 个图片文件")
    else:
        print(f"ℹ️ 目录不存在：{image_dir}")

    structured_record_file = agent_data_dir / 'image_record.jsonl'
    downloaded_record_count = count_downloaded_records(structured_record_file)
    print(
        f"\n📊 结果汇总：目标 {target_image_count} 张，"
        f"结构化记录 {downloaded_record_count} 条，已下载图片 {len(all_image_files)} 个"
    )

    if all_image_files:
        print(f"\n总共找到 {len(all_image_files)} 个下载的文件:")
        for img_file in sorted(all_image_files):
            file_size = img_file.stat().st_size
            print(f"  - {img_file.name}: {file_size:,} 字节")
            if file_size == 0:
                print(f"  ⚠️ 警告：{img_file.name} 文件大小为 0")
    else:
        print("❌ 未找到任何下载的文件")

    errors = history.errors()
    if any(errors):
        error_count = sum(1 for e in errors if e is not None)
        print(f"\n⚠️ 执行过程中出现 {error_count} 个错误")

    print("\n=== 任务统计 ===")
    print(f"总步数：{history.number_of_steps()}")
    print(f"总耗时：{history.total_duration_seconds():.2f} 秒")
    print(f"访问 URL 数：{len(history.urls())}")

    def write_final_validation() -> dict:
        validation_result = validate_download_artifacts(target_count=target_image_count)
        report = format_download_validation_report(validation_result)
        report_file = agent_data_dir / 'final_download_report.md'
        report_file.write_text(report + '\n', encoding='utf-8')
        print("\n=== 最终下载校验(以本地记录为准) ===")
        print(report)
        if validation_result['complete']:
            print("✅ 最终校验通过:可以视为任务成功")
        else:
            print("❌ 最终校验未通过:不能声称已完成目标数量")

        print("\n=== SQLite 数据库导入 ===")
        sqlite_import_script = BASE_DIR / 'import_records_to_sqlite.py'
        db_file = agent_data_dir / 'image_catalog.sqlite3'
        import_ok = run_python_script(
            str(sqlite_import_script),
            "SQLite 图片记录导入",
            [
                '--record-file',
                str(agent_data_dir / 'image_record.jsonl'),
                '--image-dir',
                str(image_dir),
                '--db-file',
                str(db_file),
            ],
        )
        if import_ok:
            print(f"✅ SQLite 数据库已更新：{db_file}")
        else:
            print(f"⚠️ SQLite 数据库导入失败，请手动执行：python {sqlite_import_script}")
        return validation_result

    if not all_image_files:
        print("💡 未检测到下载图片,跳过自动重命名")
        write_final_validation()
        return

    if downloaded_record_count == 0:
        print("💡 未收集到下载记录,跳过自动重命名")
        write_final_validation()
        return

    print("\n✅ 图片下载工具已在每张图片保存成功后立即完成最终命名,跳过旧的批量重命名步骤")
    write_final_validation()

# 临时解决方案:绑定 hosts
# 10.64.84.182 openapi.seu.edu.cn
load_dotenv()

# === 完全禁用截图功能的环境变量配置 ===
# 增加点击事件超时时间,避免下载等待时的超时警告
os.environ['TIMEOUT_ClickElementEvent'] = '60.0'  # 从默认 15s 增加到 60s
os.environ['TIMEOUT_ScreenshotEvent'] = '60.0'    # 截图事件超时也增加
os.environ.setdefault('BROWSER_USE_LIGHTWEIGHT_DOWNLOADS', '1')
os.environ.setdefault('BROWSER_USE_DISABLE_SCREENSHOTS', '1')
os.environ.setdefault('BROWSER_USE_LIGHTWEIGHT_DOM', '1')
print("✅ 已配置环境变量:启用轻量下载/DOM模式,禁用截图,增加事件超时时间")

#临时添加 host 映射,仅用在学校llm
def add_host_mapping(host, ip):
    """临时添加 host 映射到本地"""
    try:
        # 尝试解析域名,看是否已经配置
        socket.gethostbyname(host)
        print(f"✓ Host '{host}' 已配置")
    except socket.gaierror:
        print(f"⚠ 注意：需要在系统 hosts 文件中添加映射：{ip} {host}")
        print("  Windows: C:\\Windows\\System32\\drivers\\etc\\hosts")
        print(f"  以管理员身份运行记事本，添加：{ip} {host}")

# 检查 host 配置
add_host_mapping('openapi.seu.edu.cn', '10.64.84.182')

# === 导入工具函数 ===
def run_python_script(
    script_path: str,
    description: str = "脚本",
    extra_args: list[str] | None = None,
    timeout_seconds: int = 600,
) -> bool:
    """
    运行 Python 脚本的辅助函数
    
    Args:
        script_path: 脚本的绝对路径
        description: 脚本描述
        extra_args: 额外的命令行参数
        
    Returns:
        是否成功执行
    """
    script = Path(script_path)
    
    if not script.exists():
        print(f"⚠️ 警告:{description}脚本不存在:{script}")
        return False
    
    try:
        cmd = [sys.executable, str(script)]
        if extra_args:
            cmd.extend(extra_args)
        # 使用当前 Python 解释器运行(避免环境问题)
        # 先以二进制模式捕获输出,避免编码错误
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_seconds,
            cwd=str(script.parent),  # 使用脚本所在目录作为工作目录
            env={**os.environ.copy(), 'PYTHONIOENCODING': 'utf-8'}  # 继承当前环境变量并强制 UTF-8 输出
        )
        
        # 手动解码输出:优先尝试 UTF-8,失败则用 GBK(Windows 中文环境)
        try:
            stdout = result.stdout.decode('utf-8')
        except UnicodeDecodeError:
            try:
                stdout = result.stdout.decode('gbk', errors='replace')
            except Exception:
                stdout = result.stdout.decode('utf-8', errors='replace')
        
        try:
            stderr = result.stderr.decode('utf-8')
        except UnicodeDecodeError:
            try:
                stderr = result.stderr.decode('gbk', errors='replace')
            except Exception:
                stderr = result.stderr.decode('utf-8', errors='replace')
        
        # 打印输出
        if stdout:
            print(f"\n📝 {description}输出:")
            print(stdout)
        
        if stderr:
            print(f"\n⚠️ {description}警告/错误:")
            print(stderr)
        
        if result.returncode == 0:
            print(f"\n✅ {description}完成!")
            return True
        else:
            print(f"\n❌ {description}失败,返回码:{result.returncode}")
            return False
            
    except subprocess.TimeoutExpired:
        print(f"\n❌ {description}超时(超过 {timeout_seconds} 秒)")
        return False
    except Exception as e:
        print(f"\n❌ 执行{description}时出错:{e}")
        return False


async def preflight_llm_check(llm, base_url: str) -> None:
    """运行 Agent 前先做一次最小 LLM 探活.

    如果 LLM 端点被校园网/VPN 网关或 WAF 拦截(返回 HTML 门户页而非 JSON),
    会抛出 ModelAuthBlockedError,直接中止本次运行,避免空跑十几分钟并烧 token.
    其他类型的瞬时错误只告警,不阻断,交给正式运行时的重试逻辑处理.
    """
    try:
        await asyncio.wait_for(
            llm.ainvoke([UserMessage(content='ping')]),
            timeout=int(os.environ.get('BROWSER_USE_PREFLIGHT_TIMEOUT', '30')),
        )
        print('✅ LLM 端点预检通过')
    except ModelAuthBlockedError:
        # 致命:网关拦截,重试无意义,直接向上抛出由入口处理退出码.
        raise
    except Exception as e:
        print(f'⚠️ LLM 预检未通过（非拦截类错误，继续启动）：{type(e).__name__}: {e}')


async def run_agent_once(resume_run_override: bool | None = None):
    global should_quit
    should_quit = False

    # === 1. 从 task.md 文件读取任务描述 ===
    task_file = BASE_DIR / 'task.md'

    if not task_file.exists():
        print(f"❌ Task 文件不存在：{task_file}")
        raise FileNotFoundError(f"Task file not found: {task_file}")

    print(f"📄 从文件读取 task: {task_file}")
    task = read_task_file(task_file)
    print(f"✅ 成功读取 task，长度：{len(task)} 字符")

    target_image_count = extract_target_image_count(task, default=1)
    search_keyword = extract_search_keyword(task)
    max_failures, max_actions_per_step, max_steps = build_agent_run_limits(target_image_count)
    resume_run = resume_run_override if resume_run_override is not None else ask_resume_from_checkpoint(BASE_DIR)
    supervised_run_dir = os.environ.get('BROWSER_USE_RUN_DIR', '').strip()
    explicit_run_dir = Path(supervised_run_dir) if supervised_run_dir else None
    if explicit_run_dir is not None:
        print(f"🔗 受 supervisor 监督运行，使用其指定的缓存目录：{explicit_run_dir}")
    run_dir = select_active_cache_dir(
        BASE_DIR, resume_run=resume_run, keyword=search_keyword, explicit_run_dir=explicit_run_dir
    )
    configure_runtime_paths(run_dir=run_dir, image_dir=run_dir, data_dir=run_dir)
    write_run_lock(run_dir, search_keyword, target_image_count, resume_run)
    print(f"📁 本次运行缓存目录：{run_dir}")
    print(
        f"🎯 本次任务目标：下载前 {target_image_count} 张图片 "
        f"(max_failures={max_failures}, max_actions_per_step={max_actions_per_step}, max_steps={max_steps})"
    )

    if resume_run:
        print("\n♻️  保留现有 ImagesCache,从断点继续")
    else:
        print("\n🆕 已归档旧 ImagesCache(如存在),并创建新的 ImagesCache")
    
    # 等待一下确保文件系统更新完成
    await asyncio.sleep(1)
    
    # === 3. 初始化本次运行状态并创建浏览器与 llm 实例 ===
    image_dir, agent_data_dir = prepare_runtime_state(run_dir, reset_state=False)
    if resume_run:
        active = sync_idp_progress_from_page_queue(run_dir, target_image_count, search_keyword)
        print(f"✅ 已从 idp_page_progress.json 选择续跑页：page={active['page']}, start_index={active['next_index']}")
        resume_context = build_resume_task_context(run_dir, target_image_count, search_keyword)
        task = task + resume_context
        print("✅ 已读取 image_record.jsonl,并把断点续跑上下文加入本次任务")

    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    base_url = os.environ.get('OPENAI_BASE_URL', 'https://openapi.seu.edu.cn/v1')

    if not api_key:
        raise ValueError('未设置 OPENAI_API_KEY,无法启动 Agent.请先在 .env 或环境变量中配置.')

    browser = build_browser(image_dir)

    if resume_run:
        await run_idp_resume_preflight(
            browser=browser,
            run_dir=run_dir,
            target_image_count=target_image_count,
            search_keyword=search_keyword,
        )

    llm = ChatOpenAI(
        model='qwen3.5-397b-a17b',
        api_key=api_key,
        base_url=base_url,
        temperature=0.0
    )
    # llm = ChatBrowserUse()  # 官方 LLM,需付费订阅

    # 预检:确认 LLM 端点真正可达(避免校园网/VPN 未连接时被网关拦截,空跑十几分钟烧 token).
    await preflight_llm_check(llm, base_url)

    # quit 回调:agent 每步执行前会调用此函数,返回 True 则停止
    async def check_should_quit() -> bool:
        return should_quit

    # === 4. 创建 Agent(完全禁用截图,使用 JS 提取) ===
    agent = Agent(
        task=task,  # 使用从.md 文件读取的 task
        llm=llm,
        browser=browser,
        tools=tools,
        use_vision=False,
        max_failures=max_failures,
        max_actions_per_step=max_actions_per_step,
        step_timeout=int(os.environ.get('BROWSER_USE_STEP_TIMEOUT', '240')),
        llm_timeout=int(os.environ.get('BROWSER_USE_LLM_TIMEOUT', '180')),
        register_should_stop_callback=check_should_quit,
        file_system_path=str(BASE_DIR),
        available_file_paths=[
            str(BASE_DIR / 'image'),
            str(run_dir),
            str(agent_data_dir),
            str(BASE_DIR / 'source.html'),
        ],
    )
    
    # === 5. 运行 agent ===
    print("\n🚀 开始执行任务...")

    try:
        history = await agent.run(max_steps=max_steps)
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断执行 (Ctrl+C)")
        return None
    finally:
        # 清理本次运行的 run.lock,避免正常退出后残留导致后续误判 cache 被占用.
        # (被硬杀的情况由 supervisor 在轮次之间清理.)
        lock_path = run_dir / 'run.lock'
        try:
            if lock_path.exists():
                lock_path.unlink()
        except OSError:
            pass

    await finalize_download_run(
        history,
        should_quit=should_quit,
        image_dir=image_dir,
        agent_data_dir=agent_data_dir,
        target_image_count=target_image_count,
    )

    return history


async def main():
    return await run_agent_once()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ModelAuthBlockedError as e:
        print(f"\n🛑 LLM 端点被拦截，已中止本次运行：{e.message}")
        print("👉 请确认已连接校园网/VPN,且 OPENAI_API_KEY / OPENAI_BASE_URL 正确后重试.")
        # 专用退出码 3:供 auto_run_until_target.py 识别为致命错误并立即停止重试.
        sys.exit(3)
