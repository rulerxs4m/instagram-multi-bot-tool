import json, os, threading, time, collections, random
from flask import Flask, request, jsonify
from instagrapi import Client

app = Flask(__name__)
DATA_FILE = "data_v2.json"
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
bot_logs    = {}

def log(acc_id, msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    if acc_id not in bot_logs:
        bot_logs[acc_id] = collections.deque(maxlen=300)
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
    try:
        cl.direct_thread_update_title(thread_id, title)
        return True
    except Exception: pass
    try:
        thread = cl.direct_thread(thread_id)
        thread.update_title(title)
        return True
    except Exception: pass
    try:
        cl.private_request(
            f"direct_v2/threads/{thread_id}/update_title/",
            data={"title": title, "_uuid": cl.uuid, "_csrftoken": cl.token}
        )
        return True
    except Exception: pass
    return False

def get_thread_title(cl, thread_id):
    try:
        thread = cl.direct_thread(int(thread_id))
        return (thread.thread_title or "").strip()
    except Exception:
        return None

def bot_worker(acc_id, acc, stop_event):
    session_id = acc["session_id"]
    proxy = acc.get("proxy", "").strip() or None
    # Max 5 GCs
    raw_groups = [extract_thread_id(g) for g in acc.get("groups", "").split("\n") if g.strip()]
    groups = raw_groups[:5]
    titles = [t.strip() for t in acc.get("nc_titles", "").split(",") if t.strip()]
    messages = [m.strip() for m in acc.get("messages", "").split("---MSG---") if m.strip()]
    if not messages:
        single = acc.get("message", "").strip()
        if single: messages = [single]

    # Delays
    msg_delay_min  = float(acc.get("msg_delay_min", 2))
    msg_delay_max  = float(acc.get("msg_delay_max", 5))

    # Cooldown after N rounds
    cooldown_after  = int(acc.get("cooldown_after", 0))    # 0 = disabled
    cooldown_dur    = float(acc.get("cooldown_dur", 5))    # minutes

    # NC every N minutes
    nc_every_min    = float(acc.get("nc_every_min", 0))    # 0 = every round (old behavior)

    bot_logs[acc_id] = collections.deque(maxlen=300)
    bot_status[acc_id] = {
        "running": True, "sent": 0, "failed": 0,
        "nc_done": 0, "nc_failed": 0, "nc_skipped": 0,
        "round": 0, "gcs_done": 0, "total_gcs": len(groups),
        "last_action": "Logging in...", "started_at": time.time(),
        "cooldown": False
    }

    log(acc_id, "⚡ Starting bot...")
    log(acc_id, f"📋 GCs: {len(groups)} | Titles: {len(titles)} | Messages: {len(messages)}")
    log(acc_id, f"⏱ Msg delay: {msg_delay_min}-{msg_delay_max}s")
    if cooldown_after > 0:
        log(acc_id, f"😴 Cooldown: every {cooldown_after} rounds → {cooldown_dur} min")
    if nc_every_min > 0:
        log(acc_id, f"✏️ NC: every {nc_every_min} min")

    try:
        cl = get_client(acc_id, session_id, proxy)
        log(acc_id, f"✅ Logged in successfully{' (proxy)' if proxy else ''}")
        bot_status[acc_id]["last_action"] = "Logged in ✓"
    except Exception as e:
        log(acc_id, f"❌ Login failed: {e}")
        bot_status[acc_id]["running"] = False
        bot_status[acc_id]["last_action"] = f"Login failed: {e}"
        return

    title_idx   = 0
    msg_idx     = 0
    last_nc_time = 0  # track when NC was last done (epoch seconds)

    while not stop_event.is_set():
        round_num = bot_status[acc_id]["round"] + 1
        log(acc_id, f"🔄 Round {round_num} starting...")
        bot_status[acc_id]["gcs_done"] = 0

        for thread_id in groups:
            if stop_event.is_set(): break

            # NC logic — timer based
            do_nc = False
            if titles:
                if nc_every_min <= 0:
                    # old behavior — every round
                    do_nc = True
                else:
                    elapsed_since_nc = (time.time() - last_nc_time) / 60.0
                    if elapsed_since_nc >= nc_every_min:
                        do_nc = True

            if do_nc and titles:
                t = titles[title_idx % len(titles)]
                bot_status[acc_id]["last_action"] = f"Checking NC → {thread_id}"
                current_title = get_thread_title(cl, thread_id)
                if current_title is not None and current_title == t:
                    log(acc_id, f"⏭ NC skip (already '{t}') → {thread_id}")
                    bot_status[acc_id]["nc_skipped"] += 1
                else:
                    bot_status[acc_id]["last_action"] = f"NC → {t}"
                    ok = nc_rename(cl, int(thread_id), t)
                    if ok:
                        bot_status[acc_id]["nc_done"] += 1
                        log(acc_id, f"✅ NC done [{t}] → {thread_id}")
                        last_nc_time = time.time()
                    else:
                        bot_status[acc_id]["nc_failed"] += 1
                        log(acc_id, f"⚠️ NC failed → {thread_id}")

            # Send message
            message = messages[msg_idx % len(messages)] if messages else ""
            bot_status[acc_id]["last_action"] = f"Sending → {thread_id}"
            try:
                cl.direct_send(message, thread_ids=[int(thread_id)])
                bot_status[acc_id]["sent"] += 1
                log(acc_id, f"✅ Sent → {thread_id}")
            except Exception as e:
                bot_status[acc_id]["failed"] += 1
                # Extract real Instagram error
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

            if stop_event.is_set(): break
            delay = random.uniform(msg_delay_min, msg_delay_max)
            log(acc_id, f"💤 Delay: {delay:.1f}s")
            time.sleep(delay)

        title_idx += 1
        log(acc_id, f"✓ GCs done | Sent: {bot_status[acc_id]['sent']} | Failed: {bot_status[acc_id]['failed']}")
        bot_status[acc_id]["last_action"] = "GCs done — waiting for cooldown..."

        # Cooldown logic — round counts AFTER cooldown
        if cooldown_after > 0:
            dur_secs = cooldown_dur * 60
            log(acc_id, f"😴 Cooldown started — {cooldown_dur} min pause...")
            bot_status[acc_id]["last_action"] = f"Cooldown {cooldown_dur} min..."
            bot_status[acc_id]["cooldown"] = True
            elapsed = 0
            while elapsed < dur_secs and not stop_event.is_set():
                time.sleep(1)
                elapsed += 1
            bot_status[acc_id]["cooldown"] = False
            log(acc_id, "✅ Cooldown done — resuming...")

        # Round count after cooldown
        bot_status[acc_id]["round"] += 1
        log(acc_id, f"✓ Round {bot_status[acc_id]['round']} complete")
        bot_status[acc_id]["last_action"] = "Round complete ✓"

    log(acc_id, "🛑 Bot stopped")
    bot_status[acc_id]["running"] = False
    bot_status[acc_id]["last_action"] = "Stopped"

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>AURA DOWNER</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap" rel="stylesheet"/>
<style>
  :root{
    --bg:    #000000;
    --bg2:   #0a0005;
    --bg3:   #110008;
    --red:   #cc2200;
    --red2:  #ff3300;
    --dark-red: #4a0000;
    --purple:#3d0066;
    --purple2:#6600aa;
    --text:  #e8d5d5;
    --muted: #7a5a5a;
    --green: #00cc44;
    --amber: #ff8800;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Rajdhani',sans-serif;min-height:100vh;}

  /* HEADER */
  .header{
    display:flex;align-items:center;justify-content:space-between;
    padding:14px 24px;
    border-bottom:1px solid var(--dark-red);
    background: linear-gradient(90deg, #0a0005 0%, #1a0010 50%, #0a0005 100%);
    position:sticky;top:0;z-index:100;
  }
  .logo{font-family:'Share Tech Mono',monospace;font-size:20px;letter-spacing:4px;color:var(--red2);text-shadow:0 0 20px var(--red),0 0 40px #ff000055;}

  .header-stats{display:flex;gap:24px;}
  .hstat{text-align:center;}
  .hstat-val{font-family:'Share Tech Mono',monospace;font-size:20px;color:var(--red2);}
  .hstat-lbl{font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;}

  /* MAIN */
  .main{padding:24px;max-width:1100px;margin:0 auto;}

  /* ADD BUTTON */
  .btn-add{
    background:transparent;border:1px solid var(--red);color:var(--red2);
    font-family:'Share Tech Mono',monospace;font-size:13px;letter-spacing:2px;
    padding:10px 22px;cursor:pointer;text-transform:uppercase;
    transition:all .2s;margin-bottom:20px;
  }
  .btn-add:hover{background:var(--red);color:#fff;box-shadow:0 0 20px var(--red);}

  /* ACCOUNT CARD */
  .acc-card{
    border:1px solid var(--dark-red);background:var(--bg2);
    border-radius:4px;margin-bottom:16px;overflow:hidden;
    transition:border-color .2s;
  }
  .acc-card:hover{border-color:var(--red);}
  .acc-header{
    display:flex;align-items:center;gap:12px;padding:12px 16px;
    background:linear-gradient(90deg,var(--bg3),var(--bg2));
    cursor:pointer;
  }
  .status-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;}
  .dot-on{background:var(--green);box-shadow:0 0 8px var(--green);}
  .dot-off{background:var(--muted);}
  .dot-cooldown{background:var(--amber);box-shadow:0 0 8px var(--amber);}
  .acc-name{font-family:'Share Tech Mono',monospace;font-size:14px;color:var(--red2);flex:1;}
  .acc-runtime{font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--muted);}
  .acc-btns{display:flex;gap:8px;margin-left:auto;}

  .btn{font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:1px;
    padding:6px 14px;border:1px solid;cursor:pointer;text-transform:uppercase;transition:all .2s;}
  .btn-stop{border-color:var(--red);color:var(--red);background:transparent;}
  .btn-stop:hover{background:var(--red);color:#fff;box-shadow:0 0 12px var(--red);}
  .btn-start{border-color:var(--green);color:var(--green);background:transparent;}
  .btn-start:hover{background:var(--green);color:#000;box-shadow:0 0 12px var(--green);}
  .btn-edit{border-color:var(--muted);color:var(--muted);background:transparent;}
  .btn-edit:hover{border-color:var(--text);color:var(--text);}
  .btn-del{border-color:#330000;color:#660000;background:transparent;padding:6px 10px;}
  .btn-del:hover{border-color:var(--red);color:var(--red);}
  .btn-logs{border-color:var(--purple2);color:var(--purple2);background:transparent;}
  .btn-logs:hover{background:var(--purple2);color:#fff;}
  .btn-collapse{background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:0 4px;}

  /* STATS ROW */
  .stats-row{
    display:flex;gap:0;border-top:1px solid var(--dark-red);
    background:var(--bg3);
  }
  .stat{flex:1;padding:10px 8px;text-align:center;border-right:1px solid var(--dark-red);}
  .stat:last-child{border-right:none;}
  .stat-val{font-family:'Share Tech Mono',monospace;font-size:18px;font-weight:700;}
  .stat-lbl{font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-top:2px;}
  .c-green{color:var(--green);}
  .c-red{color:var(--red2);}
  .c-amber{color:var(--amber);}
  .c-purple{color:#aa44ff;}
  .c-blue{color:#4499ff;}

  /* GC PILLS */
  .gc-row{padding:10px 16px;border-top:1px solid var(--dark-red);display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
  .gc-label{font-size:11px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-right:4px;}
  .gc-pill{font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--red2);
    border:1px solid var(--dark-red);padding:3px 8px;border-radius:2px;background:var(--bg3);}

  /* INFO ROW */
  .info-row{padding:8px 16px;border-top:1px solid var(--dark-red);font-size:12px;color:var(--muted);
    display:flex;gap:20px;flex-wrap:wrap;}
  .info-item{display:flex;gap:6px;}
  .info-key{color:var(--red);text-transform:uppercase;font-size:10px;letter-spacing:1px;}
  .info-val{color:var(--text);}

  /* LAST ACTION */
  .last-action{padding:8px 16px;border-top:1px solid var(--dark-red);
    font-size:12px;color:var(--muted);font-family:'Share Tech Mono',monospace;}
  .last-action span{color:var(--text);}

  /* LOG PANEL */
  .log-panel{display:none;border-top:1px solid var(--dark-red);background:#000;}
  .log-panel.open{display:block;}
  .log-header{display:flex;justify-content:space-between;align-items:center;
    padding:6px 12px;background:var(--bg3);border-bottom:1px solid var(--dark-red);}
  .log-title{font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--muted);letter-spacing:2px;}
  .log-live{font-size:10px;color:var(--green);letter-spacing:1px;}
  .log-box{height:220px;overflow-y:auto;padding:10px 12px;font-family:'Share Tech Mono',monospace;font-size:11px;line-height:1.6;}
  .log-box::-webkit-scrollbar{width:4px;}
  .log-box::-webkit-scrollbar-track{background:#000;}
  .log-box::-webkit-scrollbar-thumb{background:var(--dark-red);}
  .log-line{color:#666;}
  .log-line.ok{color:#00aa33;}
  .log-line.err{color:var(--red);}
  .log-line.info{color:#4488cc;}
  .log-line.warn{color:var(--amber);}
  .log-line.round{color:#aa44ff;}

  /* MODAL */
  .modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:1000;align-items:center;justify-content:center;}
  .modal-overlay.open{display:flex;}
  .modal{
    background:var(--bg2);border:1px solid var(--red);
    border-radius:4px;padding:28px;width:680px;max-width:96vw;max-height:92vh;
    overflow-y:auto;box-shadow:0 0 60px #cc220033;
  }
  .modal::-webkit-scrollbar{width:4px;}
  .modal::-webkit-scrollbar-thumb{background:var(--dark-red);}
  .modal-title{font-family:'Share Tech Mono',monospace;font-size:18px;color:var(--red2);
    letter-spacing:3px;text-transform:uppercase;margin-bottom:22px;
    border-bottom:1px solid var(--dark-red);padding-bottom:12px;}
  .form-section{margin-bottom:20px;}
  .form-section-title{font-size:11px;color:var(--red);letter-spacing:2px;text-transform:uppercase;
    margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--dark-red);}
  .form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
  .form-group{display:flex;flex-direction:column;gap:5px;}
  .form-group.full{grid-column:1/-1;}
  label{font-size:11px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;}
  input,textarea,select{
    background:var(--bg3);border:1px solid var(--dark-red);color:var(--text);
    padding:9px 12px;font-family:'Share Tech Mono',monospace;font-size:12px;
    outline:none;width:100%;transition:border-color .2s;border-radius:2px;
  }
  input:focus,textarea:focus{border-color:var(--red);}
  textarea{resize:vertical;min-height:70px;}
  .hint{font-size:10px;color:var(--muted);margin-top:3px;}

  /* GC FETCH & PICKER */
  .fetch-row{display:flex;gap:10px;align-items:flex-end;}
  .btn-fetch{
    background:transparent;border:1px solid var(--red);color:var(--red2);
    font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:1px;
    padding:9px 16px;cursor:pointer;white-space:nowrap;text-transform:uppercase;
    transition:all .2s;flex-shrink:0;
  }
  .btn-fetch:hover{background:var(--red);color:#fff;box-shadow:0 0 12px var(--red);}
  #fetch-status{font-size:11px;color:var(--muted);margin-top:6px;font-family:'Share Tech Mono',monospace;}

  .gc-picker{margin-top:10px;display:none;}
  .gc-picker-title{font-size:11px;color:var(--muted);letter-spacing:1px;margin-bottom:8px;text-transform:uppercase;}
  .gc-list{display:flex;flex-direction:column;gap:6px;max-height:200px;overflow-y:auto;}
  .gc-list::-webkit-scrollbar{width:4px;}
  .gc-list::-webkit-scrollbar-thumb{background:var(--dark-red);}
  .gc-item{
    display:flex;align-items:center;gap:10px;padding:8px 12px;
    border:1px solid var(--dark-red);background:var(--bg3);cursor:pointer;
    transition:border-color .15s;border-radius:2px;
  }
  .gc-item:hover{border-color:var(--red);}
  .gc-item.selected{border-color:var(--red2);background:#1a0005;}
  .gc-item input[type=checkbox]{width:auto;accent-color:var(--red2);}
  .gc-item-name{font-family:'Share Tech Mono',monospace;font-size:12px;flex:1;}
  .gc-item-id{font-size:10px;color:var(--muted);}
  .gc-count{font-size:11px;color:var(--amber);margin-top:6px;font-family:'Share Tech Mono',monospace;}

  /* MSGS */
  .msgs-wrap{display:flex;flex-direction:column;gap:8px;}
  .msg-row{display:flex;gap:8px;align-items:flex-start;}
  .msg-row textarea{flex:1;}
  .btn-icon{background:transparent;border:1px solid var(--dark-red);color:var(--muted);
    padding:8px 10px;cursor:pointer;font-size:14px;flex-shrink:0;transition:all .2s;}
  .btn-icon:hover{border-color:var(--red);color:var(--red);}
  .btn-add-msg{
    background:transparent;border:1px dashed var(--dark-red);color:var(--muted);
    font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:1px;
    padding:8px;cursor:pointer;width:100%;text-align:center;transition:all .2s;
  }
  .btn-add-msg:hover{border-color:var(--red);color:var(--red2);}

  /* MODAL FOOTER */
  .modal-footer{display:flex;gap:10px;margin-top:24px;justify-content:flex-end;
    border-top:1px solid var(--dark-red);padding-top:16px;}
  .btn-save{
    background:var(--red);border:none;color:#fff;
    font-family:'Share Tech Mono',monospace;font-size:13px;letter-spacing:2px;
    padding:11px 28px;cursor:pointer;text-transform:uppercase;transition:all .2s;
  }
  .btn-save:hover{background:var(--red2);box-shadow:0 0 20px var(--red);}
  .btn-cancel{
    background:transparent;border:1px solid var(--muted);color:var(--muted);
    font-family:'Share Tech Mono',monospace;font-size:13px;letter-spacing:1px;
    padding:11px 20px;cursor:pointer;text-transform:uppercase;transition:all .2s;
  }
  .btn-cancel:hover{border-color:var(--text);color:var(--text);}

  /* EMPTY */
  .empty{text-align:center;padding:60px 20px;color:var(--muted);font-family:'Share Tech Mono',monospace;}
  .empty-icon{font-size:40px;margin-bottom:12px;opacity:.3;}
  .empty-text{font-size:13px;letter-spacing:2px;text-transform:uppercase;}

  @media(max-width:600px){
    .form-grid{grid-template-columns:1fr;}
    .form-group.full{grid-column:auto;}
    .header-stats{gap:12px;}
    .acc-btns{flex-wrap:wrap;}
  }
</style>
</head>
<body>

<div class="header">
  <div class="logo">
    [ AURA DOWNER ]
  </div>
  <div class="header-stats">
    <div class="hstat"><div class="hstat-val" id="h-accounts">0</div><div class="hstat-lbl">Accounts</div></div>
    <div class="hstat"><div class="hstat-val" id="h-running">0</div><div class="hstat-lbl">Running</div></div>
    <div class="hstat"><div class="hstat-val" id="h-sent">0</div><div class="hstat-lbl">Total Sent</div></div>
  </div>
</div>

<div class="main">
  <button class="btn-add" onclick="openAddModal()">+ ADD ACCOUNT</button>
  <div id="accounts-wrap"></div>
</div>

<!-- MODAL -->
<div class="modal-overlay" id="modal">
<div class="modal">
  <div class="modal-title" id="modal-title">Add Account</div>

  <!-- ACCOUNT -->
  <div class="form-section">
    <div class="form-section-title">Account</div>
    <div class="form-grid">
      <div class="form-group"><label>Name</label><input type="text" id="f-name" placeholder="Account label"/></div>
      <div class="form-group"><label>Session ID</label><input type="text" id="f-sid" placeholder="sessionid cookie" autocomplete="off"/></div>
      <div class="form-group full"><label>Proxy <span style=\"color:var(--muted)\">(optional)</span></label><input type="text" id="f-proxy" placeholder="http://user:pass@ip:port"/></div>
    </div>
  </div>

  <!-- GC SELECTION -->
  <div class="form-section">
    <div class="form-section-title">Group Chats (Max 5)</div>
    <div class="fetch-row">
      <div class="form-group" style="flex:1">
        <label>Session ID for Fetch</label>
      </div>
      <button class="btn-fetch" onclick="fetchGroups()">⚡ FETCH GCs</button>
    </div>
    <div id="fetch-status"></div>
    <div class="gc-picker" id="gc-picker">
      <div class="gc-picker-title">Select up to 5 GCs</div>
      <div class="gc-list" id="gc-list"></div>
      <div class="gc-count" id="gc-count">0 / 5 selected</div>
    </div>
    <!-- Hidden field to store selected group IDs -->
    <textarea id="f-groups" style="display:none"></textarea>
  </div>

  <!-- NC TITLES -->
  <div class="form-section">
    <div class="form-section-title">NC Titles</div>
    <div class="form-grid">
      <div class="form-group full">
        <label>Titles (comma separated)</label>
        <input type="text" id="f-titles" placeholder="Title1, Title2, Title3"/>
        <div class="hint">NC will rotate through these titles</div>
      </div>
      <div class="form-group">
        <label>NC Every (minutes)</label>
        <input type="number" id="f-nc-every" value="0" min="0"/>
        <div class="hint">0 = every round</div>
      </div>
    </div>
  </div>

  <!-- MESSAGES -->
  <div class="form-section">
    <div class="form-section-title">Messages (Round Robin)</div>
    <div class="msgs-wrap" id="msgs-wrap"></div>
    <button class="btn-add-msg" onclick="addMsgField()">+ ADD MESSAGE</button>
  </div>

  <!-- DELAYS -->
  <div class="form-section">
    <div class="form-section-title">Delays</div>
    <div class="form-grid">
      <div class="form-group">
        <label>Min Delay Between Messages (s)</label>
        <input type="number" id="f-msg-min" value="2" min="0" step="0.5"/>
      </div>
      <div class="form-group">
        <label>Max Delay Between Messages (s)</label>
        <input type="number" id="f-msg-max" value="5" min="0" step="0.5"/>
      </div>
      <div class="form-group">
        <label>Cooldown After N Rounds</label>
        <input type="number" id="f-cooldown-after" value="0" min="0"/>
        <div class="hint">0 = disabled</div>
      </div>
      <div class="form-group">
        <label>Cooldown Duration (minutes)</label>
        <input type="number" id="f-cooldown-dur" value="5" min="1"/>
      </div>
    </div>
  </div>

  <div class="modal-footer">
    <button class="btn-cancel" onclick="closeModal()">CANCEL</button>
    <button class="btn-save" onclick="saveAccount()">SAVE</button>
  </div>
</div>
</div>

<script>
let accounts = {};
let editingId = null;
let fetchedGroups = [];
let selectedGCs = []; // [{id, name}]

// ── FETCH GCs ──────────────────────────────────────────────
async function fetchGroups() {
  const sid = document.getElementById('f-sid').value.trim();
  if (!sid) { alert('Enter Session ID first'); return; }
  const statusEl = document.getElementById('fetch-status');
  statusEl.textContent = '⚡ Fetching...';
  statusEl.style.color = '#ff8800';
  try {
    const r = await fetch('/api/fetch-groups', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({session_id: sid})
    });
    const d = await r.json();
    if (d.groups && d.groups.length > 0) {
      fetchedGroups = d.groups;
      statusEl.textContent = `✅ ${d.groups.length} GCs found`;
      statusEl.style.color = '#00cc44';
      renderGCPicker();
    } else {
      statusEl.textContent = '⚠️ No GCs found';
      statusEl.style.color = '#ff8800';
    }
  } catch(e) {
    statusEl.textContent = `❌ Error: ${e.message}`;
    statusEl.style.color = '#cc2200';
  }
}

function renderGCPicker() {
  const picker = document.getElementById('gc-picker');
  const list = document.getElementById('gc-list');
  picker.style.display = 'block';
  list.innerHTML = '';
  fetchedGroups.forEach(g => {
    const isSelected = selectedGCs.some(s => s.id === g.id);
    const item = document.createElement('div');
    item.className = 'gc-item' + (isSelected ? ' selected' : '');
    item.innerHTML = `
      <input type="checkbox" ${isSelected ? 'checked' : ''} data-id="${g.id}" data-name="${g.name}"/>
      <span class="gc-item-name">${g.name}</span>
      <span class="gc-item-id">${g.id}</span>
    `;
    const cb = item.querySelector('input');
    cb.addEventListener('change', () => toggleGC(g.id, g.name, cb, item));
    list.appendChild(item);
  });
  updateGCCount();
}

function toggleGC(id, name, cb, item) {
  if (cb.checked) {
    if (selectedGCs.length >= 5) {
      cb.checked = false;
      alert('Max 5 GCs allowed');
      return;
    }
    selectedGCs.push({id, name});
    item.classList.add('selected');
  } else {
    selectedGCs = selectedGCs.filter(s => s.id !== id);
    item.classList.remove('selected');
  }
  updateGCCount();
  syncGroupsField();
}

function updateGCCount() {
  document.getElementById('gc-count').textContent = `${selectedGCs.length} / 5 selected`;
}

function syncGroupsField() {
  document.getElementById('f-groups').value = selectedGCs.map(s => s.id).join('\n');
}

// ── MESSAGES ───────────────────────────────────────────────
function addMsgField(val = '') {
  const wrap = document.getElementById('msgs-wrap');
  const row = document.createElement('div');
  row.className = 'msg-row';
  row.innerHTML = `
    <textarea placeholder="Message text..." rows="3">${val}</textarea>
    <button class="btn-icon" onclick="this.parentElement.remove()">✕</button>
  `;
  wrap.appendChild(row);
}

function getMsgs() {
  return [...document.querySelectorAll('#msgs-wrap textarea')]
    .map(t => t.value.trim()).filter(Boolean);
}

function setMsgs(raw) {
  document.getElementById('msgs-wrap').innerHTML = '';
  const parts = raw.split('---MSG---').map(s => s.trim()).filter(Boolean);
  if (parts.length === 0) { addMsgField(); return; }
  parts.forEach(p => addMsgField(p));
}

// ── MODAL ──────────────────────────────────────────────────
function openAddModal() {
  editingId = null;
  fetchedGroups = [];
  selectedGCs = [];
  document.getElementById('modal-title').textContent = 'Add Account';
  document.getElementById('f-name').value = '';
  document.getElementById('f-sid').value = '';
  document.getElementById('f-proxy').value = '';
  document.getElementById('f-titles').value = '';
  document.getElementById('f-nc-every').value = '0';
  document.getElementById('f-msg-min').value = '2';
  document.getElementById('f-msg-max').value = '5';
  document.getElementById('f-cooldown-after').value = '0';
  document.getElementById('f-cooldown-dur').value = '5';
  document.getElementById('f-groups').value = '';
  document.getElementById('gc-picker').style.display = 'none';
  document.getElementById('gc-list').innerHTML = '';
  document.getElementById('gc-count').textContent = '0 / 5 selected';
  document.getElementById('fetch-status').textContent = '';
  setMsgs('');
  document.getElementById('modal').classList.add('open');
}

function openEditModal(id) {
  editingId = id;
  fetchedGroups = [];
  const acc = accounts[id];

  // Restore selectedGCs from saved groups
  selectedGCs = [];
  const savedGroups = acc.groups ? acc.groups.split('\n').filter(Boolean) : [];
  const savedNames  = acc.group_names ? acc.group_names.split('\n').filter(Boolean) : [];
  savedGroups.forEach((gid, i) => {
    selectedGCs.push({id: gid.trim(), name: savedNames[i] || gid.trim()});
  });

  document.getElementById('modal-title').textContent = 'Edit Account';
  document.getElementById('f-name').value = acc.name || '';
  document.getElementById('f-sid').value = acc.session_id || '';
  document.getElementById('f-proxy').value = acc.proxy || '';
  document.getElementById('f-titles').value = acc.nc_titles || '';
  document.getElementById('f-nc-every').value = acc.nc_every_min || '0';
  document.getElementById('f-msg-min').value = acc.msg_delay_min || '2';
  document.getElementById('f-msg-max').value = acc.msg_delay_max || '5';
  document.getElementById('f-cooldown-after').value = acc.cooldown_after || '0';
  document.getElementById('f-cooldown-dur').value = acc.cooldown_dur || '5';
  document.getElementById('f-groups').value = savedGroups.join('\n');
  document.getElementById('fetch-status').textContent = '';

  // Show picker with saved GCs as selected
  if (selectedGCs.length > 0) {
    fetchedGroups = selectedGCs.map(s => ({id: s.id, name: s.name}));
    renderGCPicker();
  } else {
    document.getElementById('gc-picker').style.display = 'none';
  }

  setMsgs(acc.messages || acc.message || '');
  document.getElementById('modal').classList.add('open');
}

function closeModal() {
  document.getElementById('modal').classList.remove('open');
  editingId = null;
}

// ── SAVE ───────────────────────────────────────────────────
async function saveAccount() {
  const msgs = getMsgs();
  if (!msgs.length) { alert('Add at least one message'); return; }

  const body = {
    name:            document.getElementById('f-name').value.trim(),
    session_id:      document.getElementById('f-sid').value.trim(),
    proxy:           document.getElementById('f-proxy').value.trim(),
    groups:          selectedGCs.map(s => s.id).join('\n'),
    group_names:     selectedGCs.map(s => s.name).join('\n'),
    nc_titles:       document.getElementById('f-titles').value.trim(),
    nc_every_min:    document.getElementById('f-nc-every').value,
    messages:        msgs.join('---MSG---'),
    msg_delay_min:   document.getElementById('f-msg-min').value,
    msg_delay_max:   document.getElementById('f-msg-max').value,
    cooldown_after:  document.getElementById('f-cooldown-after').value,
    cooldown_dur:    document.getElementById('f-cooldown-dur').value,
  };

  if (!body.name) { alert('Enter account name'); return; }
  if (editingId && !body.session_id) delete body.session_id;

  const url    = editingId ? `/api/accounts/${editingId}` : '/api/accounts';
  const method = editingId ? 'PUT' : 'POST';
  const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const d = await r.json();
  if (d.success) { closeModal(); loadAccounts(); }
  else alert(d.error || 'Save failed');
}

// ── CONTROLS ───────────────────────────────────────────────
async function startBot(id) {
  const r = await fetch(`/api/accounts/${id}/start`, {method:'POST'});
  const d = await r.json();
  if (!d.success) alert(d.error || 'Start failed');
}

async function stopBot(id) {
  await fetch(`/api/accounts/${id}/stop`, {method:'POST'});
}

async function deleteAcc(id) {
  if (!confirm('Delete this account?')) return;
  await fetch(`/api/accounts/${id}`, {method:'DELETE'});
  loadAccounts();
}

function toggleLogs(id) {
  const el = document.getElementById(`log-panel-${id}`);
  if (el) el.classList.toggle('open');
}

function toggleCollapse(id) {
  const el = document.getElementById(`body-${id}`);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}

// ── RENDER ─────────────────────────────────────────────────
function fmtTime(secs) {
  if (!secs || secs < 0) return '--:--:--';
  const h = Math.floor(secs/3600);
  const m = Math.floor((secs%3600)/60);
  const s = Math.floor(secs%60);
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

function renderAccounts(data) {
  const wrap = document.getElementById('accounts-wrap');
  const ids = Object.keys(data);

  if (ids.length === 0) {
    wrap.innerHTML = `<div class="empty"><div class="empty-icon">☠</div><div class="empty-text">No accounts — add one to begin</div></div>`;
    return;
  }

  let totalRunning = 0, totalSent = 0;
  ids.forEach(id => {
    const st = data[id].status || {};
    if (st.running) totalRunning++;
    totalSent += st.sent || 0;
  });
  document.getElementById('h-accounts').textContent = ids.length;
  document.getElementById('h-running').textContent  = totalRunning;
  document.getElementById('h-sent').textContent     = totalSent;

  ids.forEach(id => {
    const acc = data[id];
    const st  = acc.status || {};
    const isRunning = st.running;
    const isCooldown = st.cooldown;
    const runtime = st.started_at ? fmtTime(Date.now()/1000 - st.started_at) : '--:--:--';

    const dotCls = isCooldown ? 'dot-cooldown' : (isRunning ? 'dot-on' : 'dot-off');
    const gcNames = acc.group_names ? acc.group_names.split('\n').filter(Boolean) : [];

    let existing = document.getElementById(`card-${id}`);
    if (!existing) {
      existing = document.createElement('div');
      existing.className = 'acc-card';
      existing.id = `card-${id}`;
      wrap.appendChild(existing);
    }

    existing.innerHTML = `
      <div class="acc-header">
        <div class="status-dot ${dotCls}"></div>
        <div class="acc-name">${acc.name || id}</div>
        <div class="acc-runtime">${runtime}</div>
        <div class="acc-btns">
          ${isRunning
            ? `<button class="btn btn-stop" onclick="stopBot('${id}')">■ STOP</button>`
            : `<button class="btn btn-start" onclick="startBot('${id}')">▶ START</button>`}
          <button class="btn btn-logs" onclick="toggleLogs('${id}')">LOGS</button>
          <button class="btn btn-edit" onclick="openEditModal('${id}')">EDIT</button>
          <button class="btn btn-del" onclick="deleteAcc('${id}')">✕</button>
          <button class="btn-collapse" onclick="toggleCollapse('${id}')">▲</button>
        </div>
      </div>
      <div id="body-${id}">
        <div class="stats-row">
          <div class="stat"><div class="stat-val c-green">${st.sent||0}</div><div class="stat-lbl">Sent</div></div>
          <div class="stat"><div class="stat-val c-red">${st.failed||0}</div><div class="stat-lbl">Failed</div></div>
          <div class="stat"><div class="stat-val c-purple">${st.nc_done||0}</div><div class="stat-lbl">NC Done</div></div>
          <div class="stat"><div class="stat-val c-red">${st.nc_failed||0}</div><div class="stat-lbl">NC Fail</div></div>
          <div class="stat"><div class="stat-val c-blue">${st.round||0}</div><div class="stat-lbl">Rounds</div></div>
          <div class="stat"><div class="stat-val c-amber">${st.gcs_done||0}<span style="color:var(--muted);font-size:12px"> / ${st.total_gcs||0}</span></div><div class="stat-lbl">GCs</div></div>
        </div>
        ${gcNames.length ? `
        <div class="gc-row">
          <span class="gc-label">GCs</span>
          ${gcNames.map(n=>`<span class="gc-pill">${n}</span>`).join('')}
        </div>` : ''}
        <div class="info-row">
          <div class="info-item"><span class="info-key">Delay</span><span class="info-val">${acc.msg_delay_min||2}s – ${acc.msg_delay_max||5}s</span></div>
          ${acc.cooldown_after > 0 ? `<div class="info-item"><span class="info-key">Cooldown</span><span class="info-val">Every ${acc.cooldown_after} rounds → ${acc.cooldown_dur} min</span></div>` : ''}
          ${acc.nc_titles ? `<div class="info-item"><span class="info-key">NC</span><span class="info-val">${acc.nc_titles.split(',').length} titles${acc.nc_every_min > 0 ? ` / ${acc.nc_every_min}min` : ''}</span></div>` : ''}
        </div>
        <div class="last-action">▸ <span>${st.last_action||'Idle'}</span>${isCooldown?' 😴 COOLDOWN':''}</div>
        <div class="log-panel" id="log-panel-${id}">
          <div class="log-header">
            <span class="log-title">📟 CONSOLE LOG</span>
            <span class="log-live">● LIVE</span>
          </div>
          <div class="log-box" id="log-box-${id}"></div>
        </div>
      </div>
    `;
  });

  // Remove deleted
  wrap.querySelectorAll('.acc-card').forEach(el => {
    if (!data[el.id.replace('card-','')]) el.remove();
  });
}

function colorLog(line) {
  if (line.includes('✅') || line.includes('✓')) return 'ok';
  if (line.includes('❌') || line.includes('failed') || line.includes('Failed')) return 'err';
  if (line.includes('⚠️')) return 'warn';
  if (line.includes('🔄') || line.includes('Round')) return 'round';
  if (line.includes('💤') || line.includes('⏭') || line.includes('😴')) return 'info';
  return '';
}

// ── POLL ───────────────────────────────────────────────────
async function loadAccounts() {
  const r = await fetch('/api/accounts');
  accounts = await r.json();
  renderAccounts(accounts);
}

async function pollLogs() {
  const openPanels = document.querySelectorAll('.log-panel.open');
  for (const panel of openPanels) {
    const id = panel.id.replace('log-panel-','');
    try {
      const r = await fetch(`/api/accounts/${id}/logs`);
      const d = await r.json();
      const box = document.getElementById(`log-box-${id}`);
      if (box && d.logs) {
        const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
        box.innerHTML = d.logs.map(l => `<div class="log-line ${colorLog(l)}">${l}</div>`).join('');
        if (atBottom) box.scrollTop = box.scrollHeight;
      }
    } catch(e) {}
  }
}

async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    const st = await r.json();
    Object.keys(st).forEach(id => {
      if (accounts[id]) accounts[id].status = st[id];
    });
    renderAccounts(accounts);
  } catch(e) {}
}

loadAccounts();
setInterval(pollStatus, 2000);
setInterval(pollLogs, 1500);

document.getElementById('modal').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});
</script>
</body>
</html>"""

# ── API ROUTES ──────────────────────────────────────────────

@app.route("/")
def index():
    return HTML

@app.route("/api/accounts")
def get_accounts():
    with data_lock:
        d = load_data()
    result = {}
    for acc_id, acc in d.get("accounts", {}).items():
        st = bot_status.get(acc_id, {"running": False})
        result[acc_id] = {
            "name":           acc.get("name", ""),
            "session_id":     acc.get("session_id", ""),
            "proxy":          acc.get("proxy", ""),
            "groups":         acc.get("groups", ""),
            "group_names":    acc.get("group_names", ""),
            "nc_titles":      acc.get("nc_titles", ""),
            "nc_every_min":   acc.get("nc_every_min", 0),
            "messages":       acc.get("messages", ""),
            "msg_delay_min":  acc.get("msg_delay_min", 2),
            "msg_delay_max":  acc.get("msg_delay_max", 5),
            "cooldown_after": acc.get("cooldown_after", 0),
            "cooldown_dur":   acc.get("cooldown_dur", 5),
            "status": st
        }
    return jsonify(result)

@app.route("/api/accounts", methods=["POST"])
def add_account():
    body = request.json
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "error": "Session ID required"}), 400
    try:
        cl = Client()
        cl.login_by_sessionid(session_id)
    except Exception as e:
        return jsonify({"success": False, "error": f"Login failed: {e}"}), 400

    acc_id = str(int(time.time() * 1000))
    entry = {
        "name":           body.get("name", ""),
        "session_id":     session_id,
        "proxy":          body.get("proxy", ""),
        "groups":         body.get("groups", ""),
        "group_names":    body.get("group_names", ""),
        "nc_titles":      body.get("nc_titles", ""),
        "nc_every_min":   body.get("nc_every_min", 0),
        "messages":       body.get("messages", ""),
        "msg_delay_min":  body.get("msg_delay_min", 2),
        "msg_delay_max":  body.get("msg_delay_max", 5),
        "cooldown_after": body.get("cooldown_after", 0),
        "cooldown_dur":   body.get("cooldown_dur", 5),
    }
    with data_lock:
        d = load_data()
        d["accounts"][acc_id] = entry
        save_data(d)
    return jsonify({"success": True, "id": acc_id})

@app.route("/api/accounts/<acc_id>", methods=["PUT"])
def update_account(acc_id):
    body = request.json
    with data_lock:
        d = load_data()
        if acc_id not in d["accounts"]:
            return jsonify({"success": False, "error": "Not found"}), 404
        acc = d["accounts"][acc_id]
        for k in ["name", "proxy", "groups", "group_names", "nc_titles", "nc_every_min",
                  "messages", "msg_delay_min", "msg_delay_max", "cooldown_after", "cooldown_dur"]:
            if k in body: acc[k] = body[k]
        if body.get("session_id"):
            acc["session_id"] = body["session_id"]
            ig_clients.pop(acc_id, None)
        save_data(d)
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
        bot_threads[acc_id].join(timeout=5)
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

@app.route("/api/accounts/<acc_id>/logs")
def get_logs(acc_id):
    logs = list(bot_logs.get(acc_id, []))
    return jsonify({"logs": logs})

@app.route("/api/status")
def all_status():
    return jsonify({acc_id: dict(st) for acc_id, st in bot_status.items()})

@app.route("/api/fetch-groups", methods=["POST"])
def fetch_groups():
    body = request.json
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "error": "Session ID required"}), 400
    try:
        cl = Client()
        cl.login_by_sessionid(session_id)
        threads = cl.direct_threads(amount=50)
        groups = []
        for t in threads:
            if t.is_group:
                groups.append({"id": str(t.id), "name": t.thread_title or str(t.id)})
        return jsonify({"success": True, "groups": groups})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)