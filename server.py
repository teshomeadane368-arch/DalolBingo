import hashlib, hmac, json, math, os, random, secrets, threading, time, urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import psycopg2
from psycopg2.extras import DictCursor
from contextlib import contextmanager
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
import telebot
from telebot.types import InlineKeyboardButton as IBtn, InlineKeyboardMarkup as IKB, KeyboardButton as KBtn, ReplyKeyboardMarkup as RKB, WebAppInfo

TOKEN = os.environ.get('BOT_TOKEN', '').strip().strip("'\"")
try: ADMIN_ID = int(os.environ.get('ADMIN_ID', '0').strip().strip("'\"") or '0')
except Exception: ADMIN_ID = 0

DEFAULT_URL = 'https://your-domain.example'
APP_URL = (os.environ.get('APP_URL') or DEFAULT_URL).strip().strip("'\"").rstrip('/')
APP_URL = ('https://' + APP_URL[8:]) if APP_URL.lower().startswith('https://') else DEFAULT_URL

SUPPORT = os.environ.get('SUPPORT_USERNAME', '@your_support_username')
BOT_USERNAME = os.environ.get('BOT_USERNAME', '').strip().lstrip('@')
TB_NAME = os.environ.get('TELEBIRR_NAME', 'DEMO ONLY')
TB_PHONE = os.environ.get('TELEBIRR_PHONE', '')
CB_NAME = os.environ.get('CBE_BIRR_NAME', TB_NAME)
CB_PHONE = os.environ.get('CBE_BIRR_PHONE', '')
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
HOUSE_CUT, CD_SEC, CALL_SEC, WIN_SEC = 0.20, 25, 3.0, 10
MIN_PLAYERS = int(os.environ.get('MIN_PLAYERS', '2') or '2')
db_lock, room_lock = threading.RLock(), threading.RLock()

class CursorProxy:
  def __init__(self, cursor):
    self.cursor = cursor
    self.lastrowid = None
  def execute(self, sql, params=None):
    sql = sql
    is_request_insert = sql.lstrip().upper().startswith('INSERT INTO REQUESTS ')
    if is_request_insert and 'RETURNING' not in sql.upper():
      sql = sql.rstrip().rstrip(';') + ' RETURNING id'
    self.cursor.execute(sql, params or ())
    if is_request_insert:
      row = self.cursor.fetchone()
      self.lastrowid = row['id'] if row else None
    return self
  @property
  def rowcount(self): return self.cursor.rowcount
  def fetchone(self): return self.cursor.fetchone()
  def fetchall(self): return self.cursor.fetchall()
  def __iter__(self): return iter(self.cursor)

class DBConnection:
  def __init__(self, connection): self.connection = connection
  def execute(self, sql, params=None):
    cur = self.connection.cursor()
    return CursorProxy(cur).execute(sql, params)
  def commit(self): self.connection.commit()
  def rollback(self): self.connection.rollback()
  def close(self): self.connection.close()

@contextmanager
def get_db():
  if not DATABASE_URL:
    raise RuntimeError('DATABASE_URL is missing. Add the Neon connection string in Render Environment.')
  with db_lock:
    raw = psycopg2.connect(DATABASE_URL, sslmode='require', connect_timeout=15, cursor_factory=DictCursor)
    c = DBConnection(raw)
    try:
      yield c
      raw.commit()
    except Exception:
      raw.rollback()
      raise
    finally:
      raw.close()

