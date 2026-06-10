#import os
import shutil
from pathlib import Path
from datetime import datetime

# 使用脚本所在目录作为基准路径，而非硬编码
BASE_DIR = Path(__file__).resolve().parent


def safe_print(message: str) -> None:
    """
    Windows GBK 控制台遇到梵文/变音字符文件名时可能编码失败；降级输出但不中断迁移。
    """
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode('utf-8', errors='backslashreplace').decode('ascii', errors='replace'))


def move_and_clear_images(interactive: bool = True):
    """
    清空 image 文件夹，并将内容移动到 history 文件夹。

    Args:
        interactive: 是否允许交互式确认（subprocess 调用时应为 False）
    """
    # 定义路径
    image_dir = BASE_DIR / "image"
    history_dir = BASE_DIR / "history"
    
    # 创建 history 文件夹（如果不存在）
    history_dir.mkdir(exist_ok=True)

    # 生成带时间戳的子文件夹名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_dir = history_dir / f"image_backup_{timestamp}"
    
    # === 第一部分：处理 image 文件夹 ===
    if image_dir.exists():
        files = list(image_dir.glob("*"))
        
        if files:
            target_dir.mkdir(exist_ok=True)
            safe_print(f"[信息] 准备移动 {len(files)} 个图片文件...")
            
            moved_count = 0
            for file_path in files:
                try:
                    if file_path.is_file():
                        shutil.move(str(file_path), str(target_dir / file_path.name))
                        moved_count += 1
                        safe_print(f"  [成功] {file_path.name}")
                    elif file_path.is_dir():
                        shutil.move(str(file_path), str(target_dir / file_path.name))
                        moved_count += 1
                        safe_print(f"  [成功] [目录] {file_path.name}")
                except Exception as e:
                    safe_print(f"  [失败] 移动失败 {file_path.name}: {e}")
            
            safe_print(f"\n[成功] 完成！成功移动 {moved_count}/{len(files)} 个图片文件")
            safe_print(f"[信息] 目标位置：{target_dir}")
        else:
            safe_print(f"[警告] image 文件夹为空")
    else:
        safe_print(f"[警告] image 文件夹不存在：{image_dir}")
        image_dir.mkdir(exist_ok=True)
        safe_print(f"[信息] 已创建 image 文件夹")
    
    return True


# === Information.md 清理功能（独立函数，便于单独调用或注释） ===
def clear_information_md(interactive: bool = True):
    """
    清空 Information.md 文件（先备份到 history）。

    Args:
        interactive: 是否需要用户确认
    """
    information_file = BASE_DIR / "Information.md"
    history_dir = BASE_DIR / "history"

    if not information_file.exists():
        print(f"[警告] Information.md 文件不存在，跳过清空操作")
        return True

    # 检查是否为空
    content = information_file.read_text(encoding='utf-8')
    if not content.strip():
        print(f"[信息] Information.md 已经为空，无需清空")
        return True

    # 交互式确认
    if interactive:
        print(f"\n{'=' * 60}")
        print("⚠️  即将清空 Information.md 文件")
        print(f"  文件路径: {information_file}")
        preview_lines = content[:200].split('\n')[:10]
        for line in preview_lines:
            print(f"    {line}")
        print(f"{'=' * 60}")

        while True:
            confirm = input("\n❓ 是否确认清空 Information.md 文件? (y/n): ").strip().lower()
            if confirm in ['y', 'yes', '是']:
                break
            elif confirm in ['n', 'no', '否']:
                print("[取消] 已取消清空操作")
                return True
            else:
                print("  ⚠️  请输入 y (确认) 或 n (取消)")

    # 备份到 history
    try:
        history_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = history_dir / f"Information_{timestamp}.md"
        shutil.copy2(str(information_file), str(backup_file))
        print(f"[信息] 已备份 Information.md 到: {backup_file}")

        information_file.write_text('', encoding='utf-8')
        print(f"[成功] Information.md 已清空")
    except Exception as e:
        print(f"[失败] 清空 Information.md 失败: {e}")
        return False

    return True


if __name__ == "__main__":
    import sys

    # 检查是否为非交互模式（被 subprocess 调用时传入 --no-confirm）
    no_confirm = '--no-confirm' in sys.argv

    try:
        move_and_clear_images(interactive=not no_confirm)
    except Exception as e:
        safe_print(f"[错误] 程序执行失败：{e}")
