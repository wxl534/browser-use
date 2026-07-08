"""Simulate concurrent detail-overview load to estimate throughput and observe cache behavior.

Usage:
  python -m pip install httpx bs4
  python scripts\simulate_overview_load.py --concurrency 20 --requests 200

This script exercises fetch_detail_overview_via_http (async) against a sample URL (default https://example.com)
and prints metrics from adapters.detail_overview.get_overview_metrics().
"""
import argparse
import asyncio
import time

sys_req = []

async def worker(q, func, sem):
    while True:
        item = await q.get()
        if item is None:
            q.task_done()
            break
        url, cfg = item
        try:
            await func(url, cfg)
        except Exception:
            pass
        q.task_done()

async def run_simulation(url, concurrency, total_requests):
    from adapters.detail_overview import fetch_detail_overview_via_http, get_overview_metrics
    import adapters.detail_overview as dov

    cfg = {'mode': 'sections', 'section_selector': '.detaildropdown__section', 'label_selector': 'h4'}

    q = asyncio.Queue()
    for i in range(total_requests):
        await q.put((url, cfg))
    for _ in range(concurrency):
        await q.put(None)

    tasks = [asyncio.create_task(worker(q, fetch_detail_overview_via_http, None)) for _ in range(concurrency)]
    start = time.time()
    await q.join()
    elapsed = time.time() - start
    # give caches a moment
    await asyncio.sleep(0.1)
    metrics = get_overview_metrics()
    print('\nSimulation summary:')
    print(f'  concurrency={concurrency} total_requests={total_requests} elapsed={elapsed:.2f}s')
    print('  metrics:')
    for k, v in metrics.items():
        print(f'    {k}: {v}')
    if elapsed > 0:
        print(f'  approx RPS: {total_requests/elapsed:.1f}')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--concurrency', type=int, default=10)
    parser.add_argument('--requests', type=int, default=100)
    parser.add_argument('--url', type=str, default='https://example.com')
    args = parser.parse_args()
    asyncio.run(run_simulation(args.url, args.concurrency, args.requests))
