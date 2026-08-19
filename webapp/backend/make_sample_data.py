"""生成临时样例数据,用于本地开发/验证 Web 控制台.

注意:写入真实 ImagesCache 目录,仅供开发预览;一次真实的 `--new-run`
会归档并清空它.需要时手动重新运行本脚本即可.相对路径推导,无硬编码.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

BASE_DIR = Path(__file__).resolve().parents[2]
CACHE_DIR = BASE_DIR / 'Images' / 'ImagesCache'
CACHE_DIR.mkdir(parents=True, exist_ok=True)

COLORS = ['#8d6e63', '#5c6bc0', '#26a69a', '#ef5350', '#ab47bc', '#ffa726']
TITLES = [
    'china_buddhist_001_敦煌莫高窟壁画_图1',
    'china_buddhist_002_金刚经写本残卷_图1',
    'china_buddhist_003_千手观音绢画_图1',
    'china_buddhist_004_藏经洞文书_图1',
    'china_buddhist_005_飞天彩塑_图1',
    'china_buddhist_006_法华经变相_图1',
]

records = []
now = datetime.now(timezone.utc).isoformat()
for i, (title, color) in enumerate(zip(TITLES, COLORS), start=1):
    file_name = f'{title}_{i:08x}.png'
    img = Image.new('RGB', (640, 480), color)
    draw = ImageDraw.Draw(img)
    draw.text((24, 24), f'#{i:03d}', fill='white')
    draw.text((24, 60), title, fill='white')
    img.save(CACHE_DIR / file_name)
    records.append({
        'sequence': i,
        'status': 'downloaded',
        'title': title,
        'collection_title': f'示例藏品 {i}',
        'page_url': f'https://idp.bl.uk/collection/sample-{i}/',
        'image_url': f'https://data.idp.bl.uk/sample-{i}/full/max/0/default.jpg',
        'file_name': file_name,
        'evidence': '标题/说明中包含 china buddhist 相关语境',
        'metadata': f'年代：唐；地点：敦煌；分类：绘画；馆藏号：Or.{8210 + i}',
        'summary': f'第 {i} 件敦煌佛教相关藏品的示意图像。',
        'recorded_at': now,
        'downloaded_at': now,
        'source_site': 'idp.bl.uk',
    })

record_file = CACHE_DIR / 'image_record.jsonl'
with open(record_file, 'w', encoding='utf-8') as f:
    for r in records:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')

print(f'写入 {len(records)} 条样例记录到 {record_file}')

# 调用真实导入脚本建库,保证与生产路径一致.
importer = BASE_DIR / 'import_records_to_sqlite.py'
subprocess.run(
    [sys.executable, str(importer),
     '--record-file', str(record_file),
     '--image-dir', str(CACHE_DIR),
     '--db-file', str(CACHE_DIR / 'image_catalog.sqlite3')],
    cwd=str(BASE_DIR), check=True,
)
print('已导入 SQLite.')
