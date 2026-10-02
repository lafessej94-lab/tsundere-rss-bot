# @title 🖥 Tsundere RSS Bot — Launcher
BOT_TOKEN       = ""   # @param {type: "string"}
CHANNEL_ID      = ""   # @param {type: "string"}  — ex: -1001234567890
CREATOR_ID      = 0    # @param {type: "integer"}
RSS_URL         = ""   # @param {type: "string"}
TELEGRAM_API_ID   = 0  # @param {type: "integer"}  — my.telegram.org
TELEGRAM_API_HASH = "" # @param {type: "string"}
ARIA2_SECRET    = "change-me"  # @param {type: "string"}
CHECK_INTERVAL  = 60   # @param {type: "integer"}
REPO_URL        = "https://github.com/TON_USER/tsundere-rss-bot.git"  # @param {type: "string"}
GITHUB_TOKEN    = ""   # @param {type: "string"}  — seulement si le repo est privé
USE_DRIVE       = True # @param {type: "boolean"}  — garde la base + le binaire compilé sur Google Drive

MAX_RESTARTS = 50  # @param {type: "integer"}

import subprocess, time, shutil, os, sys, re, socket

print("🌸 Tsundere RSS Bot — Launcher")
print("─" * 40)

APP_DIR = "/content/tsundere-rss-bot"


