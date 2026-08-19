from scrapling.fetchers import StealthyFetcher

def page_action(page):
    try:
        selectors = ["input[name=q]", "input[type=search]", "input#search", "input[aria-label=\"Search\"]"]
        found=None
        for s in selectors:
            try:
                el=page.locator(s)
                if el.count()>0:
                    found=el
                    break
            except Exception:
                pass
        if not found:
            try:
                page.locator("input").first.fill('china buddhist')
                page.keyboard.press('Enter')
                return
            except Exception:
                return
        found.fill('china buddhist')
        page.keyboard.press('Enter')
    except Exception:
        return

print('Running StealthyFetcher with page_action on homepage')
r = StealthyFetcher.fetch('https://www.loc.gov/', headless=True, network_idle=True, timeout=60000, page_action=page_action, solve_cloudflare=True)
open('sample_loc_search_from_home.html','wb').write(r.body if hasattr(r,'body') else r.content)
print('Wrote sample_loc_search_from_home.html, status=', getattr(r,'status',None))
