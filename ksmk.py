import json, os, threading, time, collections, subprocess, sys, platform
from flask import Flask, request, jsonify
from instagrapi import Client

app = Flask(__name__)
DATA_FILE = "data.json"
data_lock = threading.Lock()

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f: return json.load(f)
    return {"accounts": {}}

def save_data(d):
    with open(DATA_FILE, "w") as f: json.dump(d, f, indent=2)

bot_threads = {}
bot_stop    = {}
bot_status  = {}
ig_clients  = {}
# Per-account log buffer (last 200 lines)
bot_logs    = {}

def log(acc_id, msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    if acc_id not in bot_logs:
        bot_logs[acc_id] = collections.deque(maxlen=200)
    bot_logs[acc_id].append(line)

def get_client(acc_id, session_id, proxy=None):
    if acc_id in ig_clients: return ig_clients[acc_id]
    cl = Client()
    if proxy:
        cl.set_proxy(proxy)
    cl.login_by_sessionid(session_id)
    ig_clients[acc_id] = cl
    return cl

def extract_thread_id(s):
    s = s.strip()
    if "instagram.com/direct/t/" in s:
        return s.rstrip("/").split("/")[-1]
    return s

def nc_rename(cl, thread_id, title):
    """Try all known instagrapi NC methods."""
    # Method 1 — newer instagrapi
    try:
        cl.direct_thread_update_title(thread_id, title)
        return True, None
    except Exception as e1: pass

    # Method 2 — raw API with correct fields
    try:
        cl.private_request(
            f"direct_v2/threads/{thread_id}/update_title/",
            data={
                "title": title,
                "_uuid": cl.uuid,
                "_uid": cl.user_id,
                "_csrftoken": cl.token,
            }
        )
        return True, None
    except Exception as e2: pass

    # Method 3 — older instagrapi object method
    try:
        thread = cl.direct_thread(thread_id)
        thread.update_title(title)
        return True, None
    except Exception as e3: pass

    # Method 4 — bloks endpoint (newer Instagram versions)
    try:
        cl.private_request(
            f"direct_v2/threads/{thread_id}/update_title/",
            data={
                "title": title,
                "_uuid": cl.uuid,
                "_uid": str(cl.user_id),
                "use_unified_inbox": "true",
            }
        )
        return True, None
    except Exception as e4:
        return False, str(e4)

def restart_browser():
    """Kill and relaunch Chrome/Chromium browser."""
    os_ = platform.system()
    try:
        if os_ == "Windows":
            subprocess.call(["taskkill", "/F", "/IM", "chrome.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            chrome_paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
            for p in chrome_paths:
                if os.path.exists(p):
                    subprocess.Popen([p])
                    break
        elif os_ == "Darwin":  # macOS
            subprocess.call(["pkill", "-f", "Google Chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            subprocess.Popen(["open", "-a", "Google Chrome"])
        else:  # Linux
            for proc in ["chrome", "chromium", "chromium-browser", "google-chrome"]:
                subprocess.call(["pkill", "-f", proc], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            for cmd in ["google-chrome", "chromium-browser", "chromium"]:
                try:
                    subprocess.Popen([cmd])
                    break
                except FileNotFoundError:
                    continue
        return True
    except Exception as e:
        return False

def get_thread_title(cl, thread_id):
    """Fetch current title of a thread."""
    try:
        thread = cl.direct_thread(int(thread_id))
        return (thread.thread_title or "").strip()
    except Exception:
        return None

def bot_worker(acc_id, acc, stop_event):
    import random
    session_id = acc["session_id"]
    proxy = acc.get("proxy", "").strip() or None
    groups   = [extract_thread_id(g) for g in acc.get("groups","").split("\n") if g.strip()]
    titles   = [t.strip() for t in acc.get("nc_titles","").split(",") if t.strip()]
    messages = [m.strip() for m in acc.get("messages","").split("---MSG---") if m.strip()]
    if not messages:
        single = acc.get("message","").strip()
        if single: messages = [single]

    # Delays
    grp_min   = float(acc.get("grp_delay_min", acc.get("delay", 2)))
    grp_max   = float(acc.get("grp_delay_max", grp_min))
    round_min = float(acc.get("round_delay_min", 2))
    round_max = float(acc.get("round_delay_max", round_min))
    browser_restart_every = int(acc.get("browser_restart_every", 0))  # 0 = disabled

    bot_logs[acc_id] = collections.deque(maxlen=200)
    total_gcs = len(groups)
    bot_status[acc_id] = {
        "running": True, "sent": 0, "failed": 0,
        "nc_done": 0, "nc_failed": 0, "round": 0,
        "gcs_done": 0, "total_gcs": total_gcs,
        "nc_skipped": 0,
        "last_action": "Logging in...", "started_at": time.time()
    }

    log(acc_id, "⚡ Starting bot...")
    log(acc_id, f"📋 Groups: {len(groups)} | Titles: {len(titles)} | Messages: {len(messages)}")
    log(acc_id, f"⏱ Grp delay: {grp_min}-{grp_max}s | Round delay: {round_min}-{round_max}s")

    try:
        cl = get_client(acc_id, session_id, proxy)
        log(acc_id, f"✅ Logged in successfully{' (proxy)' if proxy else ''}")
        bot_status[acc_id]["last_action"] = "Logged in ✓"
    except Exception as e:
        log(acc_id, f"❌ Login failed: {e}")
        bot_status[acc_id]["running"] = False
        bot_status[acc_id]["last_action"] = f"Login failed: {e}"
        return

    title_idx = 0
    msg_idx   = 0
    while not stop_event.is_set():
        log(acc_id, f"🔄 Round {bot_status[acc_id]['round']+1} starting...")
        for thread_id in groups:
            if stop_event.is_set(): break

            # Smart NC — check current title first, no API call if already matches
            if titles:
                t = titles[title_idx % len(titles)]
                bot_status[acc_id]["last_action"] = f"Checking NC → {thread_id}"
                try:
                    current_title = get_thread_title(cl, thread_id)
                    if current_title is not None and current_title.strip() == t.strip():
                        # Title already matches — skip, no panel open
                        log(acc_id, f"⏭ NC skip (already '{t}') → {thread_id}")
                        bot_status[acc_id]["nc_skipped"] += 1
                    else:
                        # Title doesn't match — open panel and rename
                        bot_status[acc_id]["last_action"] = f"NC → {t}"
                        ok, err = nc_rename(cl, int(thread_id), t)
                        if ok:
                            bot_status[acc_id]["nc_done"] += 1
                            log(acc_id, f"✅ NC done [{t}] → {thread_id}")
                        else:
                            bot_status[acc_id]["nc_failed"] += 1
                            log(acc_id, f"❌ NC failed → {thread_id}: {err}")
                except Exception as e:
                    bot_status[acc_id]["nc_failed"] += 1
                    log(acc_id, f"❌ NC error → {thread_id}: {e}")

            # Pick message (round-robin)
            message = messages[msg_idx % len(messages)] if messages else ""

            # Send message
            bot_status[acc_id]["last_action"] = f"Sending → {thread_id}"
            try:
                cl.direct_send(message, thread_ids=[int(thread_id)])
                bot_status[acc_id]["sent"] += 1
                log(acc_id, f"✅ Sent → {thread_id}")
            except Exception as e:
                bot_status[acc_id]["failed"] += 1
                err_str = str(e)
                if hasattr(e, 'response') and e.response is not None:
                    try:
                        resp_json = e.response.json()
                        ig_msg = resp_json.get('message') or resp_json.get('error_title') or resp_json.get('feedback_message') or err_str
                        err_str = f"{ig_msg} (status {e.response.status_code})"
                    except Exception:
                        err_str = f"{e.response.status_code}: {e.response.text[:120]}"
                log(acc_id, f"❌ Send failed → {thread_id}: {err_str}")

            msg_idx += 1
            bot_status[acc_id]["gcs_done"] += 1
            grp_sleep = random.uniform(grp_min, grp_max)
            log(acc_id, f"💤 Grp delay: {grp_sleep:.1f}s")
            time.sleep(grp_sleep)

        title_idx += 1
        bot_status[acc_id]["round"] += 1
        bot_status[acc_id]["gcs_done"] = 0  # reset for next round
        log(acc_id, f"✓ Round {bot_status[acc_id]['round']} complete | Sent: {bot_status[acc_id]['sent']} | Failed: {bot_status[acc_id]['failed']}")
        bot_status[acc_id]["last_action"] = "Round complete ✓"

        # Browser restart logic
        if browser_restart_every > 0 and bot_status[acc_id]["round"] % browser_restart_every == 0:
            log(acc_id, f"🌐 {browser_restart_every} rounds complete — restarting browser...")
            bot_status[acc_id]["last_action"] = "Restarting browser..."
            ok = restart_browser()
            if ok:
                log(acc_id, "✅ Browser restarted — 8s wait...")
            else:
                log(acc_id, "⚠️ Browser restart failed — continue anyway...")
            time.sleep(8)
            # Fresh Instagram client
            ig_clients.pop(acc_id, None)
            log(acc_id, "🔄 Re-logging in...")
            bot_status[acc_id]["last_action"] = "Re-logging in..."
            try:
                cl = get_client(acc_id, session_id, proxy)
                log(acc_id, f"✅ Re-login done — starting next round...")
                bot_status[acc_id]["last_action"] = "Resumed ✓"
            except Exception as e:
                log(acc_id, f"❌ Re-login failed: {e} — will retry next round")
                bot_status[acc_id]["last_action"] = f"Re-login failed, retrying next round"

        round_sleep = random.uniform(round_min, round_max)
        log(acc_id, f"💤 Round delay: {round_sleep:.1f}s")
        time.sleep(round_sleep)

    log(acc_id, "🛑 Bot stopped")
    bot_status[acc_id]["running"] = False
    bot_status[acc_id]["last_action"] = "Stopped"

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>MAGNITUDE V1 Panel</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap');
  :root{--bg:#0a0a0f;--bg2:#0f0f1a;--bg3:#13131f;--border:#1e1e35;--purple:#7c3aed;--purple2:#a855f7;--green:#10b981;--red:#ef4444;--yellow:#f59e0b;--cyan:#06b6d4;--text:#e2e8f0;--muted:#64748b}
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:'Rajdhani',sans-serif;min-height:100vh;overflow-x:hidden}
  body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(124,58,237,.02) 2px,rgba(124,58,237,.02) 4px);pointer-events:none;z-index:9999}
  .header{background:linear-gradient(90deg,var(--bg2),var(--bg3));border-bottom:1px solid var(--border);padding:16px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
  .logo{font-family:'Share Tech Mono',monospace;font-size:20px;color:var(--purple2);letter-spacing:3px;text-shadow:0 0 20px rgba(168,85,247,.5)}
  .header-stats{display:flex;gap:24px}
  .hstat{font-family:'Share Tech Mono',monospace;font-size:12px;color:var(--muted)}
  .hstat b{color:var(--purple2)}
  .container{max-width:1200px;margin:0 auto;padding:24px}
  .add-btn{display:flex;align-items:center;gap:8px;background:linear-gradient(135deg,var(--purple),#5b21b6);border:none;color:white;padding:10px 20px;border-radius:6px;font-family:'Rajdhani',sans-serif;font-size:16px;font-weight:700;cursor:pointer;letter-spacing:1px;transition:all .2s;margin-bottom:20px;text-transform:uppercase}
  .add-btn:hover{background:linear-gradient(135deg,var(--purple2),var(--purple));box-shadow:0 0 20px rgba(124,58,237,.4)}
  .accounts-grid{display:flex;flex-direction:column;gap:16px}
  .account-card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;overflow:hidden;transition:border-color .2s}
  .account-card.running{border-color:var(--green);box-shadow:0 0 15px rgba(16,185,129,.1)}
  .card-header{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;background:var(--bg3);cursor:pointer;user-select:none}
  .card-header:hover{background:#16162a}
  .card-title{display:flex;align-items:center;gap:10px;font-size:17px;font-weight:700;letter-spacing:1px}
  .status-dot{width:10px;height:10px;border-radius:50%;background:var(--muted);flex-shrink:0}
  .status-dot.running{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 1.5s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .card-actions{display:flex;gap:8px;align-items:center}
  .btn{padding:6px 14px;border-radius:5px;border:none;font-family:'Rajdhani',sans-serif;font-size:14px;font-weight:700;cursor:pointer;letter-spacing:1px;transition:all .15s;text-transform:uppercase}
  .btn-start{background:var(--green);color:#000}.btn-start:hover{background:#34d399;box-shadow:0 0 12px rgba(16,185,129,.4)}
  .btn-stop{background:var(--red);color:white}.btn-stop:hover{background:#f87171}
  .btn-edit{background:var(--bg);color:var(--cyan);border:1px solid var(--cyan)}.btn-edit:hover{background:rgba(6,182,212,.1)}
  .btn-del{background:transparent;color:var(--muted);border:1px solid var(--border)}.btn-del:hover{color:var(--red);border-color:var(--red)}
  .btn-log{background:var(--bg);color:var(--purple2);border:1px solid var(--purple);}.btn-log:hover{background:rgba(124,58,237,.1)}
  .btn-fetch{background:var(--bg);color:var(--yellow);border:1px solid var(--yellow);padding:6px 14px;border-radius:5px;font-family:'Rajdhani',sans-serif;font-size:14px;font-weight:700;cursor:pointer;letter-spacing:1px;transition:all .15s;text-transform:uppercase}
  .btn-fetch:hover{background:rgba(245,158,11,.1)}.btn-fetch:disabled{opacity:.4;cursor:not-allowed}
  .stats-row{display:flex;border-top:1px solid var(--border);font-family:'Share Tech Mono',monospace;font-size:12px}
  .stat-item{flex:1;padding:10px 16px;border-right:1px solid var(--border);text-align:center}.stat-item:last-child{border-right:none}
  .stat-label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:1px}
  .stat-val{font-size:18px;font-weight:700;margin-top:2px}
  .stat-val.green{color:var(--green)}.stat-val.red{color:var(--red)}.stat-val.purple{color:var(--purple2)}.stat-val.yellow{color:var(--yellow)}
  .last-action{padding:8px 18px;font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--muted);border-top:1px solid var(--border);background:var(--bg)}
  .last-action span{color:var(--cyan)}
  .card-body{display:none;border-top:1px solid var(--border)}.card-body.open{display:block}
  .card-body-inner{padding:18px}
  /* Console */
  .console{background:#000;border-top:1px solid var(--border);padding:12px 16px;font-family:'Share Tech Mono',monospace;font-size:11px;height:220px;overflow-y:auto;line-height:1.6}
  .console-line{white-space:pre-wrap;word-break:break-all}
  .console-line.ok{color:#10b981}.console-line.err{color:#ef4444}.console-line.warn{color:#f59e0b}.console-line.info{color:#06b6d4}.console-line.dim{color:#64748b}
  .console-header{display:flex;align-items:center;justify-content:space-between;padding:8px 16px;background:var(--bg3);border-top:1px solid var(--border);font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--muted)}
  .form-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .form-group{display:flex;flex-direction:column;gap:6px}.form-group.full{grid-column:1/-1}
  label{font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:1px}
  input,textarea{background:var(--bg);border:1px solid var(--border);border-radius:5px;color:var(--text);font-family:'Share Tech Mono',monospace;font-size:13px;padding:9px 12px;outline:none;transition:border-color .2s;resize:vertical;autocomplete:off}
  input:focus,textarea:focus{border-color:var(--purple);box-shadow:0 0 0 2px rgba(124,58,237,.15)}
  input::placeholder,textarea::placeholder{color:var(--muted)}
  .save-btn{margin-top:14px;background:linear-gradient(135deg,var(--purple),#5b21b6);color:white;border:none;padding:10px 24px;border-radius:5px;font-family:'Rajdhani',sans-serif;font-size:15px;font-weight:700;cursor:pointer;letter-spacing:1px;text-transform:uppercase;transition:all .2s}
  .save-btn:hover{box-shadow:0 0 16px rgba(124,58,237,.4)}
  .modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:1000;align-items:center;justify-content:center}.modal-overlay.open{display:flex}
  .modal{background:var(--bg2);border:1px solid var(--purple);border-radius:10px;padding:28px;width:600px;max-width:95vw;max-height:90vh;overflow-y:auto;box-shadow:0 0 40px rgba(124,58,237,.3)}
  .modal-title{font-size:20px;font-weight:700;color:var(--purple2);letter-spacing:2px;text-transform:uppercase;margin-bottom:20px;font-family:'Share Tech Mono',monospace}
  .modal-footer{display:flex;gap:10px;margin-top:20px;justify-content:flex-end}
  .btn-cancel{background:transparent;color:var(--muted);border:1px solid var(--border);padding:9px 20px;border-radius:5px;font-family:'Rajdhani',sans-serif;font-size:15px;font-weight:700;cursor:pointer;text-transform:uppercase}
  .empty-state{text-align:center;padding:80px 20px;color:var(--muted);font-family:'Share Tech Mono',monospace}
  .empty-state .big{font-size:48px;margin-bottom:12px}
  .empty-state p{font-size:14px;margin-bottom:20px}
  .toast{position:fixed;bottom:24px;right:24px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:12px 20px;font-family:'Share Tech Mono',monospace;font-size:13px;z-index:9998;transform:translateY(80px);opacity:0;transition:all .3s}
  .toast.show{transform:translateY(0);opacity:1}.toast.success{border-color:var(--green);color:var(--green)}.toast.error{border-color:var(--red);color:var(--red)}
  .chevron{transition:transform .2s;display:inline-block}.chevron.open{transform:rotate(180deg)}
  .runtime{font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--muted)}
  .group-picker{display:flex;flex-direction:column;gap:4px;max-height:180px;overflow-y:auto;border:1px solid var(--border);border-radius:5px;padding:8px;background:var(--bg);margin-top:4px}
  .group-item{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:4px;cursor:pointer;transition:background .15s}
  .group-item:hover{background:var(--bg3)}
  .group-item input[type=checkbox]{accent-color:var(--purple);width:14px;height:14px;cursor:pointer;flex-shrink:0}
  .group-item label{font-size:12px;color:var(--text);cursor:pointer;font-family:'Share Tech Mono',monospace;margin:0;text-transform:none;letter-spacing:0;font-weight:400}
  .fetch-status{font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--yellow);margin-top:4px;min-height:16px}
  .sel-all-row{display:flex;gap:8px;margin-bottom:4px}
  .sel-btn{background:transparent;border:1px solid var(--border);color:var(--muted);padding:3px 10px;border-radius:4px;font-family:'Rajdhani',sans-serif;font-size:12px;cursor:pointer;font-weight:700}
  .sel-btn:hover{border-color:var(--purple2);color:var(--purple2)}
</style>
</head>
<body>
<div class="header">
  <div class="logo">「 MAGNITUDE V1 · <span id="global-runtime" style="color:var(--cyan);font-size:13px">00:00:00</span> 」</div>
  <div class="header-stats">
    <div class="hstat">ACCOUNTS: <b id="h-total">0</b></div>
    <div class="hstat">RUNNING: <b id="h-running">0</b></div>
    <div class="hstat">TOTAL SENT: <b id="h-sent">0</b></div>
  </div>
</div>
<div class="container">
  <button class="add-btn" onclick="openAddModal()">+ Add Account</button>
  <div class="accounts-grid" id="accounts-list">
    <div class="empty-state" id="empty-state">
      <div class="big">⚡</div>
      <p>No accounts added yet.</p>
      <button class="add-btn" style="margin:0 auto;" onclick="openAddModal()">+ Add First Account</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-title" id="modal-title">Add Account</div>
    <div class="form-grid">
      <div class="form-group full"><label>Account Name</label><input autocomplete="off" type="text" id="f-name" placeholder="e.g. Main Account"/></div>
      <div class="form-group full"><label>Session ID</label><input autocomplete="off" type="text" id="f-sid" placeholder="sessionid cookie"/></div>
      <div class="form-group full"><label>Proxy <span style="opacity:.5;font-weight:400">(optional)</span></label><input autocomplete="off" type="text" id="f-proxy" placeholder="http://user:pass@ip:port"/></div>
      <div class="form-group full">
        <label>Groups</label>
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">
          <button class="btn-fetch" id="fetch-btn" onclick="fetchGroups()">⚡ Fetch Groups</button>
        </div>
        <div class="fetch-status" id="fetch-status"></div>
        <div id="group-picker-wrap" style="display:none">
          <div class="sel-all-row">
            <button class="sel-btn" onclick="selectAll(true)">Select All</button>
            <button class="sel-btn" onclick="selectAll(false)">Deselect All</button>
          </div>
          <div class="group-picker" id="group-picker"></div>
        </div>
        <div style="margin-top:8px">
          <label style="font-size:10px;color:var(--muted)">OR paste thread IDs manually (one per line)</label>
          <textarea id="f-groups" rows="3" placeholder="https://www.instagram.com/direct/t/123456/"></textarea>
        </div>
      </div>
      <div class="form-group full"><label>NC Titles (comma separated)</label><input type="text" id="f-titles" placeholder="🔥 Title One, 💎 Title Two"/></div>
      <div class="form-group full">
        <label>Messages <span style="color:var(--muted);font-size:10px;text-transform:none;letter-spacing:0">(separate multiple messages with a blank line between --- blocks)</span></label>
        <div id="msg-slots" style="display:flex;flex-direction:column;gap:8px;"></div>
        <button type="button" class="sel-btn" style="margin-top:6px;align-self:flex-start" onclick="addMsgSlot()">+ Add Message</button>
      </div>
      <div class="form-group full" style="margin-top:8px">
        <label style="color:var(--cyan);margin-bottom:10px;display:block">⏱ DELAYS &amp; RESTART</label>
        <div style="display:flex;flex-direction:column;gap:0;border:1px solid var(--border);border-radius:6px;overflow:hidden;font-family:'Share Tech Mono',monospace;font-size:12px;">
          <!-- GRP min -->
          <div style="display:flex;align-items:center;border-bottom:1px solid var(--border);">
            <div style="width:220px;padding:9px 14px;color:var(--muted);background:var(--bg3);border-right:1px solid var(--border);font-size:11px;letter-spacing:.5px;">Grp Delay — Min (s)</div>
            <input type="number" id="f-grp-min" value="2" min="0.5" step="0.5" style="border:none;border-radius:0;background:var(--bg);flex:1;padding:9px 14px;font-size:13px;"/>
          </div>
          <!-- GRP max -->
          <div style="display:flex;align-items:center;border-bottom:1px solid var(--border);">
            <div style="width:220px;padding:9px 14px;color:var(--muted);background:var(--bg3);border-right:1px solid var(--border);font-size:11px;letter-spacing:.5px;">Grp Delay — Max (s)</div>
            <input type="number" id="f-grp-max" value="2" min="0.5" step="0.5" style="border:none;border-radius:0;background:var(--bg);flex:1;padding:9px 14px;font-size:13px;"/>
          </div>
          <!-- Round min -->
          <div style="display:flex;align-items:center;border-bottom:1px solid var(--border);">
            <div style="width:220px;padding:9px 14px;color:var(--muted);background:var(--bg3);border-right:1px solid var(--border);font-size:11px;letter-spacing:.5px;">Round Delay — Min (s)</div>
            <input type="number" id="f-round-min" value="5" min="1" step="1" style="border:none;border-radius:0;background:var(--bg);flex:1;padding:9px 14px;font-size:13px;"/>
          </div>
          <!-- Round max -->
          <div style="display:flex;align-items:center;border-bottom:1px solid var(--border);">
            <div style="width:220px;padding:9px 14px;color:var(--muted);background:var(--bg3);border-right:1px solid var(--border);font-size:11px;letter-spacing:.5px;">Round Delay — Max (s)</div>
            <input type="number" id="f-round-max" value="5" min="1" step="1" style="border:none;border-radius:0;background:var(--bg);flex:1;padding:9px 14px;font-size:13px;"/>
          </div>
          <!-- Browser restart -->
          <div style="display:flex;align-items:center;">
            <div style="width:220px;padding:9px 14px;color:var(--muted);background:var(--bg3);border-right:1px solid var(--border);font-size:11px;letter-spacing:.5px;">Client Restart every N rounds<br><span style="color:var(--muted);font-size:10px;letter-spacing:0">(0 = disabled)</span></div>
            <input type="number" id="f-browser-restart" value="0" min="0" step="1" placeholder="0" style="border:none;border-radius:0;background:var(--bg);flex:1;padding:9px 14px;font-size:13px;"/>
          </div>
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="save-btn" onclick="saveAccount()">Save Account</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>
<script>
let accounts={}, editingId=null, fetchedGroups=[], consoleOpen={}, cardOpen={};

function fmt(s){const d=Math.floor(s/86400),h=Math.floor((s%86400)/3600),m=Math.floor((s%3600)/60),ss=Math.floor(s%60);return`${String(d).padStart(2,'0')}:${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`}
function toast(msg,type='success'){const t=document.getElementById('toast');t.textContent=msg;t.className=`toast ${type} show`;setTimeout(()=>t.className='toast',2500)}

function lineClass(line){
  if(line.includes('✅')||line.includes('Sent')||line.includes('done')||line.includes('success')||line.includes('Logged in')) return 'ok';
  if(line.includes('❌')||line.includes('failed')||line.includes('error')||line.includes('Failed')) return 'err';
  if(line.includes('⚠️')||line.includes('NC failed')||line.includes('warn')) return 'warn';
  if(line.includes('⚡')||line.includes('🔄')||line.includes('Round')||line.includes('Starting')) return 'info';
  if(line.includes('🛑')||line.includes('Stopped')) return 'warn';
  return 'dim';
}

function addMsgSlot(val=''){
  const wrap=document.getElementById('msg-slots');
  const idx=wrap.children.length;
  const d=document.createElement('div');
  d.style='display:flex;gap:6px;align-items:flex-start';
  d.innerHTML=`<textarea rows="3" class="msg-slot" placeholder="Message ${idx+1}..." style="flex:1">${val}</textarea><button type="button" class="sel-btn" style="margin-top:2px;color:var(--red);border-color:var(--red)" onclick="this.parentElement.remove()">✕</button>`;
  wrap.appendChild(d);
}
function getMsgs(){return [...document.querySelectorAll('.msg-slot')].map(t=>t.value.trim()).filter(Boolean).join('---MSG---')}
function setMsgs(raw){
  document.getElementById('msg-slots').innerHTML='';
  const parts=(raw||'').split('---MSG---').filter(Boolean);
  if(!parts.length) addMsgSlot();
  else parts.forEach(p=>addMsgSlot(p));
}

function openAddModal(){
  editingId=null;
  document.getElementById('modal-title').textContent='Add Account';
  ['name','sid','groups','titles'].forEach(k=>document.getElementById('f-'+k).value='');
  document.getElementById('f-grp-min').value=2;document.getElementById('f-grp-max').value=2;
  document.getElementById('f-round-min').value=5;document.getElementById('f-round-max').value=5;
  document.getElementById('f-browser-restart').value=0;
  setMsgs('');
  document.getElementById('group-picker-wrap').style.display='none';
  document.getElementById('fetch-status').textContent='';
  fetchedGroups=[];
  document.getElementById('modal').classList.add('open');
}
function openEditModal(id){
  editingId=id; const acc=accounts[id];
  document.getElementById('modal-title').textContent='Edit Account';
  document.getElementById('f-name').value=acc.name||'';
  document.getElementById('f-sid').value=acc.session_id||'';
  document.getElementById('f-proxy').value=acc.proxy||'';
  document.getElementById('f-groups').value=acc.groups||'';
  document.getElementById('f-titles').value=acc.nc_titles||'';
  setMsgs(acc.messages||acc.message||'');
  document.getElementById('f-grp-min').value=acc.grp_delay_min||2;
  document.getElementById('f-grp-max').value=acc.grp_delay_max||2;
  document.getElementById('f-round-min').value=acc.round_delay_min||5;
  document.getElementById('f-round-max').value=acc.round_delay_max||5;
  document.getElementById('f-browser-restart').value=acc.browser_restart_every||0;
  document.getElementById('group-picker-wrap').style.display='none';
  document.getElementById('fetch-status').textContent='';
  fetchedGroups=[];
  document.getElementById('modal').classList.add('open');
}
function closeModal(){document.getElementById('modal').classList.remove('open');editingId=null}

async function fetchGroups(){
  const sid=document.getElementById('f-sid').value.trim();
  if(!sid){toast('Enter Session ID first','error');return}
  const btn=document.getElementById('fetch-btn'),status=document.getElementById('fetch-status');
  btn.disabled=true;btn.textContent='Fetching...';status.textContent='⏳ Connecting...';
  try{
    const r=await fetch('/api/fetch-groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid})});
    const data=await r.json();
    if(!data.success){status.textContent='❌ '+data.error;btn.disabled=false;btn.textContent='⚡ Fetch Groups';return}
    fetchedGroups=data.groups;
    renderGroupPicker(data.groups);
    status.textContent=`✓ Found ${data.groups.length} groups`;
  }catch(e){status.textContent='❌ Network error'}
  btn.disabled=false;btn.textContent='⚡ Fetch Groups';
}
function renderGroupPicker(groups){
  const picker=document.getElementById('group-picker');picker.innerHTML='';
  groups.forEach((g,i)=>{
    const div=document.createElement('div');div.className='group-item';
    div.innerHTML=`<input type="checkbox" id="grp-${i}" value="${g.id}" checked/><label for="grp-${i}">${g.title} <span style="color:var(--muted)">(${g.id})</span></label>`;
    picker.appendChild(div);
  });
  document.getElementById('group-picker-wrap').style.display='block';
}
function selectAll(val){document.querySelectorAll('#group-picker input[type=checkbox]').forEach(cb=>cb.checked=val)}
function getSelectedGroups(){
  const checked=[...document.querySelectorAll('#group-picker input[type=checkbox]:checked')].map(cb=>cb.value);
  const manual=document.getElementById('f-groups').value.trim();
  const manualLines=manual?manual.split('\n').filter(Boolean):[];
  return[...new Set([...checked,...manualLines])].join('\n');
}

async function saveAccount(){
  const groups=fetchedGroups.length>0?getSelectedGroups():document.getElementById('f-groups').value;
  const body={
    name:document.getElementById('f-name').value,
    session_id:document.getElementById('f-sid').value,
    proxy:document.getElementById('f-proxy').value.trim(),
    groups,
    nc_titles:document.getElementById('f-titles').value,
    messages:getMsgs(),
    grp_delay_min:parseFloat(document.getElementById('f-grp-min').value)||2,
    grp_delay_max:parseFloat(document.getElementById('f-grp-max').value)||2,
    round_delay_min:parseFloat(document.getElementById('f-round-min').value)||5,
    round_delay_max:parseFloat(document.getElementById('f-round-max').value)||5,
    browser_restart_every:parseInt(document.getElementById('f-browser-restart').value)||0
  };
  if(!body.name){toast('Account name required','error');return}
  if(body.grp_delay_max<body.grp_delay_min)body.grp_delay_max=body.grp_delay_min;
  if(body.round_delay_max<body.round_delay_min)body.round_delay_max=body.round_delay_min;
  let url='/api/accounts',method='POST';
  if(editingId){url=`/api/accounts/${editingId}`;method='PUT';if(!body.session_id)delete body.session_id}
  const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await r.json();
  if(data.success){toast(editingId?'Account updated!':'Account added!');closeModal();loadAccounts()}
  else toast(data.error||'Error','error');
}

async function startBot(id,e){e.stopPropagation();const r=await fetch(`/api/accounts/${id}/start`,{method:'POST'});const d=await r.json();if(d.success)toast('Bot started! 🚀');else toast(d.error||'Failed','error')}
async function stopBot(id,e){e.stopPropagation();await fetch(`/api/accounts/${id}/stop`,{method:'POST'});toast('Bot stopped.')}
async function deleteAccount(id,e){e.stopPropagation();if(!confirm('Delete?'))return;await fetch(`/api/accounts/${id}`,{method:'DELETE'});toast('Deleted.');loadAccounts()}
function toggleCard(id){
  const b=document.getElementById(`body-${id}`);b.classList.toggle('open');
  document.getElementById(`chev-${id}`).classList.toggle('open');
  cardOpen[id]=b.classList.contains('open');
}
function toggleConsole(id,e){
  if(e)e.stopPropagation();
  consoleOpen[id]=!consoleOpen[id];
  const c=document.getElementById(`cons-${id}`);
  if(c)c.style.display=consoleOpen[id]?'block':'none';
}

async function loadAccounts(){
  const[accRes,statusRes]=await Promise.all([fetch('/api/accounts'),fetch('/api/status')]);
  accounts=await accRes.json();const statusData=await statusRes.json();
  const statuses=statusData.accounts||statusData;
  const serverNow=statusData.server_time||Date.now()/1000;
  const list=document.getElementById('accounts-list'),empty=document.getElementById('empty-state'),ids=Object.keys(accounts);
  if(!ids.length){list.innerHTML='';list.appendChild(empty);empty.style.display='block';['h-total','h-running','h-sent'].forEach(i=>document.getElementById(i).textContent=0);return}
  empty.style.display='none';let totalRunning=0,totalSent=0;

  for(const id of ids){
    const acc=accounts[id],st=statuses[id]||{running:false,sent:0,failed:0,nc_done:0,nc_failed:0,nc_skipped:0,round:0,last_action:'Idle'};
    const running=st.running;if(running)totalRunning++;totalSent+=(st.sent||0);
    // runtime: use server's started_at (unix seconds), compare with server's now
    const runtime=running&&st.started_at?fmt(serverNow-st.started_at):'—';
    const grpCount=(acc.groups||'').split('\n').filter(Boolean).length;

    let card=document.getElementById(`card-${id}`);
    const isNew=!card;
    if(isNew){card=document.createElement('div');card.id=`card-${id}`;list.appendChild(card);}
    card.className=`account-card ${running?'running':''}`;
    card.innerHTML=`
      <div class="card-header" onclick="toggleCard('${id}')">
        <div class="card-title"><div class="status-dot ${running?'running':''}"></div>${acc.name}${running?`<span class="runtime"> [${runtime}]</span>`:''}</div>
        <div class="card-actions">
          ${!running?`<button class="btn btn-start" onclick="startBot('${id}',event)">▶ Start</button>`:`<button class="btn btn-stop" onclick="stopBot('${id}',event)">■ Stop</button>`}
          <button class="btn btn-log" onclick="toggleConsole('${id}',event)">📋 Logs</button>
          <button class="btn btn-edit" onclick="openEditModal('${id}');event.stopPropagation()">Edit</button>
          <button class="btn btn-del" onclick="deleteAccount('${id}',event)">✕</button>
          <span class="chevron" id="chev-${id}">▾</span>
        </div>
      </div>
      <div class="stats-row">
        <div class="stat-item"><div class="stat-label">Sent</div><div class="stat-val green">${st.sent||0}</div></div>
        <div class="stat-item"><div class="stat-label">Failed</div><div class="stat-val red">${st.failed||0}</div></div>
        <div class="stat-item"><div class="stat-label">NC Done</div><div class="stat-val purple">${st.nc_done||0}</div></div>
        <div class="stat-item"><div class="stat-label">NC Skip</div><div class="stat-val" style="color:var(--muted)">${st.nc_skipped||0}</div></div>
        <div class="stat-item"><div class="stat-label">Rounds</div><div class="stat-val yellow">${st.round||0}</div></div>
        <div class="stat-item"><div class="stat-label">GCs</div><div class="stat-val" style="color:var(--cyan);font-size:14px">${st.gcs_done||0}<span style="color:var(--muted);font-size:11px"> / ${st.total_gcs||grpCount}</span></div></div>
        <div class="stat-item"><div class="stat-label">Runtime</div><div class="stat-val" style="color:var(--cyan);font-size:12px">${runtime}</div></div>
      </div>
      <div class="last-action">▸ <span>${st.last_action||'Idle'}</span></div>
      <div class="card-body" id="body-${id}">
        <div class="card-body-inner" style="font-family:'Share Tech Mono',monospace;font-size:12px;color:var(--muted)">
          <div style="margin-bottom:6px"><b style="color:var(--cyan)">GROUPS:</b> ${grpCount} configured</div>
          <div style="margin-bottom:6px"><b style="color:var(--cyan)">NC TITLES:</b> ${acc.nc_titles||'—'}</div>
          <div style="margin-bottom:6px"><b style="color:var(--cyan)">GRP DELAY:</b> ${acc.grp_delay_min||2}s – ${acc.grp_delay_max||2}s &nbsp;|&nbsp; <b style="color:var(--cyan)">ROUND DELAY:</b> ${acc.round_delay_min||5}s – ${acc.round_delay_max||5}s</div>
          ${(()=>{const msgs=(acc.messages||acc.message||'').split('---MSG---').filter(Boolean);return msgs.map((m,i)=>`<div style="margin-bottom:4px"><b style="color:var(--cyan)">MSG ${i+1}:</b> ${m.substring(0,80)}${m.length>80?'...':''}</div>`).join('')})()}
        </div>
        <div class="console-header">
          <span>📋 CONSOLE LOG</span>
          <span style="color:var(--green)">${running?'● LIVE':'○ IDLE'}</span>
        </div>
        <div class="console" id="cons-${id}" style="display:${consoleOpen[id]?'block':'none'}"></div>
      </div>`;
    // Restore card open state
    if(cardOpen[id]){document.getElementById(`body-${id}`).classList.add('open');const ch=document.getElementById(`chev-${id}`);if(ch)ch.classList.add('open');}
  }

  // Remove deleted cards
  [...list.querySelectorAll('.account-card')].forEach(c=>{
    if(!accounts[c.id.replace('card-','')]){list.removeChild(c)}
  });

  document.getElementById('h-total').textContent=ids.length;
  document.getElementById('h-running').textContent=totalRunning;
  document.getElementById('h-sent').textContent=totalSent;

  // Update console logs
  updateLogs();
}

async function updateLogs(){
  const ids=Object.keys(accounts);
  for(const id of ids){
    const consEl=document.getElementById(`cons-${id}`);
    if(!consEl) continue;
    try{
      const r=await fetch(`/api/logs/${id}`);
      const data=await r.json();
      const wasAtBottom=consEl.scrollHeight-consEl.scrollTop<=consEl.clientHeight+20;
      consEl.innerHTML=data.logs.map(line=>`<div class="console-line ${lineClass(line)}">${line}</div>`).join('');
      if(wasAtBottom) consEl.scrollTop=consEl.scrollHeight;
    }catch(e){}
  }
}

document.getElementById('modal').addEventListener('click',function(e){if(e.target===this)closeModal()});

// Global runtime clock (counts up from page load)
let _pageStart=Date.now();
function updateGlobalClock(){
  const s=Math.floor((Date.now()-_pageStart)/1000);
  const h=String(Math.floor(s/3600)).padStart(2,'0');
  const m=String(Math.floor((s%3600)/60)).padStart(2,'0');
  const sec=String(s%60).padStart(2,'0');
  const el=document.getElementById('global-runtime');
  if(el)el.textContent=`${h}:${m}:${sec}`;
}
setInterval(updateGlobalClock,1000);
updateGlobalClock();
loadAccounts();
setInterval(loadAccounts,3000);
setInterval(updateLogs,1500);
</script>
</body>
</html>"""

@app.route("/")
def index(): return HTML

@app.route("/api/fetch-groups", methods=["POST"])
def fetch_groups():
    body = request.json
    session_id = body.get("session_id","").strip()
    if not session_id:
        return jsonify({"success": False, "error": "Session ID required"})
    try:
        cl = Client()
        cl.login_by_sessionid(session_id)
        threads = cl.direct_threads(amount=50)
        groups = []
        for t in threads:
            if len(t.users) > 1:
                title = t.thread_title or f"Group ({len(t.users)+1} members)"
                groups.append({"id": str(t.id), "title": title, "members": len(t.users)+1})
        return jsonify({"success": True, "groups": groups})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/logs/<acc_id>")
def get_logs(acc_id):
    logs = list(bot_logs.get(acc_id, []))
    return jsonify({"logs": logs})

@app.route("/api/accounts", methods=["GET"])
def get_accounts():
    with data_lock: d = load_data()
    result = {}
    for acc_id, acc in d.get("accounts", {}).items():
        st = bot_status.get(acc_id, {"running": False})
        result[acc_id] = {"name": acc.get("name",""), "session_id": acc.get("session_id",""), "proxy": acc.get("proxy",""), "groups": acc.get("groups",""),
            "nc_titles": acc.get("nc_titles",""), "messages": acc.get("messages",""),
            "message": acc.get("message",""),
            "grp_delay_min": acc.get("grp_delay_min", 2),
            "grp_delay_max": acc.get("grp_delay_max", 2),
            "round_delay_min": acc.get("round_delay_min", 5),
            "round_delay_max": acc.get("round_delay_max", 5),
            "browser_restart_every": acc.get("browser_restart_every", 0),
            "status": st}
    return jsonify(result)

@app.route("/api/accounts", methods=["POST"])
def add_account():
    body = request.json
    with data_lock:
        d = load_data()
        acc_id = str(int(time.time() * 1000))
        d["accounts"][acc_id] = {"name": body.get("name", f"Account {acc_id}"),
            "session_id": body.get("session_id",""), "proxy": body.get("proxy",""), "groups": body.get("groups",""),
            "nc_titles": body.get("nc_titles",""), "messages": body.get("messages",""),
            "message": body.get("message",""),
            "grp_delay_min": body.get("grp_delay_min", 2),
            "grp_delay_max": body.get("grp_delay_max", 2),
            "round_delay_min": body.get("round_delay_min", 5),
            "round_delay_max": body.get("round_delay_max", 5),
            "browser_restart_every": body.get("browser_restart_every", 0)}
        save_data(d)
    return jsonify({"success": True, "id": acc_id})

@app.route("/api/accounts/<acc_id>", methods=["PUT"])
def update_account(acc_id):
    body = request.json
    with data_lock:
        d = load_data()
        if acc_id not in d["accounts"]: return jsonify({"success": False, "error": "Not found"}), 404
        acc = d["accounts"][acc_id]
        for k in ["name","session_id","proxy","groups","nc_titles","messages","message",
                  "grp_delay_min","grp_delay_max","round_delay_min","round_delay_max",
                  "browser_restart_every"]:
            if k in body and body[k] != "": acc[k] = body[k]
        save_data(d)
    ig_clients.pop(acc_id, None)
    return jsonify({"success": True})

@app.route("/api/accounts/<acc_id>", methods=["DELETE"])
def delete_account(acc_id):
    if acc_id in bot_stop: bot_stop[acc_id].set()
    ig_clients.pop(acc_id, None)
    with data_lock:
        d = load_data()
        d["accounts"].pop(acc_id, None)
        save_data(d)
    return jsonify({"success": True})

@app.route("/api/accounts/<acc_id>/start", methods=["POST"])
def start_bot(acc_id):
    with data_lock:
        d = load_data()
        acc = d["accounts"].get(acc_id)
    if not acc: return jsonify({"success": False, "error": "Not found"}), 404
    # If thread is alive, stop it first and wait
    if acc_id in bot_threads and bot_threads[acc_id].is_alive():
        if acc_id in bot_stop: bot_stop[acc_id].set()
        bot_threads[acc_id].join(timeout=5)  # max 5s wait
        if bot_threads[acc_id].is_alive():
            return jsonify({"success": False, "error": "Bot did not stop in time, please wait a moment"})
    stop_event = threading.Event()
    bot_stop[acc_id] = stop_event
    t = threading.Thread(target=bot_worker, args=(acc_id, acc, stop_event), daemon=True)
    bot_threads[acc_id] = t
    t.start()
    return jsonify({"success": True})

@app.route("/api/accounts/<acc_id>/stop", methods=["POST"])
def stop_bot(acc_id):
    if acc_id in bot_stop: bot_stop[acc_id].set()
    if acc_id in bot_status:
        bot_status[acc_id]["running"] = False
        bot_status[acc_id]["last_action"] = "Stopped"
    return jsonify({"success": True})

@app.route("/api/status")
def all_status():
    return jsonify({"accounts": bot_status, "server_time": time.time()})

@app.route("/health")
def health(): return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)