from scrapling.fetchers import StealthyFetcher
import os, json
from pathlib import Path
STORAGE = os.environ.get('IDP_STORAGE_STATE', '')
COOKIES=None
UA=None
if STORAGE and Path(STORAGE).exists():
    data=json.loads(Path(STORAGE).read_text(encoding='utf-8'))
    COOKIES=data.get('cookies')
    UA=(data.get('_meta') or {}).get('user_agent')
print('using cookies:', bool(COOKIES), 'ua:', UA)
url='https://www.loc.gov/search/?in=&q=china+buddhist&new=true'
resp=StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=120000, wait=2000, cookies=COOKIES, useragent=UA)
body = getattr(resp,'body', None) or getattr(resp,'content', None) or b''
text = body.decode('utf-8',errors='replace') if isinstance(body, (bytes,bytearray)) else str(body)
Path('sample_debug_loc.html').write_text(text,encoding='utf-8')
print('wrote sample_debug_loc.html, status=', getattr(resp,'status',None))