def init_db():
  with get_db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS users (
      uid BIGINT PRIMARY KEY, name TEXT DEFAULT 'Player', phone TEXT,
      play_wallet DOUBLE PRECISION DEFAULT 0, win_wallet DOUBLE PRECISION DEFAULT 0,
      matches_played INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
      total_won DOUBLE PRECISION DEFAULT 0, referred_by BIGINT, created_at BIGINT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
      id BIGSERIAL PRIMARY KEY, uid BIGINT, icon TEXT, title TEXT,
      amount DOUBLE PRECISION, created_at BIGINT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS requests (
      id BIGSERIAL PRIMARY KEY, req_type TEXT, uid BIGINT,
      amount DOUBLE PRECISION, method TEXT, details TEXT,
      status TEXT DEFAULT 'pending', created_at BIGINT, reference TEXT)""")
    c.execute("""ALTER TABLE requests ADD COLUMN IF NOT EXISTS reference TEXT""")
    c.execute("""CREATE TABLE IF NOT EXISTS house_stats (
      id INTEGER PRIMARY KEY CHECK (id=1),
      total_commission DOUBLE PRECISION DEFAULT 0,
      total_rounds BIGINT DEFAULT 0)""")
    c.execute("INSERT INTO house_stats (id,total_commission,total_rounds) VALUES (1,0,0) ON CONFLICT (id) DO NOTHING")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_requests_reference ON requests(reference) WHERE reference IS NOT NULL AND reference <> ''")
    c.execute("""CREATE TABLE IF NOT EXISTS bonus_claims (
      id BIGSERIAL PRIMARY KEY, uid BIGINT NOT NULL, bonus_key TEXT NOT NULL,
      amount DOUBLE PRECISION NOT NULL, claimed_at BIGINT NOT NULL,
      UNIQUE(uid, bonus_key))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bonus_claims_key ON bonus_claims(bonus_key)")

def ensure_user(uid, name='Player', ref=None):
  with get_db() as c:
    r = c.execute('SELECT * FROM users WHERE uid=%s', (uid,)).fetchone()
    if not r:
      c.execute('INSERT INTO users (uid,name,play_wallet,win_wallet,referred_by,created_at) VALUES (%s,%s,0,0,%s,%s)', (uid, name or 'Player', ref, int(time.time())))
      r = c.execute('SELECT * FROM users WHERE uid=%s', (uid,)).fetchone()
    elif name and name != 'Player' and r['name'] != name:
      c.execute('UPDATE users SET name=%s WHERE uid=%s', (name, uid))
      r = c.execute('SELECT * FROM users WHERE uid=%s', (uid,)).fetchone()
    return dict(r)

def add_tx(c, uid, ic, title, amt):
  c.execute('INSERT INTO transactions (uid,icon,title,amount,created_at) VALUES (%s,%s,%s,%s,%s)', (uid, ic, title, float(amt), int(time.time())))

def gen_card(no):
  rng = random.SystemRandom()
  cols = [rng.sample(range(a, b + 1), 5) for a, b in [(1,15),(16,30),(31,45),(46,60),(61,75)]]
  return ['FREE' if (r == 2 and c == 2) else cols[c][r] for r in range(5) for c in range(5)]

WIN_LINES = [[0,1,2,3,4],[5,6,7,8,9],[10,11,12,13,14],[15,16,17,18,19],[20,21,22,23,24],
             [0,5,10,15,20],[1,6,11,16,21],[2,7,12,17,22],[3,8,13,18,23],[4,9,14,19,24],
             [0,6,12,18,24],[4,8,12,16,20],[0,4,20,24]]
check_win = lambda g, cs: next((ln for ln in WIN_LINES if all(g[i] == 'FREE' or g[i] in cs for i in ln)), None)
TEST_BOTS_ENABLED = os.environ.get('TEST_BOTS_ENABLED', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
BOT_NAMES = [
  'Abebe K.', 'Hana M.', 'Dawit T.', 'Mimi E.', 'Liya G.', 'Kebede A.',
  'Selam B.', 'Biruk D.', 'Meron T.', 'Nahom K.', 'Rahel M.', 'Yonas G.',
  'Tigist A.', 'Fikru B.', 'Sami H.', 'Mekdes T.', 'Henok D.', 'Eden K.',
  'Solomon M.', 'Betty A.', 'Daniel G.', 'Marta S.', 'Natnael B.', 'Almaz K.',
  'Bereket T.', 'Hirut D.', 'Abel M.', 'Mahi A.', 'Robel G.', 'Sara B.',
  'Yared K.', 'Genet T.', 'Mulu D.', 'Fitsum A.', 'Elsa M.', 'Kaleb S.',
  'Aster G.', 'Tewodros B.', 'Rahel D.', 'Nardos K.'
]
mk_room = lambda s: {'stake': s, 'phase': 'WAITING', 'countdown': CD_SEC, 'cards': {}, 'called': [], 'deck': [], 'tick': time.time(), 'winner': None, 'win_until': 0.0, 'bot_target': random.randint(30, 40), 'bot_names': [], 'next_bot_at': time.time() + random.uniform(0.5, 1.5)}
rooms = {10: mk_room(10), 20: mk_room(20), 50: mk_room(50)}
n_players = lambda r: len({v['uid'] for v in r['cards'].values()})
human_cards = lambda r: sum(1 for v in r['cards'].values() if not v.get('is_bot'))
total_paid_cards = lambda r: len(r['cards'])
derash = lambda r: round(total_paid_cards(r) * r['stake'] * (1.0 - HOUSE_CUT), 2)

def add_one_test_bot(r):
  if not TEST_BOTS_ENABLED or sum(1 for v in r['cards'].values() if v.get('is_bot')) >= r['bot_target']:
    return
  available = [n for n in range(1, 401) if n not in r['cards']]
  used_names = r.get('bot_names', [])
  names = [name for name in BOT_NAMES if name not in used_names]
  if not available or not names:
    r['bot_target'] = sum(1 for v in r['cards'].values() if v.get('is_bot'))
    return
  cno = random.choice(available)
  name = random.choice(names)
  bot_credits = random.randint(500, 2000)
  bot_credits -= r['stake']  # Simulated bot wallet pays the same card fee.
  r['cards'][cno] = {'uid': -(100000 + cno), 'name': name, 'grid': gen_card(cno), 'is_bot': True, 'credits_paid': r['stake'], 'simulated_credits_left': bot_credits}
  r.setdefault('bot_names', []).append(name)
  r['next_bot_at'] = time.time() + random.uniform(0.08, 0.25)

def bot_winner(r):
  called = set(r['called'])
  for cno, info in r['cards'].items():
    if info.get('is_bot'):
      line = check_win(info['grid'], called)
      if line:
        return cno, info, line
  return None

def finish_round(r, uid, name, cno, wl):
  is_bot = uid < 0
  prize = derash(r)  # Bots display the simulated round win; only human winners receive wallet credit.
  if not is_bot:
    comm = round(total_paid_cards(r) * r['stake'] - prize, 2)
    with get_db() as c:
      c.execute('UPDATE users SET win_wallet=win_wallet+%s, wins=wins+1, total_won=total_won+%s WHERE uid=%s', (prize, prize, uid))
      add_tx(c, uid, '🏆', f'BINGO አሸነፉ! (Card #{cno})', prize)
      c.execute('UPDATE house_stats SET total_commission=total_commission+%s, total_rounds=total_rounds+1 WHERE id=1', (comm,))
  r['phase'], r['win_until'] = 'WINNER', time.time() + WIN_SEC
  r['winner'] = {'uid': uid, 'name': name, 'card_no': cno, 'prize': prize, 'grid': r['cards'][cno]['grid'], 'win_line': wl}

def reset_room(r):
  r.update({'phase': 'WAITING', 'countdown': CD_SEC, 'cards': {}, 'called': [], 'deck': [], 'winner': None, 'tick': time.time(), 'bot_target': random.randint(30, 40), 'bot_names': [], 'next_bot_at': time.time() + random.uniform(0.5, 1.5)})

def game_loop():
  while True:
    now = time.time()
    with room_lock:
      for r in rooms.values():
        pc = n_players(r)
        if r['phase'] in ('WAITING', 'COUNTDOWN'):
          if TEST_BOTS_ENABLED and now >= r.get('next_bot_at', now + 2):
            add_one_test_bot(r)
          bot_total = sum(1 for v in r['cards'].values() if v.get('is_bot'))
          if r['phase'] == 'WAITING' and ((TEST_BOTS_ENABLED and bot_total >= 1) or pc >= MIN_PLAYERS):
            r.update({'phase': 'COUNTDOWN', 'countdown': CD_SEC, 'tick': now})
          elif r['phase'] == 'COUNTDOWN' and not r['cards']:
            r.update({'phase': 'WAITING', 'countdown': CD_SEC})
          elif r['phase'] == 'COUNTDOWN' and now - r['tick'] >= 1.0:
            r['tick'], r['countdown'] = now, r['countdown'] - 1
            if r['countdown'] <= 0:
              r.update({'phase': 'PLAYING', 'called': [], 'deck': list(range(1, 76)), 'tick': now})
              secrets.SystemRandom().shuffle(r['deck'])
              with get_db() as c:
                for uid in {v['uid'] for v in r['cards'].values() if not v.get('is_bot')}:
                  c.execute('UPDATE users SET matches_played=matches_played+1 WHERE uid=%s', (uid,))
        elif r['phase'] == 'PLAYING' and now - r['tick'] >= CALL_SEC:
          r['tick'] = now
          if r['deck']:
            r['called'].insert(0, r['deck'].pop())
            bw = bot_winner(r)
            if bw:
              cno, info, wl = bw
              finish_round(r, info['uid'], info['name'], cno, wl)
          else:
            cs, won = set(r['called']), False
            for cno, info in r['cards'].items():
              wl = check_win(info['grid'], cs)
              if wl: finish_round(r, info['uid'], info['name'], cno, wl); won = True; break
            if not won: reset_room(r)
        elif r['phase'] == 'WINNER' and now >= r['win_until']:
          reset_room(r)
    time.sleep(0.1)

def auth_user(init_data: str):
  if not init_data or not TOKEN:
    raise HTTPException(401, 'እባክዎ አፑን በቴሌግራም ቦት ውስጥ ብቻ ይክፈቱ!')
  try:
    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    if len(dict(pairs)) != len(pairs):
      raise ValueError('duplicate fields')
    p = dict(pairs)
    rh = p.pop('hash', None)
    dc = '\n'.join(f'{k}={v}' for k, v in sorted(p.items()))
    sec = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(sec, dc.encode(), hashlib.sha256).hexdigest()
    if not rh or not hmac.compare_digest(expected, rh):
      raise ValueError('invalid hash')
    auth_date = int(p.get('auth_date', '0'))
    now = int(time.time())
    if auth_date <= 0 or auth_date > now + 30 or now - auth_date > 86400:
      raise ValueError('expired initData')
    uo = json.loads(p.get('user', '{}'))
    uid = int(uo['id'])
    if uid <= 0:
      raise ValueError('invalid user')
    nm = ' '.join(x for x in [uo.get('first_name'), uo.get('last_name')] if x) or 'Player'
    return ensure_user(uid, nm)
  except Exception:
    raise HTTPException(401, 'የTelegram መግቢያ መረጃ ተሳስቷል ወይም ጊዜው አልፎበታል። እባክዎ አፑን እንደገና ይክፈቱ!')

bot = telebot.TeleBot(TOKEN) if TOKEN else None
is_adm = lambda uid: ADMIN_ID != 0 and int(uid) == int(ADMIN_ID)

def menu_kb(uid=None):
  kb = RKB(resize_keyboard=True)
  kb.row(KBtn('🎮 Play Dalol Bingo'))
  kb.row(KBtn('💳 ባላንስ (Balance)'), KBtn('👥 ግብዣ (Invite)'))
  kb.row(KBtn('💰 ገቢ (Deposit)'), KBtn('💸 ወጪ (Withdraw)'))
  kb.row(KBtn('📝 ምዝገባ (Register)'), KBtn('🎧 ድጋፍ (Support)'))
  kb.row(KBtn('📜 ህግና መመሪያ'), KBtn('👑 Admin Panel')) if (uid and is_adm(uid)) else kb.row(KBtn('📜 ህግና መመሪያ'))
  return kb

def play_kb(lbl='🎲 Play Dalol Bingo'):
  kb = IKB(); kb.add(IBtn(lbl, web_app=WebAppInfo(url=APP_URL))); return kb

def adm_kb():
  kb = IKB()
  kb.row(IBtn('📊 ሪፖርትና ትርፍ', callback_data='adm_stats'), IBtn('👥 ተጫዋቾች', callback_data='adm_users'))
  kb.row(IBtn('➕ ብር ጨምር', callback_data='adm_add'), IBtn('➖ ብር ቀንስ', callback_data='adm_sub'))
  kb.row(IBtn(f'⚙️ ዝቅተኛ ተጫዋች: {MIN_PLAYERS}', callback_data='adm_minp'), IBtn('💾 DB Backup', callback_data='adm_backup'))
  kb.row(IBtn('📢 ለሁሉም መልዕክት ላክ', callback_data='adm_bcast'))
  return kb

def notify_req(rtype, uid, amt, md, det, reference=None):
  u = ensure_user(uid)
  with get_db() as c:
    rid = c.execute('INSERT INTO requests (req_type,uid,amount,method,details,status,created_at,reference) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                    (rtype, uid, float(amt), md, det, 'pending', int(time.time()), reference)).lastrowid
  if not bot or ADMIN_ID == 0: return rid
  kb = IKB()
  if rtype == 'deposit':
    hdr = '🔔 አዲስ የገቢ (DEPOSIT) ጥያቄ!'
    kb.row(IBtn('✅ ፍቀድ', callback_data=f'ap_dep_{rid}'), IBtn('❌ ሰርዝ', callback_data=f'rj_dep_{rid}'))
  else:
    hdr = '🔔 አዲስ የወጪ (WITHDRAW) ጥያቄ!'
    kb.row(IBtn('✅ ተልኳል', callback_data=f'ap_wd_{rid}'), IBtn('❌ ሰርዝና መልስ', callback_data=f'rj_wd_{rid}'))
  try: bot.send_message(ADMIN_ID, f'{hdr}\n\n🆔 #{rid}\n👤 {u["name"]} (ID: {uid})\n📞 {u["phone"] or "-"}\n💵 {float(amt):.2f} ብር\n🏦 {md}\n📝 {det}', reply_markup=kb)
  except Exception: pass
  return rid

if bot:
  @bot.message_handler(commands=['myid'])
  def c_myid(m): bot.reply_to(m, f'🆔 የአንተ Telegram ID፦ `{m.from_user.id}`', parse_mode='Markdown')

  @bot.message_handler(commands=['admin'])
  @bot.message_handler(func=lambda m: m.text == '👑 Admin Panel')
  def c_admin(m):
    if not is_adm(m.from_user.id): return bot.reply_to(m, '⛔ ለአድሚን ብቻ!')
    bot.send_message(m.chat.id, '👑 DALOL BINGO — የአድሚን መቆጣጠሪያ', reply_markup=adm_kb())

  @bot.message_handler(commands=['start'])
  def c_start(m):
    ps = (m.text or '').split()
    ref = int(ps[1]) if len(ps) > 1 and ps[1].isdigit() and int(ps[1]) != m.from_user.id else None
    u = ensure_user(m.from_user.id, m.from_user.first_name or 'Player', ref)
    if not u['phone']:
      kb = RKB(resize_keyboard=True, one_time_keyboard=True); kb.add(KBtn('📱 ስልክ ቁጥር አጋራ', request_contact=True))
      return bot.send_message(m.chat.id, '👑 እንኳን ወደ DALOL BINGO በደህና መጡ!\n\nለመመዝገብ ስልክ ቁጥርዎን ያጋሩ 👇', reply_markup=kb)
    bot.send_message(m.chat.id, f'👑 ወደ DALOL BINGO በደህና መጡ!\n🎧 ድጋፍ፦ {SUPPORT}', reply_markup=menu_kb(m.from_user.id))
    bot.send_message(m.chat.id, '🎰 ጨዋታውን ለመክፈት ከታች ይጫኑ 👇', reply_markup=play_kb('🎮 Play Now'))

  @bot.message_handler(content_types=['contact'])
  def c_contact(m):
    ensure_user(m.from_user.id, m.from_user.first_name or 'Player')
    with get_db() as c: c.execute('UPDATE users SET phone=%s WHERE uid=%s', (m.contact.phone_number, m.from_user.id))
    bot.send_message(m.chat.id, f'✅ ተመዝግበዋል! ({m.contact.phone_number})', reply_markup=menu_kb(m.from_user.id))
    bot.send_message(m.chat.id, '🎰 ጨዋታውን ለመክፈት ከታች ይጫኑ 👇', reply_markup=play_kb('🎮 Play Now'))

  @bot.message_handler(commands=['play'])
  @bot.message_handler(func=lambda m: m.text == '🎮 Play Dalol Bingo')
  def c_play(m): bot.send_message(m.chat.id, 'Good luck Dalol Bingo 🍀', reply_markup=play_kb('🎲 Play Dalol Bingo'))

  @bot.message_handler(commands=['balance'])
  @bot.message_handler(func=lambda m: m.text == '💳 ባላንስ (Balance)')
  def c_bal(m):
    u = ensure_user(m.from_user.id, m.from_user.first_name or 'Player')
    bot.send_message(m.chat.id, f'🏦 ባላንስ\n🎮 Play: {u["play_wallet"]:.2f} ብር\n🏆 Win: {u["win_wallet"]:.2f} ብር\n💰 ጠቅላላ: {u["play_wallet"]+u["win_wallet"]:.2f} ብር', reply_markup=play_kb('🎲 Play'))

  @bot.message_handler(commands=['deposit'])
  @bot.message_handler(func=lambda m: m.text == '💰 ገቢ (Deposit)')
  def c_dep(m):
    kb = IKB(); kb.add(IBtn('💳 Wallet ክፈት', web_app=WebAppInfo(url=APP_URL + '?tab=wallet')))
    bot.reply_to(m, '💳 ገቢ ለማድረግ DALOL BINGO Walletን ይክፈቱ።', reply_markup=kb)

  @bot.callback_query_handler(func=lambda c: c.data in ('dep_TeleBirr', 'dep_CBE'))
  def cb_dep(call):
    bot.answer_callback_query(call.id); md = call.data.split('_')[1]
    bot.register_next_step_handler(bot.send_message(call.message.chat.id, f'💵 በ {md} የሚያስገቡትን መጠን ይጻፉ (ዝቅተኛ 20 ብር)፦'), s_dep_amt, md)

  def s_dep_amt(m, md):
    try:
      amt = float((m.text or '').strip())
      if not math.isfinite(amt) or amt < 20 or amt > 1000000:
        return bot.reply_to(m, '❌ 20 ብር እስከ 1,000,000 ብር ያለ ትክክለኛ መጠን ያስገቡ።')
    except Exception: return bot.reply_to(m, '❌ ትክክለኛ ቁጥር ያስገቡ።')
    bot.register_next_step_handler(bot.send_message(m.chat.id, f'🏦 {md}\n👤 {TB_NAME}\n📱 {TB_PHONE}\n💵 {amt:.2f} ብር\n\nከላኩ በኋላ የSMS Transaction ID ይላኩ 👇'), s_dep_tx, amt, md)

  def s_dep_tx(m, amt, md):
    tx = (m.text or '').strip()
    if len(tx) < 4 or len(tx) > 120: return bot.reply_to(m, '❌ ትክክለኛ Transaction ID ያስገቡ።')
    try: notify_req('deposit', m.from_user.id, amt, md, f'TX: {tx}', reference=tx)
    except psycopg2.errors.UniqueViolation: return bot.reply_to(m, '❌ ይህ Transaction ID ቀድሞ ተጠቅሟል።')
    bot.reply_to(m, f'✅ የ {amt:.2f} ብር ገቢ ጥያቄዎ ለአድሚን ተልኳል!', reply_markup=menu_kb(m.from_user.id))

  @bot.message_handler(commands=['withdraw'])
  @bot.message_handler(func=lambda m: m.text == '💸 ወጪ (Withdraw)')
  def c_wd(m):
    kb = IKB(); kb.add(IBtn('💸 Wallet ክፈት', web_app=WebAppInfo(url=APP_URL + '?tab=wallet')))
    bot.reply_to(m, '💸 ወጪ ለመጠየቅ DALOL BINGO Walletን ይክፈቱ።', reply_markup=kb)

  @bot.callback_query_handler(func=lambda c: c.data in ('wd_TeleBirr', 'wd_CBE'))
  def cb_wd(call):
    bot.answer_callback_query(call.id); md = call.data.split('_')[1]
    bot.register_next_step_handler(bot.send_message(call.message.chat.id, f'💸 በ {md} የሚወጣውን መጠን ይጻፉ (ዝቅተኛ 100 ብር)፦'), s_wd_amt, md)

  def s_wd_amt(m, md):
    u = ensure_user(m.from_user.id)
    try: amt = float((m.text or '').strip())
    except Exception: return bot.reply_to(m, '❌ ትክክለኛ ቁጥር ያስገቡ።')
    if not math.isfinite(amt) or amt < 100 or amt > 1000000 or u['win_wallet'] < amt:
      return bot.reply_to(m, f'❌ 100–1,000,000 ብር ያስገቡ እና በቂ Win Wallet እንዳለ ያረጋግጡ ({u["win_wallet"]:.2f} ብር)።')
    bot.register_next_step_handler(bot.reply_to(m, f'📱 የ {md} ስልክ/አካውንት ቁጥርዎን ያስገቡ፦'), s_wd_acc, amt, md)

  def s_wd_acc(m, amt, md):
    acc = (m.text or '').strip()
    if len(acc) < 6 or len(acc) > 64:
      return bot.reply_to(m, '❌ ትክክለኛ የስልክ/አካውንት ቁጥር ያስገቡ።')
    try:
      with get_db() as c:
        c.execute('BEGIN')
        u = c.execute('SELECT win_wallet FROM users WHERE uid=%s', (m.from_user.id,)).fetchone()
        if not u or not math.isfinite(float(amt)) or amt < 100 or u['win_wallet'] < amt:
          return bot.reply_to(m, '❌ በቂ Win Wallet የለዎትም ወይም የገንዘብ መጠኑ ትክክል አይደለም!')
        cur = c.execute('UPDATE users SET win_wallet=win_wallet-%s WHERE uid=%s AND win_wallet>=%s', (amt, m.from_user.id, amt))
        if cur.rowcount != 1:
          return bot.reply_to(m, '❌ በቂ Win Wallet የለዎትም!')
        add_tx(c, m.from_user.id, '💸', f'የወጪ ጥያቄ ({md})', -amt)
        rid = c.execute('INSERT INTO requests (req_type,uid,amount,method,details,status,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                        ('withdraw', m.from_user.id, amt, md, f'Acc: {acc}', 'pending', int(time.time()))).lastrowid
      if bot and ADMIN_ID:
        kb = IKB(); kb.row(IBtn('✅ ተልኳል', callback_data=f'ap_wd_{rid}'), IBtn('❌ ሰርዝና መልስ', callback_data=f'rj_wd_{rid}'))
        try: bot.send_message(ADMIN_ID, f'🔔 አዲስ የወጪ ጥያቄ!\n\n🆔 #{rid}\n👤 {m.from_user.first_name or "Player"} (ID: {m.from_user.id})\n💵 {amt:.2f} ብር\n🏦 {md}\n📱 {acc}', reply_markup=kb)
        except Exception: pass
      bot.reply_to(m, f'✅ የ {amt:.2f} ብር የወጪ ጥያቄዎ ተመዝግቧል።', reply_markup=menu_kb(m.from_user.id))
    except psycopg2.Error:
      bot.reply_to(m, '❌ ጥያቄውን ማስመዝገብ አልተቻለም። እባክዎ እንደገና ይሞክሩ።')

  @bot.callback_query_handler(func=lambda c: c.data.startswith(('adm_', 'ap_', 'rj_')))
  def cb_adm(call):
    global MIN_PLAYERS
    if not is_adm(call.from_user.id): return bot.answer_callback_query(call.id, '⛔ ለአድሚን ብቻ!', show_alert=True)
    d = call.data; bot.answer_callback_query(call.id)
    if d == 'adm_stats':
      with get_db() as c:
        uc = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        pw = c.execute('SELECT COALESCE(SUM(play_wallet),0) FROM users').fetchone()[0]
        ww = c.execute('SELECT COALESCE(SUM(win_wallet),0) FROM users').fetchone()[0]
        pn = c.execute("SELECT COUNT(*) FROM requests WHERE status='pending'").fetchone()[0]
        hs = c.execute('SELECT total_commission,total_rounds FROM house_stats WHERE id=1').fetchone()
      with room_lock: lv = ' | '.join(f'{k}ብር:{n_players(r)}ሰው({r["phase"]})' for k, r in rooms.items())
      bot.send_message(call.message.chat.id, f'📊 ሪፖርት\n👥 ተጫዋቾች: {uc}\n🎮 Play: {pw:.2f} ብር\n🏆 Win: {ww:.2f} ብር\n💰 ትርፍ(20%): {hs["total_commission"]:.2f} ብር\n🎲 ዙሮች: {hs["total_rounds"]}\n⏳ ጥያቄዎች: {pn}\n🔴 {lv}')
    elif d == 'adm_users':
      with get_db() as c: rows = c.execute('SELECT * FROM users ORDER BY created_at DESC LIMIT 20').fetchall()
      bot.send_message(call.message.chat.id, '\n'.join(f'• {r["name"]} (`{r["uid"]}`) | P:{r["play_wallet"]:.1f} W:{r["win_wallet"]:.1f}' for r in rows) or 'ምንም ተጫዋች የለም።', parse_mode='Markdown')
    elif d in ('adm_add', 'adm_sub'):
      act = 'add' if d == 'adm_add' else 'sub'
      bot.register_next_step_handler(bot.send_message(call.message.chat.id, '✏️ ID እና መጠን ይላኩ (ምሳሌ፦ `528672685 100`)', parse_mode='Markdown'), s_adm_bal, act)
    elif d == 'adm_minp':
      MIN_PLAYERS = 1 if MIN_PLAYERS >= 3 else MIN_PLAYERS + 1
      bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=adm_kb())
      bot.send_message(call.message.chat.id, f'⚙️ ዝቅተኛ ተጫዋች ወደ {MIN_PLAYERS} ተቀይሯል።')
    elif d == 'adm_backup':
      bot.send_message(call.message.chat.id, '💾 Neon PostgreSQL ዳታቤዝ ነው። የፋይል backup ከዚህ ምናሌ አይደገፍም፤ በNeon dashboard የbackup/restore አማራጮችን ይጠቀሙ።')
    elif d == 'adm_bcast':
      bot.register_next_step_handler(bot.send_message(call.message.chat.id, '📢 ለሁሉም የሚላከውን መልዕክት ይጻፉ፦'), s_bcast)
    elif d.startswith(('ap_', 'rj_')):
      act, rt, rid = d.split('_')
      with get_db() as c:
        c.execute('BEGIN')
        rq = c.execute('SELECT * FROM requests WHERE id=%s', (int(rid),)).fetchone()
        if not rq or rq['status'] != 'pending': return bot.send_message(call.message.chat.id, '⚠️ ቀድሞ ተወስኗል።')
        uid, amt = rq['uid'], rq['amount']
        if rt == 'dep' and act == 'ap':
          c.execute("UPDATE requests SET status='approved' WHERE id=%s", (int(rid),))
          c.execute('UPDATE users SET play_wallet=play_wallet+%s WHERE uid=%s', (amt, uid))
          add_tx(c, uid, '💰', f'ገቢ ጸድቋል ({rq["method"]})', amt)
          u = c.execute('SELECT referred_by FROM users WHERE uid=%s', (uid,)).fetchone()
          if u and u['referred_by']:
            c.execute('UPDATE users SET play_wallet=play_wallet+15 WHERE uid=%s', (u['referred_by'],))
            c.execute('UPDATE users SET referred_by=NULL WHERE uid=%s', (uid,))
            add_tx(c, u['referred_by'], '🎁', 'የግብዣ ሽልማት', 15.0)
          bot.edit_message_text(call.message.text + f'\n\n✅ ጸድቋል (+{amt:.2f} ብር)', call.message.chat.id, call.message.message_id)
          try: bot.send_message(uid, f'🎉 የ {amt:.2f} ብር ገቢዎ ጸድቋል!')
          except Exception: pass
        elif rt == 'dep':
          c.execute("UPDATE requests SET status='rejected' WHERE id=%s", (int(rid),))
          bot.edit_message_text(call.message.text + '\n\n❌ ተሰርዟል', call.message.chat.id, call.message.message_id)
        elif rt == 'wd' and act == 'ap':
          c.execute("UPDATE requests SET status='paid' WHERE id=%s", (int(rid),))
          bot.edit_message_text(call.message.text + f'\n\n✅ ተልኳል ({amt:.2f} ብር)', call.message.chat.id, call.message.message_id)
          try: bot.send_message(uid, f'✅ የ {amt:.2f} ብር ወጪዎ ተልኳል!')
          except Exception: pass
        else:
          c.execute("UPDATE requests SET status='rejected' WHERE id=%s", (int(rid),))
          c.execute('UPDATE users SET win_wallet=win_wallet+%s WHERE uid=%s', (amt, uid))
          add_tx(c, uid, '↩️', 'ወጪ ተመልሷል', amt)
          bot.edit_message_text(call.message.text + '\n\n❌ ተሰርዞ ተመልሷል', call.message.chat.id, call.message.message_id)

  def s_adm_bal(m, act):
    if not is_adm(m.from_user.id): return
    try:
      u_s, a_s = (m.text or '').strip().split()
      uid, amt = int(u_s), float(a_s)
      if uid <= 0 or not math.isfinite(amt) or amt <= 0 or amt > 1000000: raise ValueError()
      ensure_user(uid)
      with get_db() as c:
        if act == 'add': c.execute('UPDATE users SET play_wallet=play_wallet+%s WHERE uid=%s', (amt, uid)); add_tx(c, uid, '🎁', 'አድሚን ጨምሯል', amt)
        else: c.execute('UPDATE users SET play_wallet=GREATEST(0,play_wallet-%s) WHERE uid=%s', (amt, uid)); add_tx(c, uid, '➖', 'አድሚን ቀንሷል', -amt)
        pw = c.execute('SELECT play_wallet FROM users WHERE uid=%s', (uid,)).fetchone()[0]
      bot.reply_to(m, f'✅ ተስተካክሏል! ID `{uid}` Play: {pw:.2f} ብር', parse_mode='Markdown')
    except Exception: bot.reply_to(m, '❌ ምሳሌ፦ `528672685 100`', parse_mode='Markdown')

  def s_bcast(m):
    if not is_adm(m.from_user.id): return
    with get_db() as c: uids = [r[0] for r in c.execute('SELECT uid FROM users').fetchall()]
    s = 0
    for u in uids:
      try: bot.send_message(u, f'📢 ማስታወቂያ\n\n{m.text}'); s += 1
      except Exception: pass
    bot.reply_to(m, f'✅ ለ {s} ተጫዋቾች ተልኳል።')

  @bot.message_handler(func=lambda m: m.text == '👥 ግብዣ (Invite)')
  def c_inv(m):
    me = bot.get_me()
    bot.send_message(m.chat.id, f'🎁 ለእያንዳንዱ ስኬታማ ግብዣ 15 ብር ይሸለሙ!\n🔗 https://t.me/{me.username}?start={m.from_user.id}')

  @bot.message_handler(func=lambda m: m.text == '📝 ምዝገባ (Register)')
  def c_reg(m):
    kb = RKB(resize_keyboard=True, one_time_keyboard=True); kb.add(KBtn('📱 ስልክ ቁጥር አጋራ', request_contact=True))
    bot.send_message(m.chat.id, '📱 ስልክ ቁጥርዎን ያጋሩ 👇', reply_markup=kb)

  @bot.message_handler(func=lambda m: m.text == '🎧 ድጋፍ (Support)')
  def c_sup(m): bot.send_message(m.chat.id, f'🎧 ድጋፍ፦ {SUPPORT}')

  @bot.message_handler(func=lambda m: m.text == '📜 ህግና መመሪያ')
  def c_rules(m): bot.send_message(m.chat.id, '📜 ህግና መመሪያ\n• ቢያንስ 2 ተጫዋቾች ካርድ ሲገዙ ይጀምራል።\n• ካርድ: 10/20/50 ብር (እስከ 4)።\n• ደራሽ: 80% ለአሸናፊው።\n• ዝቅተኛ ወጪ: 100 ብር።')

def run_bot():
  global BOT_USERNAME
  if not bot: return print('WARNING: BOT_TOKEN not set; bot polling skipped.')
  try: BOT_USERNAME = bot.get_me().username or BOT_USERNAME
  except Exception as e: print('Bot username lookup failed:', e)
  while True:
    try: bot.infinity_polling(timeout=20, long_polling_timeout=15)
    except Exception as e: print('Bot error:', e); time.sleep(5)

app = FastAPI(title='DALOL BINGO Live')
allowed_origins = [x.strip().rstrip('/') for x in os.environ.get('CORS_ORIGINS', APP_URL).split(',') if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_methods=['GET', 'POST'], allow_headers=['Content-Type', 'X-Tg-Init-Data'])
if os.path.isdir('sounds'): app.mount('/sounds', StaticFiles(directory='sounds'), name='sounds')

@app.on_event('startup')
def on_startup():
  init_db()
  threading.Thread(target=game_loop, daemon=True).start()
  threading.Thread(target=run_bot, daemon=True).start()

@app.get('/health')
def health(): return {'ok': True, 'time': int(time.time())}

@app.head('/')
def head_root(): return Response(status_code=200)

@app.get('/', response_class=HTMLResponse)
def serve_app(): return FileResponse('index.html') if os.path.exists('index.html') else HTMLResponse('index.html missing', 404)

BONUS_TZ = ZoneInfo('Africa/Addis_Ababa')
BONUS_DAILY = [
  ('morning', 'የጠዋት ቦነስ', 8, 13),
  ('midday', 'የቀን መካከል ቦነስ', 13, 19),
  ('evening', 'የማታ ቦነስ', 19, 8),
]
BONUS_REWARD = 50
BONUS_LIMIT = 100

def get_bonus_state(c, u):
  now = datetime.now(BONUS_TZ)
  day = now.date()
  current = None
  for name, label, start_hour, end_hour in BONUS_DAILY:
    start = datetime.combine(day, datetime.min.time(), BONUS_TZ).replace(hour=start_hour)
    end_day = day if end_hour > start_hour else day + timedelta(days=1)
    end = datetime.combine(end_day, datetime.min.time(), BONUS_TZ).replace(hour=end_hour)
    if start <= now < end:
      current = (name, label, start, end)
      break
  cards = []
  for name, label, start_hour, end_hour in BONUS_DAILY:
    start = datetime.combine(day, datetime.min.time(), BONUS_TZ).replace(hour=start_hour)
    end_day = day if end_hour > start_hour else day + timedelta(days=1)
    end = datetime.combine(end_day, datetime.min.time(), BONUS_TZ).replace(hour=end_hour)
    if current and current[0] == name:
      event_day = start.date().isoformat()
      event_key = name + ':' + event_day
      count = c.execute('SELECT COUNT(*) FROM bonus_claims WHERE bonus_key=%s', (event_key,)).fetchone()[0]
      own = c.execute('SELECT 1 FROM bonus_claims WHERE uid=%s AND bonus_key=%s', (u['uid'], event_key)).fetchone()
      status = 'claimed' if own else ('full' if count >= BONUS_LIMIT else 'open')
      remain = max(0, int((end-now).total_seconds()))
    else:
      status, count, remain = 'closed', 0, max(0, int((start-now).total_seconds())) if start > now else 0
      event_key = name + ':' + start.date().isoformat()
    cards.append({'id':name,'label':label,'key':event_key,'reward':BONUS_REWARD,'count':count,'limit':BONUS_LIMIT,'status':status,'remaining_seconds':remain,'starts_at':start.isoformat(),'ends_at':end.isoformat()})
  rounds = int(u.get('matches_played') or 0)
  lifetime_key = 'ten_rounds'
  done = c.execute('SELECT 1 FROM bonus_claims WHERE uid=%s AND bonus_key=%s', (u['uid'], lifetime_key)).fetchone()
  cards.append({'id':'ten_rounds','label':'የ10 ዙር ተጫዋች ቦነስ','reward':BONUS_REWARD,'progress':min(rounds,10),'target':10,'status':'claimed' if done else ('open' if rounds >= 10 else 'locked')})
  return {'daily':cards,'timezone':'Africa/Addis_Ababa'}

@app.post('/api/claim_bonus')
async def api_claim_bonus(req: Request, x_tg_init_data: str = Header(default='')):
  u = auth_user(x_tg_init_data)
  try:
    body = await req.json()
    bonus_id = str(body.get('bonus_id','')).strip()
  except Exception:
    raise HTTPException(400, 'የቦነስ ጥያቄው ትክክል አይደለም።')
  if bonus_id not in ('morning','midday','evening','ten_rounds'):
    raise HTTPException(400, 'የቦነሱ አይነት አይታወቅም።')
  with get_db() as c:
    c.execute('BEGIN')
    c.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('dalol_bonus_' + bonus_id,))
    now = datetime.now(BONUS_TZ)
    if bonus_id == 'ten_rounds':
      row = c.execute('SELECT matches_played FROM users WHERE uid=%s', (u['uid'],)).fetchone()
      if not row or int(row['matches_played'] or 0) < 10:
        raise HTTPException(400, 'ቦነሱን ለመውሰድ 10 ዙር መጫወት አለብዎት።')
      bonus_key = 'ten_rounds'
    else:
      session = next((x for x in BONUS_DAILY if x[0] == bonus_id), None)
      if not session:
        raise HTTPException(400, 'የቦነስ ሰዓቱ አልደረሰም።')
      _, _, start_hour, end_hour = session
      today = now.date()
      start = datetime.combine(today, datetime.min.time(), BONUS_TZ).replace(hour=start_hour)
      end_day = today if end_hour > start_hour else today + timedelta(days=1)
      end = datetime.combine(end_day, datetime.min.time(), BONUS_TZ).replace(hour=end_hour)
      if not (start <= now < end):
        raise HTTPException(400, 'የቦነሱ ጊዜ አልደረሰም ወይም አልፏል።')
      bonus_key = bonus_id + ':' + start.date().isoformat()
      count = c.execute('SELECT COUNT(*) FROM bonus_claims WHERE bonus_key=%s', (bonus_key,)).fetchone()[0]
      if count >= BONUS_LIMIT:
        raise HTTPException(409, 'ይህ ቦነስ በ100 ተጫዋቾች ተወስዷል።')
    already = c.execute('SELECT 1 FROM bonus_claims WHERE uid=%s AND bonus_key=%s', (u['uid'], bonus_key)).fetchone()
    if already:
      raise HTTPException(409, 'ይህን ቦነስ ቀድሞ ወስደዋል።')
    c.execute('INSERT INTO bonus_claims(uid,bonus_key,amount,claimed_at) VALUES(%s,%s,%s,%s)', (u['uid'], bonus_key, BONUS_REWARD, int(time.time())))
    c.execute('UPDATE users SET play_wallet=play_wallet+%s WHERE uid=%s', (BONUS_REWARD, u['uid']))
    title = 'የ10 ዙር ቦነስ' if bonus_id == 'ten_rounds' else 'የ' + {'morning':'ጠዋት','midday':'ቀትር','evening':'ማታ'}[bonus_id] + ' ቦነስ'
    add_tx(c, u['uid'], '🎁', title, BONUS_REWARD)
  return {'ok':True,'amount':BONUS_REWARD,'message':'ቦነሱ ወደ Play Wallet ተጨምሯል!'}

@app.get('/api/state')
def api_state(stake: int = 10, x_tg_init_data: str = Header(default='')):
  u = auth_user(x_tg_init_data)
  if stake not in rooms: raise HTTPException(400, 'ልክ ያልሆነ የጨዋታ ክፍል!')
  r = rooms[stake]
  with room_lock:
    my_c = sorted([k for k, v in r['cards'].items() if v['uid'] == u['uid']])
    tk_c = sorted([k for k, v in r['cards'].items() if v['uid'] != u['uid']])
    snap = {'stake': r['stake'], 'phase': r['phase'], 'countdown': r['countdown'], 'players': n_players(r),
            'min_players': MIN_PLAYERS, 'total_cards': len(r['cards']), 'derash': derash(r),
            'taken_cards': tk_c, 'my_cards': my_c, 'my_boards': {str(n): r['cards'][n]['grid'] for n in my_c},
            'called': r['called'], 'winner': r['winner'], 'winner_left': max(0, int(r['win_until'] - time.time())) if r['phase'] == 'WINNER' else 0}
  with get_db() as c:
    txs = [dict(x) for x in c.execute('SELECT icon,title,amount,created_at FROM transactions WHERE uid=%s ORDER BY id DESC LIMIT 25', (u['uid'],)).fetchall()]
    top = [dict(x) for x in c.execute('SELECT name,total_won FROM users WHERE total_won>0 ORDER BY total_won DESC LIMIT 10').fetchall()]
    pending_invites = c.execute('SELECT COUNT(*) FROM users WHERE referred_by=%s', (u['uid'],)).fetchone()[0]
    earned_bonus = c.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE uid=%s AND title='የግብዣ ሽልማት'", (u['uid'],)).fetchone()[0]
  bonus_data = get_bonus_state(c, u)
  return {'user': {'uid': u['uid'], 'name': u['name'], 'play_wallet': round(u['play_wallet'],2), 'win_wallet': round(u['win_wallet'],2), 'matches_played': u['matches_played'], 'wins': u['wins']},
          'config': {'telebirr_name': TB_NAME, 'telebirr_phone': TB_PHONE, 'cbe_name': CB_NAME, 'cbe_phone': CB_PHONE, 'support': SUPPORT}, 'room': snap, 'history': txs, 'rank': top,
          'bonus': {'reward_per_invite': 15, 'pending_invites': pending_invites, 'earned_total': round(float(earned_bonus or 0),2), 'bot_username': BOT_USERNAME, **bonus_data}}

@app.post('/api/buy_card')
async def api_buy(req: Request, x_tg_init_data: str = Header(default='')):
  u, b = auth_user(x_tg_init_data), await req.json()
  try: st, cno = int(b.get('stake', 10)), int(b.get('card_no', 0))
  except (TypeError, ValueError): raise HTTPException(400, 'ልክ ያልሆነ ካርድ!')
  if st not in rooms or not (1 <= cno <= 400): raise HTTPException(400, 'ልክ ያልሆነ ካርድ!')
  with room_lock:
    r = rooms[st]
    if r['phase'] not in ('WAITING', 'COUNTDOWN'): raise HTTPException(400, 'ጨዋታው ተጀምሯል!')
    if cno in r['cards']:
      if r['cards'][cno]['uid'] == u['uid']:
        with get_db() as c:
          c.execute('BEGIN')
          c.execute('UPDATE users SET play_wallet=play_wallet+%s WHERE uid=%s', (st, u['uid']))
          add_tx(c, u['uid'], '↩️', f'ካርድ #{cno} ተሰርዟል', st)
        del r['cards'][cno]
        return {'ok': True, 'action': 'removed'}
      raise HTTPException(400, f'ካርድ #{cno} ተይዟል!')
    if sum(1 for v in r['cards'].values() if v['uid'] == u['uid']) >= 4: raise HTTPException(400, 'እስከ 4 ካርድ ብቻ!')
    with get_db() as c:
      c.execute('BEGIN')
      wallet = c.execute('SELECT play_wallet FROM users WHERE uid=%s', (u['uid'],)).fetchone()
      if not wallet or wallet['play_wallet'] < st: raise HTTPException(400, 'በቂ Play Wallet የለዎትም! ገቢ ያድርጉ።')
      c.execute('UPDATE users SET play_wallet=play_wallet-%s WHERE uid=%s', (st, u['uid'])); add_tx(c, u['uid'], '🎴', f'ካርድ #{cno} ({st} ብር)', -st)
    r['cards'][cno] = {'uid': u['uid'], 'name': u['name'], 'grid': gen_card(cno), 'is_bot': False}
    return {'ok': True, 'action': 'bought'}

@app.post('/api/claim_bingo')
async def api_claim(req: Request, x_tg_init_data: str = Header(default='')):
  u, b = auth_user(x_tg_init_data), await req.json()
  try: st, cno = int(b.get('stake', 10)), int(b.get('card_no', 0))
  except (TypeError, ValueError): raise HTTPException(400, 'ልክ ያልሆነ ክፍል/ካርድ!')
  if st not in rooms or not (1 <= cno <= 100): raise HTTPException(400, 'ልክ ያልሆነ ክፍል/ካርድ!')
  with room_lock:
    r = rooms[st]
    if r['phase'] != 'PLAYING': raise HTTPException(400, 'ጨዋታው አልተጀመረም!')
    own = r['cards'].get(cno)
    if not own or own['uid'] != u['uid']: raise HTTPException(403, 'ይህ ካርድ የእርስዎ አይደለም!')
    wl = check_win(own['grid'], set(r['called']))
    if not wl: raise HTTPException(400, 'ገና BINGO አልተሟላም!')
    finish_round(r, u['uid'], u['name'], cno, wl)
    return {'ok': True, 'prize': r['winner']['prize']}

@app.post('/api/transfer')
async def api_transfer(x_tg_init_data: str = Header(default='')):
  u = auth_user(x_tg_init_data)
  with get_db() as c:
    c.execute('BEGIN')
    row = c.execute('SELECT win_wallet FROM users WHERE uid=%s', (u['uid'],)).fetchone()
    amt = row['win_wallet'] if row else 0
    if amt <= 0: raise HTTPException(400, 'በ Win Wallet ውስጥ ገንዘብ የለም!')
    cur = c.execute('UPDATE users SET play_wallet=play_wallet+%s, win_wallet=0 WHERE uid=%s AND win_wallet=%s', (amt, u['uid'], amt))
    if cur.rowcount != 1: raise HTTPException(409, 'ባላንሱ ተቀይሯል፤ እንደገና ይሞክሩ።')
    add_tx(c, u['uid'], '🔄', 'ከ Win ወደ Play', amt)
  return {'ok': True, 'amount': amt}

@app.post('/api/deposit')
async def api_dep(req: Request, x_tg_init_data: str = Header(default='')):
  u = auth_user(x_tg_init_data)
  try:
    b = await req.json()
    amt = float(b.get('amount', 0))
    md = str(b.get('method', '')).strip()
    reference = str(b.get('reference', '')).strip()
  except Exception:
    raise HTTPException(400, 'የገቢ ጥያቄ መረጃ ትክክል አይደለም።')
  if not math.isfinite(amt) or amt < 20 or amt > 1000000:
    raise HTTPException(400, 'ከ20 እስከ 1,000,000 ብር ያለ መጠን ያስገቡ።')
  if md not in ('TeleBirr', 'CBE Birr'):
    raise HTTPException(400, 'TeleBirr ወይም CBE Birr ይምረጡ።')
  if len(reference) < 4 or len(reference) > 120:
    raise HTTPException(400, 'ትክክለኛ Transaction ID ያስገቡ።')
  try:
    rid = notify_req('deposit', u['uid'], amt, md, f'TX: {reference}', reference=reference)
  except psycopg2.errors.UniqueViolation:
    raise HTTPException(409, 'ይህ Transaction ID ቀድሞ ተጠቅሟል።')
  except psycopg2.Error:
    raise HTTPException(503, 'ጥያቄውን ማስመዝገብ አልተቻለም።')
  return {'ok': True, 'request_id': rid, 'status': 'pending', 'message': 'የገቢ ጥያቄዎ ለአድሚን ተልኳል።'}

@app.post('/api/withdraw')
async def api_wd(req: Request, x_tg_init_data: str = Header(default='')):
  u = auth_user(x_tg_init_data)
  try:
    b = await req.json()
    amt = float(b.get('amount', 0))
    md = str(b.get('method', '')).strip()
    account = str(b.get('account', '')).strip()
  except Exception:
    raise HTTPException(400, 'የወጪ ጥያቄ መረጃ ትክክል አይደለም።')
  if not math.isfinite(amt) or amt < 100 or amt > 1000000:
    raise HTTPException(400, 'ከ100 እስከ 1,000,000 ብር ያለ መጠን ያስገቡ።')
  if md not in ('TeleBirr', 'CBE Birr') or len(account) < 6 or len(account) > 64:
    raise HTTPException(400, 'የክፍያ ዘዴንና የአካውንት ቁጥርን ያረጋግጡ።')
  try:
    with get_db() as c:
      c.execute('BEGIN')
      row = c.execute('SELECT win_wallet FROM users WHERE uid=%s FOR UPDATE', (u['uid'],)).fetchone()
      if not row or float(row['win_wallet']) < amt:
        raise HTTPException(400, 'በ Win Wallet በቂ ባላንስ የለዎትም።')
      cur = c.execute('UPDATE users SET win_wallet=win_wallet-%s WHERE uid=%s AND win_wallet>=%s', (amt, u['uid'], amt))
      if cur.rowcount != 1:
        raise HTTPException(409, 'ባላንሱ ተቀይሯል፤ እንደገና ይሞክሩ።')
      add_tx(c, u['uid'], '⏳', f'የወጪ ጥያቄ ({md})', -amt)
      rid = c.execute('INSERT INTO requests (req_type,uid,amount,method,details,status,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)',
        ('withdraw', u['uid'], amt, md, f'Acc: {account}', 'pending', int(time.time()))).lastrowid
    if bot and ADMIN_ID:
      kb = IKB()
      kb.row(IBtn('✅ ተልኳል', callback_data=f'ap_wd_{rid}'), IBtn('❌ ሰርዝና መልስ', callback_data=f'rj_wd_{rid}'))
      try: bot.send_message(ADMIN_ID, f'🔔 አዲስ የወጪ ጥያቄ!\n\n🆔 #{rid}\n👤 {u["name"]} (ID: {u["uid"]})\n💵 {amt:.2f} ብር\n🏦 {md}\n📱 {account}', reply_markup=kb)
      except Exception: pass
  except HTTPException:
    raise
  except psycopg2.Error:
    raise HTTPException(503, 'የወጪ ጥያቄውን ማስመዝገብ አልተቻለም።')
  return {'ok': True, 'request_id': rid, 'status': 'pending', 'message': 'የወጪ ጥያቄዎ ተመዝግቧል።'}
