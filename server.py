# -*- coding: utf-8 -*-
"""
=====================================================================
 校园条幅报备系统 · 本地后端服务
=====================================================================
 运行环境：仅需 Python 3（标准库实现，无需 pip 安装任何依赖）
 数据存储：SQLite 数据库 banner.db（首次启动自动建库并写入演示数据）

 功能：
   1. 静态托管 login.html / index.html / admin.html
   2. 用户注册 / 登录（部门用户存数据库；管理员内置 admin/admin123）
   3. 报备记录 REST API（所有设备访问同一份服务器数据，
      任何注册用户发布的报备都会实时汇总到管理员后台）

 API 一览：
   POST /api/register                  注册部门用户
   POST /api/login                     登录 {role, account, pwd}
   GET  /api/records                   获取全部报备记录
   POST /api/records                   创建报备（状态：待审核）
   POST /api/records/<id>/action       单条操作：
       {action:'approve'}                          审核通过
       {action:'reject',  reviewNote}              驳回
       {action:'remind'}                           到期提醒
       {action:'cancel'}                           用户撤销（仅待审核）
       {action:'withdraw', removedAt, removedCount, removedNote} 提交撤下记录
   POST /api/reset                     清空并恢复演示数据（注册用户保留）

 启动方式：双击「启动.bat」，或命令行执行  python server.py
=====================================================================
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务器（兼容 Python 3.5+，各请求独立线程）"""
    daemon_threads = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # 本文件所在目录（静态文件根目录）
DB_PATH = os.path.join(BASE_DIR, 'banner.db')           # SQLite 数据库文件
PORT = int(os.environ.get('PORT', 8000))                # 监听端口（云平台通过 PORT 环境变量下发，本地默认 8000）

# 内置管理员账号（页面上不展示，由宣传部掌握）
ADMIN = {'account': 'admin', 'pwd': 'admin123', 'name': '宣传部管理员'}

# ---------------------------------------------------------------------
# 日期工具（与前端保持一致：日期格式 YYYY-MM-DD，时间格式 YYYY-MM-DD HH:MM）
# ---------------------------------------------------------------------
def _pad(n):
    return str(n).zfill(2)

def _iso(d):
    return '%d-%s-%s' % (d.year, _pad(d.month), _pad(d.day))

def today():
    return _iso(datetime.now())

def d_off(n):
    """今天偏移 n 天的日期字符串"""
    return _iso(datetime.now() + timedelta(days=n))

def dt_off(n, h, m):
    """今天偏移 n 天、指定时:分 的日期时间字符串"""
    d = datetime.now() + timedelta(days=n)
    return _iso(d) + ' ' + _pad(h) + ':' + _pad(m)