def step(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")
    sys.stdout.flush()


def run(cmd, **kw):
    return subprocess.run(cmd, shell=True, **kw)


# ── Vérification des paramètres ──────────────────────────────────────
missing = [n for n, v in {
    "BOT_TOKEN": BOT_TOKEN, "CHANNEL_ID": CHANNEL_ID, "CREATOR_ID": CREATOR_ID,
    "RSS_URL": RSS_URL, "TELEGRAM_API_ID": TELEGRAM_API_ID,
    "TELEGRAM_API_HASH": TELEGRAM_API_HASH,
}.items() if not v]
if missing:
    step(f"❌ Paramètres manquants : {', '.join(missing)}")
    sys.exit(1)

# ── Google Drive (persistance) ───────────────────────────────────────
PERSIST = "/content/tsundere_data"
if USE_DRIVE:
    from google.colab import drive
    drive.mount("/content/drive")
    PERSIST = "/content/drive/MyDrive/tsundere_rss_bot"
    step("💾 Drive monté — base et binaire sauvegardés dans " + PERSIST)
os.makedirs(PERSIST, exist_ok=True)

# ── Nettoyage / clone ────────────────────────────────────────────────
if os.path.exists("/content/sample_data"):
    shutil.rmtree("/content/sample_data")

if os.path.exists(APP_DIR):
    step("🧹 Ancien dossier détecté — suppression avant re-clone")
    shutil.rmtree(APP_DIR)

clone_url = REPO_URL
if GITHUB_TOKEN:
    clone_url = REPO_URL.replace("https://", f"https://{GITHUB_TOKEN}@")

step("📥 Clonage du repo...")
if run(f"git clone -q {clone_url} {APP_DIR}").returncode != 0:
    step("❌ Échec du git clone — vérifie REPO_URL (ou GITHUB_TOKEN si repo privé).")
    sys.exit(1)
step("✅ Repo cloné")

# ── Paquets système ──────────────────────────────────────────────────
step("📦 Installation de ffmpeg + aria2 (apt)...")
run("apt update -qq && apt install -y -qq ffmpeg aria2")
if shutil.which("ffmpeg") is None or shutil.which("aria2c") is None:
    step("❌ ffmpeg ou aria2 n'a pas pu s'installer.")
    sys.exit(1)
step("✅ ffmpeg + aria2c installés")

# ── Dépendances Python ───────────────────────────────────────────────
step("📦 Installation des dépendances Python...")
if run(f"pip3 install -q -r {APP_DIR}/requirements.txt").returncode != 0:
    step("❌ Échec de l'installation des dépendances Python.")
    sys.exit(1)
step("✅ Dépendances Python installées")

# ── telegram-bot-api (compilé une seule fois si Drive) ───────────────
BIN = f"{PERSIST}/telegram-bot-api"
if not os.path.exists(BIN):
    step("🔨 Compilation de telegram-bot-api (10-20 min, une seule fois avec Drive)...")
    run("apt install -y -qq make git zlib1g-dev libssl-dev gperf cmake g++")
    run("rm -rf /tmp/tgbotapi && git clone --recursive -q "
        "https://github.com/tdlib/telegram-bot-api.git /tmp/tgbotapi")
    run("mkdir -p /tmp/tgbotapi/build && cd /tmp/tgbotapi/build && "
        "cmake -DCMAKE_BUILD_TYPE=Release .. > /dev/null && "
        "cmake --build . --target telegram-bot-api -j$(nproc) > /dev/null")
    built = "/tmp/tgbotapi/build/telegram-bot-api"
    if not os.path.exists(built):
        step("❌ Compilation de telegram-bot-api échouée.")
        sys.exit(1)
    shutil.copy(built, BIN)
    os.chmod(BIN, 0o755)
    step("✅ telegram-bot-api compilé")
else:
    step("✅ telegram-bot-api déjà compilé (cache)")

# ── Services : aria2 + telegram-bot-api ──────────────────────────────
run("pkill -f telegram-bot-api; pkill -f 'aria2c --enable-rpc'")
time.sleep(1)
os.makedirs("/content/tgbotapi-data", exist_ok=True)
os.makedirs(f"{APP_DIR}/downloads", exist_ok=True)

run(f"aria2c --enable-rpc --rpc-listen-port=6800 --rpc-secret={ARIA2_SECRET} "
    "--max-connection-per-server=16 --split=16 --continue=true --daemon=true")
step("🚀 aria2 lancé (RPC :6800)")

api_log = open("/content/tgbotapi.log", "w")
api_proc = subprocess.Popen(
    [BIN, f"--api-id={TELEGRAM_API_ID}", f"--api-hash={TELEGRAM_API_HASH}",
     "--local", "--http-ip-address=127.0.0.1", "--http-port=8081",
     "--dir=/content/tgbotapi-data"],
    stdout=api_log, stderr=subprocess.STDOUT,
)
for _ in range(20):
    try:
        socket.create_connection(("127.0.0.1", 8081), timeout=1).close()
        break
    except OSError:
        time.sleep(1)
else:
    step("❌ telegram-bot-api ne répond pas — voir /content/tgbotapi.log")
    sys.exit(1)
step("🚀 telegram-bot-api lancé (:8081)")

# ── Environnement du bot ─────────────────────────────────────────────
env = os.environ.copy()
env.update({
    "BOT_TOKEN": BOT_TOKEN,
    "CHANNEL_ID": str(CHANNEL_ID),
    "CREATOR_ID": str(CREATOR_ID),
    "RSS_URL": RSS_URL,
    "ARIA2_SECRET": ARIA2_SECRET,
    "CHECK_INTERVAL": str(CHECK_INTERVAL),
    "DB_FILE": f"{PERSIST}/rss_bot.db",
    "DOWNLOAD_DIR": f"{APP_DIR}/downloads",
})

# ── Boucle de démarrage / auto-restart ───────────────────────────────
flood_re = re.compile(r"(?:FLOOD_WAIT_SECONDS=(\d+)|A wait of (\d+) seconds is required|Retry in (\d+))")
restart_count = 0

step("🚀 Démarrage du bot (python3 bot.py)...")
print("─" * 40)

while restart_count < MAX_RESTARTS:
    if api_proc.poll() is not None:
        step("⚠️ telegram-bot-api s'est arrêté — relance la cellule.")
        break

    start = time.time()
    proc = subprocess.Popen(
        ["python3", "bot.py"], cwd=APP_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    captured = []
    try:
        while True:
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if line:
                print(line, end="")
                sys.stdout.flush()
                captured.append(line)
                if len(captured) > 200:
                    captured = captured[-100:]
    finally:
        if proc.stdout:
            proc.stdout.close()

    return_code = proc.wait()

    if return_code == 0:
        step("✅ Bot arrêté proprement.")
        break

    if time.time() - start > 300:
        restart_count = 0
    restart_count += 1

    wait = min(5 * restart_count, 30)
    for line in reversed(captured):
        m = flood_re.search(line)
        if m:
            wait = int(next(g for g in m.groups() if g)) + 5
            step(f"⏳ FloodWait détecté — attente {wait}s")
            break

    step(f"⚠️ Bot arrêté (code {return_code}). Redémarrage dans {wait}s [{restart_count}/{MAX_RESTARTS}]")
    print("─" * 40)
    time.sleep(wait)
else:
    step("❌ Trop de redémarrages, arrêt du script.")
