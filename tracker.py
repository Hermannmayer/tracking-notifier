#!/usr/bin/env python3
"""
Hermes 快递追踪系统 — 浏览器自动化版
支持 流通王(ScoreJP) / OCS

依赖: pip install websocket-client beautifulsoup4 lxml
使用 CloakBrowser CDP 做浏览器自动化查物流

用法:
  tracker.py add <物流商:单号> [备注]   — 添加追踪
  tracker.py list                       — 查看所有
  tracker.py check                      — 检查所有状态
  tracker.py remove <id>                — 移除
  tracker.py check-one <物流商:单号>    — 快速查单个
  tracker.py cookie <物流商> <cookie>   — 更新 cookie
"""

import sqlite3
import json
import os
import re
import sys
import time
import urllib.request
import base64
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv as _dotenv

# Load .env from project root (next to this script)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    _dotenv(_env_path)
else:
    # fallback: try current directory
    _dotenv()
_ocs_username = os.environ.get("OCS_USERNAME", "")
_ocs_password = os.environ.get("OCS_PASSWORD", "")

# ─── 可配置路径 ────────────────────
_data_dir = os.environ.get(
    "TRACKER_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
)
os.makedirs(_data_dir, exist_ok=True)

DB_PATH = os.environ.get("TRACKER_DB_PATH", os.path.join(_data_dir, "tracker.db"))
COOKIE_PATH = os.environ.get("TRACKER_COOKIE_PATH", os.path.join(_data_dir, "ocs_cookies.json"))
CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")
CAPTCHA_PATH = os.environ.get("CAPTCHA_PATH", "/tmp/ocs_captcha.png")

# ─── 自定义异常 ────────────────────

class CookieExpiredError(Exception):
    """OCS Cookie 过期信号，不写入 DB 状态"""
    pass

try:
    from websocket import create_connection
except ImportError:
    print("❌ 需要安装 websocket-client: pip install websocket-client")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

BJT = timezone(timedelta(hours=8))

STATUS_WEIGHT = {"pending": 0, "in_transit": 1, "customs": 2,
                 "out_for_delivery": 3, "delivered": 4, "exception": -1}
STATUS_ICONS = {"pending": "📦", "in_transit": "🚚", "customs": "🛃",
                "out_for_delivery": "📮", "delivered": "✅", "exception": "⚠️"}
STATUS_NAMES = {"pending": "待揽收", "in_transit": "运输中", "customs": "清关中",
                "out_for_delivery": "派送中", "delivered": "已签收", "exception": "异常"}

