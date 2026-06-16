#!/usr/bin/env python3
"""
Hermes cron wrapper for tracking check.
- Silently exits if no changes (no notification)
- Reports status updates only when they happen
- Detects OCS cookie expiry as a separate signal
- Does NOT suppress 流通王 updates when OCS is broken
- Distinguishes real system errors from "no changes"
"""
import sys, json, subprocess, os, time

script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracker.py")

# Phase 1: Run check
result = subprocess.run(
    [sys.executable, script, "check"],
    capture_output=True, text=True, timeout=120
)

output = result.stdout.strip()
errors = result.stderr.strip()
exit_code = result.returncode

# Phase 2: Analyze output

# 2a: System-level crash — no meaningful output at all
if exit_code != 0 and "🔍" in output and "无变化" not in output and "→" not in output:
    print(f"⚠️ 快递追踪系统错误 (exit {exit_code})")
    if errors:
        print(f"{errors[:300]}")
    print("系统将在下次定时检查时重试。")
    sys.exit(0)

# 2b: CDP/connection crash — printed "🔍" then crashed
if exit_code != 0 and ("CDP" in errors or "connect" in errors.lower() or "timeout" in errors.lower()):
    print(f"⚠️ 快递追踪浏览器连接异常，下次检查自动重试")
    sys.exit(0)

# 2c: OCS Cookie expired — signal the cron agent
if "OCS Cookie 过期" in output or "cookie_expired" in output.lower():
    print("🔄 OCS Cookie 过期，需要续期")
    # Also include any 流通王 updates if present
    if "**" in output and ("流通王" in output or "ScoreJP" in output):
        # Strip the OCS expiry message, pass through 流通王 updates
        lines = [l for l in output.split('\n') if "OCS" not in l and "cookie_expired" not in l.lower()]
        if lines:
            print('\n'.join(lines))
    sys.exit(0)

# 2d: Real status changes (contains markdown bold markers)
if "**" in output and ("→" in output or "追踪" in output):
    print(output)
    sys.exit(0)

# 2e: Normal "no changes" or just info text — silent exit
sys.exit(0)
