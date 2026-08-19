from scrapling.fetchers import StealthyFetcher
url='https://www.loc.gov/search/?in=&q=china+buddhist&new=true'
print('Fetching via StealthyFetcher:', url)
r = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=60000, solve_cloudflare=True)
open('sample_loc_search_stealth.html','wb').write(r.body if hasattr(r,'body') else r.content)
print('Wrote sample_loc_search_stealth.html, status=', getattr(r,'status',None))