# ─── 数据库 ───────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            courier TEXT NOT NULL,
            tracking_number TEXT NOT NULL,
            note TEXT DEFAULT '',
            carrier_name TEXT DEFAULT '',
            latest_status_text TEXT DEFAULT '',
            latest_detail TEXT DEFAULT '',
            latest_location TEXT DEFAULT '',
            latest_event_time TEXT DEFAULT '',
            status_category TEXT DEFAULT 'pending',
            added_at TEXT DEFAULT (datetime('now', '+8 hours')),
            last_check_at TEXT,
            is_active INTEGER DEFAULT 1,
            UNIQUE(courier, tracking_number)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS check_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id INTEGER,
            status_text TEXT,
            status_category TEXT,
            detail TEXT,
            location TEXT,
            event_time TEXT,
            checked_at TEXT DEFAULT (datetime('now', '+8 hours'))
        )
    """)
    return conn


def cleanup_old_logs(max_per_package=100):
    """清理每个包裹多余的 check_log 记录，只保留最新的 max_per_package 条"""
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT package_id FROM check_log").fetchall()
    for row in rows:
        pid = row["package_id"]
        # 删除最早的多余记录
        conn.execute("""
            DELETE FROM check_log WHERE id IN (
                SELECT id FROM check_log WHERE package_id=?
                ORDER BY id ASC
                LIMIT MAX(0, (SELECT COUNT(*) FROM check_log WHERE package_id=?) - ?)
            )
        """, (pid, pid, max_per_package))
    conn.execute("PRAGMA optimize")
    conn.commit()
    conn.close()
    return True


def ensure_indexes():
    conn = get_db()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_packages_active ON packages(is_active)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_check_log_pkg ON check_log(package_id)")
    conn.commit()
    conn.close()

# ─── 读取 Cookie ──────────────────

def load_ocs_cookie():
    """从 JSON 文件读取 OCS session cookie"""
    try:
        with open(COOKIE_PATH) as f:
            data = json.load(f)
        return data.get("selfservice.ocschina.com", {}).get("ASP.NET_SessionId", "")
    except:
        return ""

def save_ocs_cookie(session_id):
    """保存 OCS session cookie"""
    data = {}
    try:
        with open(COOKIE_PATH) as f:
            data = json.load(f)
    except:
        pass
    if "selfservice.ocschina.com" not in data:
        data["selfservice.ocschina.com"] = {}
    data["selfservice.ocschina.com"]["ASP.NET_SessionId"] = session_id
    with open(COOKIE_PATH, 'w') as f:
        json.dump(data, f, indent=2)
    return True

# ─── OCS 自动登录 ──────────────────

def get_ocs_credentials():
    """从 .env 读取 OCS 账号密码"""
    return _ocs_username, _ocs_password

def ocs_login():
    """使用 CloakBrowser CDP 自动登录 OCS，返回 True/False
    
    流程：清 Cookie → 打开登录页 → 填账号密码 → vision 看验证码
    → 输入验证码 → 点登录 → 保存 Cookie
    """
    ws, target_id = cdp_create_page()
    try:
        cdp_send(ws, "Page.enable")
        cdp_send(ws, "Network.enable")
        cdp_send(ws, "Network.clearBrowserCookies")
        
        username, password = get_ocs_credentials()
        if not username or not password:
            print("⚠️ OCS 账号密码未配置，请在 .env 中设置 OCS_USERNAME 和 OCS_PASSWORD")
            return False
        
        # 打开登录页
        cdp_navigate(ws, "https://selfservice.ocschina.com/main.aspx")
        time.sleep(2)
        
        # 填账号密码
        cdp_js(ws, f"document.getElementById('account').value = '{username}'")
        time.sleep(0.2)
        cdp_js(ws, f"document.getElementById('Password').value = '{password}'")
        time.sleep(0.2)
        
        # 截验证码图
        screenshot = cdp_send(ws, "Page.captureScreenshot",
                              {"format": "png", "fromSurface": True}, timeout_sec=10)
        if not screenshot or not screenshot.get("data"):
            print("⚠️ 无法截取验证码图片")
            return False
        
        img_data = base64.b64decode(screenshot["data"])
        with open(CAPTCHA_PATH, "wb") as f:
            f.write(img_data)
        
        # 检测验证码 - 通过查看 captcha image 的 src data URI
        captcha_src = cdp_js(ws, """
        (function() {
            var imgs = document.querySelectorAll('img');
            for (var i = 0; i < imgs.length; i++) {
                var src = imgs[i].getAttribute('src') || '';
                if (src.startsWith('data:image')) return src;
            }
            return '';
        })();
        """)
        
        captcha_text = ""
        if captcha_src and captcha_src.startswith("data:image"):
            # 解码 captcha 图片 base64
            try:
                b64data = captcha_src.split(",")[1]
                cap_img = base64.b64decode(b64data)
                cap_path = CAPTCHA_PATH.replace(".png", "_raw.png")
                with open(cap_path, "wb") as f:
                    f.write(cap_img)
                # 调用 vision model 识别（通过 Hermes vision 能力）
                # 实际上无法直接调 vision，返回路径让调用方处理
                captcha_text = "NEED_VISION:" + CAPTCHA_PATH
            except:
                pass
        
        if captcha_text.startswith("NEED_VISION:"):
            # 这部分在脚本外处理 - 外部调用 ocs_login-vision
            print(f"CAPTCHA_NEEDED:{CAPTCHA_PATH}")
            print(f"如需继续请执行: python3 tracker.py login-vision <验证码文字>")
            return False
        
        # 如果上面没检测到 data URI 的验证码，用截图路径
        if not captcha_text:
            print(f"CAPTCHA_NEEDED:{CAPTCHA_PATH}")
            print(f"请查看 {CAPTCHA_PATH} 并执行: python3 tracker.py login-vision <验证码文字>")
            return False
            
    finally:
        try:
            cdp_send(ws, "Target.closeTarget", {"targetId": target_id}, timeout_sec=3)
        except:
            pass
        try:
            ws.close()
        except:
            pass

def ocs_login_with_captcha(captcha_code):
    """给定验证码，完成 OCS 登录并保存 cookie"""
    ws, target_id = cdp_create_page()
    try:
        cdp_send(ws, "Page.enable")
        cdp_send(ws, "Network.enable")
        cdp_send(ws, "Network.clearBrowserCookies")
        
        username, password = get_ocs_credentials()
        cdp_navigate(ws, "https://selfservice.ocschina.com/main.aspx")
        time.sleep(2)
        
        cdp_js(ws, f"document.getElementById('account').value = '{username}'")
        time.sleep(0.2)
        cdp_js(ws, f"document.getElementById('Password').value = '{password}'")
        time.sleep(0.2)
        cdp_js(ws, f"document.getElementById('Vcode').value = '{captcha_code}'")
        time.sleep(0.3)
        
        # 点登录
        cdp_js(ws, "document.getElementById('login').click();")
        time.sleep(3)
        
        # 检查登录结果
        page_text = cdp_js(ws, "document.body.innerText")
        
        if "欢迎" in page_text and "陈小姐" in page_text:
            # 登录成功，获取 cookie
            cookies = cdp_send(ws, "Network.getAllCookies", {}, timeout_sec=5)
            if cookies:
                for c in cookies.get("cookies", []):
                    if c["name"] == "ASP.NET_SessionId":
                        save_ocs_cookie(c["value"])
                        print(f"✅ OCS 登录成功！新 Cookie 已保存")
                        return True
            print("✅ OCS 登录成功但未获取到 Cookie")
            return True
        else:
            # 可能验证码错误
            print("❌ OCS 登录失败，验证码可能错误或已过期")
            return False
    finally:
        try:
            cdp_send(ws, "Target.closeTarget", {"targetId": target_id}, timeout_sec=3)
        except:
            pass
        try:
            ws.close()
        except:
            pass

def ocs_refresh_cookie():
    """尝试自动续 Cookie，需要人工输入验证码时返回提示"""
    cookie = load_ocs_cookie()
    if cookie:
        # 先试试现有 cookie 是否有效
        ws, target_id = cdp_create_page()
        try:
            cdp_send(ws, "Page.enable")
            cdp_send(ws, "Network.enable")
            cdp_send(ws, "Network.clearBrowserCookies")
            cdp_set_cookie(ws, "selfservice.ocschina.com", "ASP.NET_SessionId", cookie)
            cdp_navigate(ws, "https://selfservice.ocschina.com/main.aspx")
            time.sleep(2)
            page_text = cdp_js(ws, "document.body.innerText")
            if "欢迎" in page_text and "陈小姐" in page_text:
                print("✅ OCS Cookie 仍然有效")
                return True
        finally:
            try:
                cdp_send(ws, "Target.closeTarget", {"targetId": target_id}, timeout_sec=3)
            except: pass
            try:
                ws.close()
            except: pass
    
    # Cookie 过期，自动登录遇到验证码
    return ocs_login()

# ─── CDP ──────────────────────────

def cdp_create_page():
    """创建 CDP 页面并返回 (ws, target_id)，首次失败重试一次"""
    for attempt in range(2):
        try:
            req = urllib.request.Request(f"{CDP_URL}/json/new", method="PUT")
            resp = urllib.request.urlopen(req, timeout=5)
            page = json.loads(resp.read().decode())
            ws = create_connection(page["webSocketDebuggerUrl"], timeout=10)
            return ws, page["id"]
        except Exception as e:
            if attempt == 1:
                raise ConnectionError(f"CDP 页面创建失败 (2次重试): {e}")
            time.sleep(2)
    # unreachable
    raise ConnectionError("CDP 页面创建失败")

def cdp_send(ws, method, params=None, timeout_sec=10):
    if params is None: params = {}
    msg_id = int(time.time() * 1000) % 100000
    ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        ws.settimeout(min(5.0, deadline - time.time()))
        try:
            r = json.loads(ws.recv())
        except:
            break
        if r.get("id") == msg_id:
            if "error" in r:
                raise Exception(f"CDP error: {r['error']}")
            return r.get("result")
    raise TimeoutError(f"CDP command timed out: {method}")

def cdp_js(ws, js, timeout_sec=10):
    """执行 JS 并返回字符串结果"""
    result = cdp_send(ws, "Runtime.evaluate", {
        "expression": js, "returnByValue": True
    }, timeout_sec=timeout_sec)
    return (result or {}).get("result", {}).get("value", "")

def cdp_get_text(ws):
    """获取页面纯文本"""
    return cdp_js(ws, "document.body.innerText")

def cdp_navigate(ws, url):
    """导航到 URL（带等待）"""
    cdp_send(ws, "Page.enable")
    cdp_send(ws, "Page.navigate", {"url": url})
    time.sleep(2)

def cdp_click_element(ws, selector):
    """通过 CSS 选择器点击元素"""
    info = cdp_js(ws, f"""
    (function() {{
        var el = document.querySelector('{selector}');
        if (!el) return JSON.stringify({{found: false}});
        var r = el.getBoundingClientRect();
        return JSON.stringify({{found:true, x:r.x+r.width/2, y:r.y+r.height/2}});
    }})()
    """)
    info_d = json.loads(info)
    if not info_d.get("found"):
        return False
    x, y = info_d["x"], info_d["y"]
    cdp_send(ws, "Input.dispatchMouseEvent",
             {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    cdp_send(ws, "Input.dispatchMouseEvent",
             {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
    return True

def cdp_type(ws, selector, text):
    safe_text = json.dumps(text)
    safe_sel = json.dumps(selector)
    cdp_js(ws, f"""
    var el = document.querySelector({safe_sel});
    if (el) {{ el.value = ''; el.value = {safe_text}; el.dispatchEvent(new Event('input', {{bubbles:true}})); }}
    """)

def cdp_set_cookie(ws, domain, name, value):
    """设置浏览器 Cookie"""
    cdp_send(ws, "Network.setCookie", {
        "domain": domain,
        "name": name,
        "value": value,
        "path": "/",
        "httpOnly": True,
        "secure": False,
        "session": True
    })

def cdp_cleanup(ws, target_id):
    """关闭页面和 WebSocket"""
    try:
        cdp_send(ws, "Target.closeTarget", {"targetId": target_id}, timeout_sec=3)
    except:
        pass
    try:
        ws.close()
    except:
        pass

# ─── 物流查询 ─────────────────────

def check_scorejp(ws, tracking_number):
    """查流通王"""
    cdp_navigate(ws, "http://www.shuka.scorejp.com/SCJTrace/")
    
    # 填入单号
    cdp_type(ws, "#txtInvoiceNo", tracking_number)
    time.sleep(0.5)
    
    # 点击查询按钮
    clicked = cdp_click_element(ws, "#ibtnSeach")
    if not clicked:
        cdp_js(ws, "document.getElementById('ibtnSeach').click();")
    time.sleep(3)
    
    # 读取结果
    text = cdp_get_text(ws)
    
    # 解析表格
    if BeautifulSoup:
        html = cdp_js(ws, "document.documentElement.outerHTML")
        soup = BeautifulSoup(html, 'lxml')
        tables = soup.find_all('table')
        for tbl in tables:
            rows = tbl.find_all('tr')
            for row in rows:
                cells = row.find_all('td')
                if len(cells) >= 3 and tracking_number in cells[0].get_text():
                    status = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                    event_time = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                    return {
                        "status": status,
                        "event_time": event_time,
                        "detail": status,
                        "location": "",
                        "carrier_name": "流通王(ScoreJP)",
                    }
    
    # 正则后备解析
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    result = {"status": "", "event_time": "", "detail": "",
              "location": "", "carrier_name": "流通王(ScoreJP)"}
    for i, line in enumerate(lines):
        if tracking_number in line:
            m = re.search(r'\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}', line)
            if m:
                result["event_time"] = m.group()
            if i + 1 < len(lines):
                result["status"] = result["detail"] = lines[i + 1]
            break
    return result


def check_ocs(ws, tracking_number):
    """查 OCS — 使用已保存的 session cookie 访问首页进行查询"""
    cookie = load_ocs_cookie()
    if not cookie:
        raise CookieExpiredError("OCS cookie file empty, login required")
    
    # 清除 cookies 确保使用我们的 session
    cdp_send(ws, "Network.enable")
    cdp_send(ws, "Network.clearBrowserCookies")
    
    # 设置 OCS session cookie
    cdp_set_cookie(ws, "selfservice.ocschina.com", "ASP.NET_SessionId", cookie)
    
    # 导航到首页（搜索功能在 main.aspx 上，不是在 WaybillQuery.aspx）
    cdp_navigate(ws, "https://selfservice.ocschina.com/main.aspx")
    time.sleep(2)
    
    page_text = cdp_get_text(ws)
    
    # 检查是否已登录 — 过期则直接抛异常，不污染 DB 状态
    if "用户登录" in page_text or "登录" in page_text[:500]:
        raise CookieExpiredError("OCS session cookie expired, login required")
    
    # 输入单号
    cdp_type(ws, "#CWB_NOLText", tracking_number)
    time.sleep(0.5)
    
    # 点击查询按钮
    cdp_js(ws, "document.getElementById('btn_query').click();")
    time.sleep(3)
    
    text = cdp_get_text(ws)
    
    # 解析结果
    result = {"status": "", "event_time": "", "detail": "",
              "location": "", "carrier_name": "OCS"}
    
    # 从页面文本中提取追踪结果
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    
    # 找 "运单号：XXXXXXXX" 所在的位置
    found_number = False
    status_found = ""
    detail_lines = []
    
    for i, line in enumerate(lines):
        if f"运单号：{tracking_number}" in line or f"运单号:{tracking_number}" in line:
            found_number = True
            detail_lines.append(line)
            continue
        if found_number:
            # 收集后续所有信息行（遇到空段落或下一个关键标题时停止）
            if line.startswith("运单号：") or line.startswith("运单号:"):
                # 另一个运单，停止
                break
            if line in ("自动反馈区", "快件跟踪查询"):
                break
            detail_lines.append(line)
    
    if found_number:
        result["detail"] = '\n'.join(detail_lines)
        for line in detail_lines:
            if "最新状态：" in line:
                result["status"] = line.replace("最新状态：", "").replace("最新状态:", "").strip()
            elif "始发站点：" in line or "始发站点:" in line:
                result["location"] = line.replace("始发站点：", "").replace("始发站点:", "").strip()
            # 匹配时间行如 "2026/6/9 13:49,星期二"
            m = re.search(r'(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})', line)
            if m and not result["event_time"]:
                result["event_time"] = m.group(1)
        
        return result
    
    # 检查是否无结果
    if "0条" in text and ("共0条" in text or "0 件"):
        result["status"] = "未查到物流信息"
        return result
    
    # 正则后备
    for line in lines:
        if tracking_number in line:
            m = re.search(r'(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})', line)
            if m:
                result["event_time"] = m.group(1)
            result["status"] = line
            result["detail"] = text[:1000]
            return result
    
    result["status"] = "未查到物流信息"
    return result


def check_single(courier, tracking_number):
    """查询单个单号"""
    ws, target_id = cdp_create_page()
    try:
        if courier == "流通王":
            return check_scorejp(ws, tracking_number)
        elif courier == "OCS":
            return check_ocs(ws, tracking_number)
        return check_scorejp(ws, tracking_number)
    finally:
        cdp_cleanup(ws, target_id)


# ─── 状态工具 ─────────────────────

def classify_status(text):
    if not text: return "pending"
    text = text.lower()
    if any(w in text for w in ["delivered","signed","received","签收","配達完了","届け","お届け済み","配送完了","派送并签收"]):
        return "delivered"
    if any(w in text for w in ["out for delivery","delivering","派送中","配達中","配送中"]):
        return "out_for_delivery"
    if any(w in text for w in ["customs","clearance","清关","通関","税関"]):
        return "customs"
    if any(w in text for w in ["exception","return","reject","异常","返送","拒否","エラー"]):
        return "exception"
    if any(w in text for w in ["transit","transport","arrived","到着","発送","出荷","準備完了","通過","出発","仕分け"]):
        return "in_transit"
    if any(w in text for w in ["pickup","collected","pending","待揽收","荷物受付","受付"]):
        return "pending"
    if any(w in text for w in ["expired","过期","期限切れ"]):
        return "expired"
    if text and text not in ("未查到物流信息", "⚠️ Cookie 已过期"):
        return "in_transit"
    return "pending"


def normalize_courier(name):
    m = {"ocs": "OCS", "流通王": "流通王", "scorejp": "流通王", "score": "流通王"}
    return m.get(name.strip().lower(), name.upper())


def extract_tracking(text):
    m = re.match(r'(OCS|流通王|ScoreJP|scorejp)[：:]\s*([\w-]+)', text, re.I)
    if m:
        return normalize_courier(m.group(1)), m.group(2)
    return None, text.strip()


# ─── 数据库操作 ───────────────────

def add_package(courier, number, note=""):
    conn = get_db()
    courier = normalize_courier(courier)
    try:
        conn.execute("INSERT OR IGNORE INTO packages (courier, tracking_number, note) VALUES (?,?,?)",
                     (courier, number, note))
        conn.commit()
        row = conn.execute("SELECT * FROM packages WHERE courier=? AND tracking_number=?",
                           (courier, number)).fetchone()
        return {"status": "ok", "id": row["id"], "courier": row["courier"],
                "tracking": row["tracking_number"], "note": row["note"]}
    finally:
        conn.close()


def list_packages(active=True):
    conn = get_db()
    q = "SELECT * FROM packages"
    if active: q += " WHERE is_active=1"
    q += " ORDER BY added_at DESC"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_package(pkg_id):
    conn = get_db()
    conn.execute("UPDATE packages SET is_active=0 WHERE id=?", (pkg_id,))
    conn.commit()
    conn.close()


def check_all():
    pkgs = list_packages()
    if not pkgs: return []
    
    # 确保索引存在，清理历史日志
    ensure_indexes()
    cleanup_old_logs()
    
    # 按快递商分组，每组用独立 CDP 页面避免 cookie 污染
    courier_groups = {}
    for p in pkgs:
        courier_groups.setdefault(p["courier"], []).append(p)
    
    updates = []
    
    for courier, group in courier_groups.items():
        ws, target_id = cdp_create_page()
        try:
            for pkg in group:
                result = None
                try:
                    if courier == "流通王":
                        result = check_scorejp(ws, pkg["tracking_number"])
                    elif courier == "OCS":
                        result = check_ocs(ws, pkg["tracking_number"])
                except CookieExpiredError:
                    # Cookie 过期 — 不写入 DB，记录到 updates 供 cron agent 处理
                    updates.append({"cookie_expired": True, "courier": "OCS",
                        "tracking": pkg["tracking_number"],
                        "msg": "OCS Cookie 已过期，等待自动续期"})
                    continue
                except Exception as e:
                    conn = get_db()
                    conn.execute("INSERT INTO check_log (package_id, status_text, status_category, detail) VALUES (?,?,?,?)",
                                 (pkg["id"], f"查询异常: {str(e)[:50]}", "pending", ""))
                    conn.commit()
                    conn.close()
                    continue
                
                if result and result.get("status"):
                    conn = get_db()
                    pid = pkg["id"]
                    prev = pkg["status_category"]
                    st = result.get("status", "")
                    cat = classify_status(st)
                    
                    conn.execute("INSERT INTO check_log (package_id, status_text, status_category, detail, location, event_time) VALUES (?,?,?,?,?,?)",
                                 (pid, st, cat, result.get("detail",""), result.get("location",""), result.get("event_time","")))
                    conn.execute("""UPDATE packages SET latest_status_text=?, latest_detail=?, latest_location=?,
                        latest_event_time=?, status_category=?, last_check_at=datetime('now','+8 hours'),
                        carrier_name=COALESCE(NULLIF(carrier_name,''),?)
                        WHERE id=?""",
                        (st, result.get("detail",""), result.get("location",""), result.get("event_time",""),
                         cat, result.get("carrier_name",""), pid))
                    
                    # === 已签收自动归档逻辑 ===
                    # 场景A：刚变成 delivered → 通知一次，立即归档
                    # 场景B：之前就是 delivered → 直接归档（不发通知）
                    if cat == "delivered":
                        conn.execute("UPDATE packages SET is_active=0 WHERE id=?", (pid,))
                        if prev != "delivered":
                            # 刚签收，发通知
                            updates.append({"id": pid, "courier": pkg["courier"],
                                "tracking": pkg["tracking_number"], "note": pkg["note"],
                                "old_cat": prev, "new_cat": cat, "status_text": st,
                                "detail": result.get("detail",""), "event_time": result.get("event_time","")})
                        # 已经是 delivered 的静默归档，不发通知
                    
                    else:
                        # 非签收状态，按原有逻辑判断是否通知
                        pw = STATUS_WEIGHT.get(prev, 0)
                        nw = STATUS_WEIGHT.get(cat, 0)
                        if cat != prev and (nw > pw or cat in ("exception",)):
                            updates.append({"id": pid, "courier": pkg["courier"],
                                "tracking": pkg["tracking_number"], "note": pkg["note"],
                                "old_cat": prev, "new_cat": cat, "status_text": st,
                                "detail": result.get("detail",""), "event_time": result.get("event_time","")})
                    
                    conn.commit()
                    conn.close()
        finally:
            cdp_cleanup(ws, target_id)
    
    return updates


# ─── 格式化输出 ───────────────────

def fmt_update(u):
    if u.get("cookie_expired"):
        return f"🔄 **OCS Cookie 过期**\n单号: `{u['tracking']}`\n{u['msg']}\n需重新登录续期 Cookie"
    old = STATUS_NAMES.get(u["old_cat"], u["old_cat"])
    new = STATUS_NAMES.get(u["new_cat"], u["new_cat"])
    icon = STATUS_ICONS.get(u["new_cat"], "📦")
    msg = f"{icon} **{u['courier']} 追踪更新**\n"
    msg += f"单号: `{u['tracking']}`\n"
    if u["note"]: msg += f"备注: {u['note']}\n"
    msg += f"状态: {old} → **{new}**\n"
    if u["status_text"]: msg += f"详情: {u['status_text']}\n"
    if u["event_time"]: msg += f"时间: {u['event_time']}\n"
    return msg


# ─── CLI ──────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法:\n  tracker.py add <物流商:单号> [备注]\n  tracker.py list\n  tracker.py check\n  tracker.py check-one <物流商:单号>\n  tracker.py remove <id>\n  tracker.py cookie <物流商> <session_id>")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "add":
        if len(sys.argv) < 3:
            print("❌ 需要单号，格式: 物流商:单号"); sys.exit(1)
        courier, number = extract_tracking(sys.argv[2])
        if not courier:
            print("❌ 请指定物流商，格式: OCS:单号 或 流通王:单号"); sys.exit(1)
        note = sys.argv[3] if len(sys.argv) > 3 else ""
        r = add_package(courier, number, note)
        print(f"✅ 已添加追踪: [{r['courier']}] {r['tracking']} {r['note']}")

    elif cmd == "list":
        pkgs = list_packages()
        if not pkgs:
            print("📭 暂无追踪中的包裹")
        else:
            print(f"📋 共 {len(pkgs)} 个追踪包裹:\n")
            for p in pkgs:
                icon = STATUS_ICONS.get(p["status_category"], "📦")
                st = p["latest_status_text"] or "未查询"
                nt = f" | {p['note']}" if p["note"] else ""
                print(f"  [{p['id']}] {icon} {p['courier']} | {p['tracking_number']} | {st}{nt}")

    elif cmd == "check":
        print("🔍 正在检查所有包裹状态...")
        updates = check_all()
        if not updates:
            print("✅ 所有包裹状态无变化")
        else:
            for u in updates:
                print(fmt_update(u))
                print()

    elif cmd == "check-one":
        if len(sys.argv) < 3:
            print("❌ 需要单号"); sys.exit(1)
        courier, number = extract_tracking(sys.argv[2])
        if not courier:
            courier, number = "流通王", sys.argv[2]
        print(f"🔍 正在查询 [{courier}] {number}...")
        result = check_single(courier, number)
        if result and result.get("status"):
            cat = classify_status(result["status"])
            icon = STATUS_ICONS.get(cat, "📦")
            print(f"{icon} **{courier}** `{number}`")
            print(f"状态: **{STATUS_NAMES.get(cat, cat)}**")
            print(f"详情: {result['status']}")
            if result.get("event_time"):
                print(f"时间: {result['event_time']}")
        else:
            print("❌ 未查询到状态信息")

    elif cmd == "remove":
        if len(sys.argv) < 3:
            print("❌ 需要包裹ID"); sys.exit(1)
        remove_package(int(sys.argv[2]))
        print(f"✅ 已移除追踪 #{sys.argv[2]}")

    elif cmd == "login-captcha":
        """打开 OCS 登录页，填账号密码，截图验证码（特写）"""
        ws, target_id = cdp_create_page()
        try:
            cdp_send(ws, "Page.enable")
            cdp_send(ws, "Network.enable")
            cdp_send(ws, "Network.clearBrowserCookies")
            
            username, password = get_ocs_credentials()
            if not username or not password:
                print("⚠️ OCS 账号密码未配置")
                sys.exit(1)
            
            cdp_navigate(ws, "https://selfservice.ocschina.com/main.aspx")
            time.sleep(2)
            
            # 填账号密码
            cdp_type(ws, "#account", username)
            time.sleep(0.3)
            cdp_type(ws, "#Password", password)
            time.sleep(0.3)
            
            # 等待验证码图片加载
            time.sleep(1)
            
            # 方法1: 优先截验证码图片元素特写
            captcha_bbox = cdp_js(ws, """
            (function() {
                var img = document.querySelector('img[src*="data:image"], img.verifyimg, img#verifyImage, #VcodeImg');
                if (!img) {
                    // 试试找页面上的任意图片
                    var imgs = document.querySelectorAll('img');
                    for (var i = 0; i < imgs.length; i++) {
                        var src = imgs[i].getAttribute('src') || '';
                        if (src.indexOf('data:image') >= 0 || src.indexOf('captcha') >= 0 || src.indexOf('verify') >= 0 || src.indexOf('vcode') >= 0 || src.indexOf('checkcode') >= 0) {
                            img = imgs[i];
                            break;
                        }
                    }
                }
                if (!img) return JSON.stringify({found:false});
                var r = img.getBoundingClientRect();
                return JSON.stringify({found:true, x:r.x, y:r.y, w:r.width, h:r.height, pageX:r.x+window.scrollX, pageY:r.y+window.scrollY});
            })();
            """)
            
            bbox = json.loads(captcha_bbox) if captcha_bbox else {"found": False}
            
            if bbox.get("found"):
                # 特写验证码区域（加10px边距）
                clip = {
                    "x": max(0, bbox["x"] - 10),
                    "y": max(0, bbox["y"] - 10),
                    "width": bbox["w"] + 20,
                    "height": bbox["h"] + 20,
                    "scale": 2  # 2倍放大提高辨识度
                }
                screenshot = cdp_send(ws, "Page.captureScreenshot",
                                      {"format": "png", "fromSurface": True, "clip": clip}, timeout_sec=10)
                if screenshot and screenshot.get("data"):
                    img_data = base64.b64decode(screenshot["data"])
                    with open(CAPTCHA_PATH, "wb") as f:
                        f.write(img_data)
                    print(f"📸 验证码特写已保存: {CAPTCHA_PATH} (scale=2x)")
                    print(f"CAPTCHA_PATH:{CAPTCHA_PATH}")
                else:
                    print("⚠️ 无法截取验证码特写")
            else:
                # 方法2: 全屏截图
                screenshot = cdp_send(ws, "Page.captureScreenshot",
                                      {"format": "png", "fromSurface": True}, timeout_sec=10)
                if screenshot and screenshot.get("data"):
                    img_data = base64.b64decode(screenshot["data"])
                    with open(CAPTCHA_PATH, "wb") as f:
                        f.write(img_data)
                    print(f"📸 全屏截图已保存: {CAPTCHA_PATH}")
                    print(f"CAPTCHA_PATH:{CAPTCHA_PATH}")
                else:
                    print("⚠️ 无法截取验证码图片")
        finally:
            cdp_cleanup(ws, target_id)

    elif cmd == "login-vision":
        """外部调用：给验证码，登录 OCS"""
        if len(sys.argv) < 3:
            print("❌ 需要验证码，用法: tracker.py login-vision <验证码>")
            sys.exit(1)
        captcha = sys.argv[2].strip()
        ocs_login_with_captcha(captcha)

    elif cmd == "login-refresh":
        """刷新 OCS Cookie（遇到验证码时看截图后重试）"""
        ok = ocs_refresh_cookie()
        if not ok:
            print("❌ Cookie 刷新失败")

    elif cmd == "cookie":
        if len(sys.argv) < 3:
            print("❌ 需要物流商和 session_id"); sys.exit(1)
        courier = normalize_courier(sys.argv[2])
        if courier == "OCS" and len(sys.argv) >= 4:
            save_ocs_cookie(sys.argv[3])
            print(f"✅ OCS Cookie 已更新")
        else:
            print(f"❌ 不支持的物流商: {courier}")

    else:
        print(f"❌ 未知命令: {cmd}")