# ---------------------------------------------------------------------
# 数据库初始化
# ---------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  phone TEXT PRIMARY KEY,      -- 手机号（登录账号）
  name  TEXT NOT NULL,         -- 姓名
  dept  TEXT NOT NULL,         -- 所属单位
  pwd   TEXT NOT NULL,         -- 密码（原型明文存储，正式环境应加密）
  createdAt TEXT               -- 注册时间
);
CREATE TABLE IF NOT EXISTS records(
  id           TEXT PRIMARY KEY,  -- 报备单号 TB+日期+序号
  dept         TEXT,              -- 所属单位
  owner        TEXT,              -- 负责人
  phone        TEXT,              -- 联系电话
  creator      TEXT,              -- 创建账号（注册手机号，用于"我的"归属判定）
  "count"      INTEGER,           -- 条幅条数
  content      TEXT,              -- 条幅内容
  location     TEXT,              -- 悬挂位置
  start        TEXT,              -- 开始日期
  "end"        TEXT,              -- 截止日期
  createdAt    TEXT,              -- 报备时间
  status       TEXT DEFAULT 'pending', -- pending/active/done/rejected
  reviewNote   TEXT,              -- 驳回原因
  reminded     INTEGER DEFAULT 0, -- 是否已提醒撤下
  removedAt    TEXT,              -- 实际撤下日期
  removedCount INTEGER,           -- 实际撤下条数
  removedNote  TEXT               -- 撤下备注
);
"""

def seed_rows():
    """演示种子数据（日期基于"今天"动态生成，任何时间启动演示效果一致）。
    字段顺序：dept, owner, phone, count, content, location,
              start偏移, end偏移, 创建日偏移, 创建时, 创建分, status,
              removedAt偏移/None, removedCount, removedNote
    """
    raw = [
        ('校团委',       '李老师', '13801012233', 3, '「青春正当时」主题团日活动宣传',              '操场围栏',   2,   9,   0,  9, 30, 'pending', None, None, None),
        ('社团联合会',   '王同学', '13912244556', 3, '「百团大战」社团纳新联合招新',                '篮球场围栏', 0,   5,   0, 11, 20, 'pending', None, None, None),
        ('人工智能学院', '张老师', '13800001111', 2, '2026年秋季专场招聘宣讲会｜10月28日 大学生活动中心', '教学楼大厅', -3, 4,  -3,  9, 12, 'active',  None, None, None),
        ('人工智能学院', '张老师', '13800001111', 1, 'ACM程序设计大赛校内选拔报名进行中',           '教学楼大厅', -1,  3,  -1,  9, 40, 'active',  None, None, None),
        ('校学生会',     '刘同学', '13755667788', 2, '第21届校园歌手大赛报名倒计时',                '食堂门口',  -1,  6,  -1, 14,  5, 'active',  None, None, None),
        ('宣传部',       '陈老师', '13699887766', 4, '建校70周年校庆倒计时主题宣传',                '校门口',    -2, 12,  -2, 10,  0, 'active',  None, None, None),
        ('外国语学院',   '赵老师', '13566778899', 2, '外语文化节「语通世界」系列活动',              '教学楼大厅', -5,  2,  -5, 16, 45, 'active',  None, None, None),
        ('艺术学院',     '孙老师', '13411223344', 1, '毕业生美术作品展 · 美术馆一楼',               '图书馆前', -10, -2, -10, 15, 30, 'active',  None, None, None),
        ('智能制造学院', '周老师', '13322446688', 2, '智能制造产教融合论坛欢迎横幅',                '校门口',    -8, -1,  -8, 10, 20, 'active',  None, None, None),
        ('管理学院',     '吴老师', '13288776655', 1, '会计技能大赛赛前动员',                        '食堂门口', -20, -12, -20, 9,  0, 'done', -11, 1, '已完整拆除'),
        ('外国语学院',   '赵老师', '13566778899', 1, '英语角十周年主题活动',                        '教学楼大厅', -15, -8, -15, 11, 10, 'done',  -8, 1, ''),
        ('明德通识学院', '郑老师', '13199001122', 2, '心理健康活动月系列宣传',                      '教学楼大厅', -30, -22, -30, 14, 30, 'done', -21, 2, '两处点位均已清理'),
        ('体育部',       '钱老师', '13088112233', 2, '教职工排球联赛火热进行',                      '体育馆门前', -6, -5,  -6, 10, 10, 'done',  -5, 2, ''),
        ('青年志愿者协会','冯同学','15822334455', 1, '重阳节敬老志愿服务招募',                      '宿舍区广场', -4, -3,  -4,  9,  0, 'done',  -3, 1, '已撤下并回收'),
    ]
    rows = []
    seqs = {}  # 每个创建日期一个递增序号，生成单号 TB+日期+序号
    for dept, owner, phone, count, content, location, so, eo, co, ch, cm, status, ro, rc, rn in raw:
        created = dt_off(co, ch, cm)
        day = created[:10].replace('-', '')
        seqs[day] = seqs.get(day, 0) + 1
        rows.append({
            'id': 'TB%s%03d' % (day, seqs[day]),
            'dept': dept, 'owner': owner, 'phone': phone, 'creator': None,
            'count': count, 'content': content, 'location': location,
            'start': d_off(so), 'end': d_off(eo), 'createdAt': created,
            'status': status, 'reviewNote': None, 'reminded': 0,
            'removedAt': d_off(ro) if ro is not None else None,
            'removedCount': rc, 'removedNote': rn,
        })
    return rows

def get_db():
    """每次请求新建连接（SQLite 线程安全的最简单方式）"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """建表；若 records 为空则写入演示种子数据"""
    conn = get_db()
    with conn:
        conn.executescript(SCHEMA)
        if conn.execute('SELECT COUNT(*) c FROM records').fetchone()['c'] == 0:
            insert_records(conn, seed_rows())
    conn.close()

def insert_records(conn, rows):
    conn.executemany(
        'INSERT INTO records(id,dept,owner,phone,creator,"count",content,location,'
        'start,"end",createdAt,status,reviewNote,reminded,removedAt,removedCount,removedNote)'
        ' VALUES(:id,:dept,:owner,:phone,:creator,:count,:content,:location,'
        ':start,:end,:createdAt,:status,:reviewNote,:reminded,:removedAt,:removedCount,:removedNote)',
        rows)

def row_to_dict(r):
    """数据库行 → 前端 JSON 字段（reminded 转布尔）"""
    d = dict(r)
    d['reminded'] = bool(d['reminded'])
    return d

# ---------------------------------------------------------------------
# HTTP 请求处理
# ---------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    """静态文件 + REST API 一体化处理器（纯手写响应，兼容 Python 3.5+）"""

    # ---------- 通用工具 ----------
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html_file(self, fname):
        """输出本目录下的 html 静态文件"""
        path = os.path.join(BASE_DIR, fname)
        if not os.path.isfile(path):
            return self.send_json({'ok': False, 'msg': '页面不存在'}, 404)
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        """读取并解析 JSON 请求体，失败返回 None"""
        try:
            n = int(self.headers.get('Content-Length') or 0)
            return json.loads(self.rfile.read(n).decode('utf-8') or '{}')
        except Exception:
            return None

    def log_message(self, fmt, *args):  # 保持默认访问日志，便于排查
        try:
            BaseHTTPRequestHandler.log_message(self, fmt, *args)
        except Exception:
            pass

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/':                       # 根路径跳转到登录页
            self.send_response(302)
            self.send_header('Location', '/login.html')
            self.end_headers()
            return
        if path == '/api/records':            # 查询全部报备（按报备时间倒序）
            conn = get_db()
            rows = conn.execute('SELECT * FROM records ORDER BY createdAt DESC, id DESC').fetchall()
            conn.close()
            self.send_json({'ok': True, 'records': [row_to_dict(r) for r in rows]})
            return
        if path in ('/login.html', '/index.html', '/admin.html'):
            return self.send_html_file(path.lstrip('/'))
        self.send_json({'ok': False, 'msg': 'Not Found'}, 404)

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        data = self.read_body()
        if data is None:
            return self.send_json({'ok': False, 'msg': '请求格式错误'}, 400)

        if path == '/api/register':
            return self.api_register(data)
        if path == '/api/login':
            return self.api_login(data)
        if path == '/api/records':
            return self.api_create(data)
        if path == '/api/reset':
            return self.api_reset()
        m = re.match(r'^/api/records/([^/]+)/action$', path)
        if m:
            return self.api_action(m.group(1), data)
        self.send_json({'ok': False, 'msg': '接口不存在'}, 404)

    # ---------- 注册 ----------
    def api_register(self, d):
        name = (d.get('name') or '').strip()
        phone = (d.get('phone') or '').strip()
        dept = (d.get('dept') or '').strip()
        pwd = d.get('pwd') or ''
        if not name:
            return self.send_json({'ok': False, 'msg': '请输入姓名'})
        if not re.match(r'^1[3-9]\d{9}$', phone):
            return self.send_json({'ok': False, 'msg': '请输入正确的 11 位手机号'})
        if not dept:
            return self.send_json({'ok': False, 'msg': '请选择所属单位'})
        if len(pwd) < 6:
            return self.send_json({'ok': False, 'msg': '密码至少 6 位'})
        conn = get_db()
        try:
            with conn:
                if conn.execute('SELECT 1 FROM users WHERE phone=?', (phone,)).fetchone():
                    return self.send_json({'ok': False, 'msg': '该手机号已注册，请直接登录'})
                conn.execute('INSERT INTO users(phone,name,dept,pwd,createdAt) VALUES(?,?,?,?,?)',
                             (phone, name, dept, pwd, dt_off(0, datetime.now().hour, datetime.now().minute)))
        finally:
            conn.close()
        self.send_json({'ok': True, 'session': {'role': 'user', 'name': name, 'phone': phone, 'dept': dept}})

    # ---------- 登录 ----------
    def api_login(self, d):
        role = d.get('role')
        acc = (d.get('account') or '').strip()
        pwd = d.get('pwd') or ''
        if role == 'admin':
            if acc == ADMIN['account'] and pwd == ADMIN['pwd']:
                return self.send_json({'ok': True, 'session': {'role': 'admin', 'name': ADMIN['name'], 'phone': acc}})
            return self.send_json({'ok': False, 'msg': '管理员账号或密码错误'})
        conn = get_db()
        u = conn.execute('SELECT name,phone,dept FROM users WHERE phone=? AND pwd=?', (acc, pwd)).fetchone()
        conn.close()
        if not u:
            return self.send_json({'ok': False, 'msg': '账号或密码错误，未注册请先注册'})
        self.send_json({'ok': True, 'session': {'role': 'user', 'name': u['name'], 'phone': u['phone'], 'dept': u['dept']}})

    # ---------- 创建报备 ----------
    def api_create(self, d):
        required = ['dept', 'owner', 'phone', 'content', 'location', 'start', 'end']
        if any(not str(d.get(k) or '').strip() for k in required):
            return self.send_json({'ok': False, 'msg': '请完整填写报备信息'})
        try:
            count = int(d.get('count'))
            assert count >= 1
        except Exception:
            return self.send_json({'ok': False, 'msg': '请填写正确的条幅条数'})
        conn = get_db()
        try:
            with conn:
                # 单号：TB+当天日期+当日序号（3位）
                prefix = 'TB' + today().replace('-', '')
                seq = conn.execute('SELECT COUNT(*) c FROM records WHERE id LIKE ?', (prefix + '%',)).fetchone()['c'] + 1
                now = datetime.now()
                rec = {
                    'id': '%s%03d' % (prefix, seq),
                    'dept': d['dept'].strip(), 'owner': d['owner'].strip(),
                    'phone': d['phone'].strip(), 'creator': (d.get('creator') or '').strip() or None,
                    'count': count, 'content': d['content'].strip(), 'location': d['location'].strip(),
                    'start': d['start'], 'end': d['end'],
                    'createdAt': today() + ' ' + _pad(now.hour) + ':' + _pad(now.minute),
                    'status': 'pending', 'reviewNote': None, 'reminded': 0,
                    'removedAt': None, 'removedCount': None, 'removedNote': None,
                }
                insert_records(conn, [rec])
        finally:
            conn.close()
        self.send_json({'ok': True, 'record': rec})

    # ---------- 单条记录操作 ----------
    def api_action(self, rid, d):
        action = d.get('action')
        conn = get_db()
        try:
            r = conn.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
            if not r:
                return self.send_json({'ok': False, 'msg': '记录不存在'})
            with conn:
                if action == 'approve':                       # 管理员审核通过
                    if r['status'] != 'pending':
                        return self.send_json({'ok': False, 'msg': '该记录已审核'})
                    conn.execute("UPDATE records SET status='active' WHERE id=?", (rid,))
                elif action == 'reject':                      # 管理员驳回（需原因）
                    note = (d.get('reviewNote') or '').strip()
                    if not note:
                        return self.send_json({'ok': False, 'msg': '请填写驳回原因'})
                    conn.execute("UPDATE records SET status='rejected', reviewNote=? WHERE id=?", (note, rid))
                elif action == 'remind':                      # 到期提醒
                    conn.execute('UPDATE records SET reminded=1 WHERE id=?', (rid,))
                elif action == 'cancel':                      # 用户撤销（仅待审核，整单删除）
                    if r['status'] != 'pending':
                        return self.send_json({'ok': False, 'msg': '该记录已审核，无法撤销'})
                    conn.execute('DELETE FROM records WHERE id=?', (rid,))
                elif action == 'withdraw':                    # 用户提交撤下记录 → 完成归档
                    date = (d.get('removedAt') or '').strip()
                    try:
                        cnt = int(d.get('removedCount'))
                        assert date and cnt >= 1
                    except Exception:
                        return self.send_json({'ok': False, 'msg': '请填写撤下日期和条数'})
                    conn.execute("UPDATE records SET status='done', removedAt=?, removedCount=?, removedNote=? WHERE id=?",
                                 (date, cnt, (d.get('removedNote') or '').strip(), rid))
                else:
                    return self.send_json({'ok': False, 'msg': '不支持的操作'})
        finally:
            conn.close()
        self.send_json({'ok': True})

    # ---------- 重置演示数据 ----------
    def api_reset(self):
        conn = get_db()
        with conn:
            conn.execute('DELETE FROM records')
            insert_records(conn, seed_rows())
        conn.close()
        self.send_json({'ok': True})

# ---------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------
def main():
    init_db()
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print('=' * 52)
    print('  校园条幅报备系统 · 本地服务已启动')
    print('  本机访问:   http://localhost:%d' % PORT)
    print('  局域网访问: http://<本机IP>:%d' % PORT)
    print('  数据库文件: %s' % DB_PATH)
    print('  关闭本窗口即停止服务')
    print('=' * 52)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
