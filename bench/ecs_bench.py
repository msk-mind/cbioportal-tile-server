"""
ECS range-request latency benchmark.

Run before doing any app-level testing to establish whether ECS itself
is fast enough to be the backing store.

Usage:
    pip install boto3
    python bench/ecs_bench.py \
        --endpoint https://your-ecs.mskcc.org \
        --bucket slides \
        --key path/to/slide.svs \
        --concurrency 50 \
        --requests 500
"""

import argparse
import concurrent.futures
import os
import statistics
import time

import boto3
from botocore.config import Config


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", default=os.environ.get("ECS_ENDPOINT_URL", ""))
    p.add_argument("--bucket", default=os.environ.get("S3_BUCKET", "slides"))
    p.add_argument("--key", required=True, help="S3 key of a real SVS file")
    p.add_argument("--access-key", default=os.environ.get("AWS_ACCESS_KEY_ID"))
    p.add_argument("--secret-key", default=os.environ.get("AWS_SECRET_ACCESS_KEY"))
    p.add_argument("--concurrency", type=int, default=50)
    p.add_argument("--requests", type=int, default=500)
    return p.parse_args()


CHUNK = 262_144  # 256 KB per request — representative tile read


def build_ranges(file_size: int) -> list:
    """Generate representative read offsets spread across the file."""
    if file_size <= CHUNK:
        return [(0, file_size - 1)]
    offsets = [
        0,                           # header / TIFF directory
        min(65_536, file_size - 1),  # near-start
        file_size // 4,
        file_size // 2,
        file_size * 3 // 4,
        max(0, file_size - CHUNK),   # near end
    ]
    return [(o, min(o + CHUNK - 1, file_size - 1)) for o in offsets]


def timed_range_get(s3, bucket, key, byte_range):
    start = time.perf_counter()
    s3.get_object(
        Bucket=bucket,
        Key=key,
        Range=f"bytes={byte_range[0]}-{byte_range[1]}",
    )["Body"].read()
    return (time.perf_counter() - start) * 1000  # ms


def main():
    args = parse_args()

    s3 = boto3.client(
        "s3",
        endpoint_url=args.endpoint or None,
        aws_access_key_id=args.access_key,
        aws_secret_access_key=args.secret_key,
        config=Config(
            max_pool_connections=args.concurrency + 10,
            retries={"max_attempts": 1},
        ),
    )

    # Get object size then build valid ranges
    head = s3.head_object(Bucket=args.bucket, Key=args.key)
    file_size = head["ContentLength"]
    print(f"Object size: {file_size / 1_048_576:.1f} MB\n")

    RANGES = build_ranges(file_size)

    import random
    work = [random.choice(RANGES) for _ in range(args.requests)]

    print(f"Firing {args.requests} range requests at concurrency={args.concurrency}...")
    print(f"Endpoint: {args.endpoint or 'AWS default'}")
    print(f"Object:   s3://{args.bucket}/{args.key}\n")

    t_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        latencies = list(ex.map(lambda r: timed_range_get(s3, args.bucket, args.key, r), work))
    elapsed = time.perf_counter() - t_start

    s = sorted(latencies)
    n = len(s)
    print(f"Results ({n} requests in {elapsed:.1f}s):")
    print(f"  Throughput : {n / elapsed:.1f} req/s")
    print(f"  p50        : {statistics.median(s):.1f} ms")
    print(f"  p75        : {s[int(n * 0.75)]:.1f} ms")
    print(f"  p95        : {s[int(n * 0.95)]:.1f} ms")
    print(f"  p99        : {s[int(n * 0.99)]:.1f} ms")
    print(f"  max        : {s[-1]:.1f} ms")
    print()
    print("Interpretation:")
    if s[int(n * 0.95)] < 50:
        print("  ✅ p95 < 50ms — direct ECS reads should work well")
    elif s[int(n * 0.95)] < 150:
        print("  ⚠️  p95 50–150ms — consider fsspec BlockCache on NVMe")
    else:
        print("  ❌ p95 > 150ms — stage slides to GPFS for acceptable tile latency")


if __name__ == "__main__":
    main()
