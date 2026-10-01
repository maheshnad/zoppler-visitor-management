import os, re, secrets, csv, io, json, hashlib, smtplib, threading, time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone, timedelta
from email.message import EmailMessage
from functools import wraps
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from flask import Flask, request, jsonify, session, send_from_directory, Response
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException
from psycopg import InterfaceError, OperationalError, connect
from psycopg.rows import dict_row
from PIL import Image, UnidentifiedImageError

ROOT=os.path.dirname(os.path.abspath(__file__))
app=Flask(__name__,static_folder=None)
app.wsgi_app=ProxyFix(app.wsgi_app,x_for=1,x_proto=1,x_host=1)
app.secret_key=os.environ.get('SECRET_KEY','')
if len(app.secret_key)<32: raise RuntimeError('Set SECRET_KEY to a random value of at least 32 characters')
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE','1')=='1',MAX_CONTENT_LENGTH=6*1024*1024)
if not os.environ.get('DATABASE_URL'): raise RuntimeError('Set DATABASE_URL')
def db_connection():
    return connect(os.environ['DATABASE_URL'],row_factory=dict_row,connect_timeout=15)

email_executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='visitor-email')
photo_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='visitor-photo')
SCHEMA='''CREATE TABLE IF NOT EXISTS admins (id BIGSERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS visits (id TEXT PRIMARY KEY, name TEXT NOT NULL, mobile TEXT NOT NULL, email TEXT NOT NULL, company TEXT NOT NULL DEFAULT '', host TEXT NOT NULL, department TEXT NOT NULL DEFAULT '', visit_date DATE NOT NULL, visit_time TIME NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending','Approved','Rejected','Checked In','Checked Out')), created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), approved_by BIGINT REFERENCES admins(id), checked_in_at TIMESTAMPTZ, checked_out_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS visit_audit (id BIGSERIAL PRIMARY KEY, visit_id TEXT NOT NULL REFERENCES visits(id), admin_id BIGINT NOT NULL REFERENCES admins(id), old_status TEXT NOT NULL, new_status TEXT NOT NULL, changed_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS visits_created_idx ON visits(created_at DESC);
ALTER TABLE admins ADD COLUMN IF NOT EXISTS email TEXT UNIQUE;
ALTER TABLE admins ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS admins_username_lower_idx ON admins(lower(username));
CREATE UNIQUE INDEX IF NOT EXISTS admins_email_lower_idx ON admins(lower(email)) WHERE email IS NOT NULL;
ALTER TABLE visits ADD COLUMN IF NOT EXISTS aadhar_number TEXT;
ALTER TABLE visits ADD COLUMN IF NOT EXISTS photo BYTEA;
ALTER TABLE visits ADD COLUMN IF NOT EXISTS photo_mime TEXT;
ALTER TABLE visits ADD COLUMN IF NOT EXISTS expected_checkout_date DATE;
ALTER TABLE visits ADD COLUMN IF NOT EXISTS expected_checkout_time TIME;
ALTER TABLE visits ADD COLUMN IF NOT EXISTS request_token TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS visits_request_token_idx ON visits(request_token) WHERE request_token IS NOT NULL;
CREATE TABLE IF NOT EXISTS password_resets (id BIGSERIAL PRIMARY KEY, admin_id BIGINT NOT NULL REFERENCES admins(id) ON DELETE CASCADE, token_hash TEXT UNIQUE NOT NULL, expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE password_resets ADD COLUMN IF NOT EXISTS otp_hash TEXT;
ALTER TABLE password_resets ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;'''
with db_connection() as conn:
    for statement in SCHEMA.split(';'):
        if statement.strip(): conn.execute(statement)

def public(v):
    return dict(id=v['id'],status=v['status'],date=str(v['visit_date']),time=v['visit_time'].strftime('%H:%M'))
def full(v):
    d=public(v)
    d.update({k:v[k] for k in ('name','mobile','email','company','host','department','purpose')})
    aadhar=v.get('aadhar_number') or ''
    d.update(aadhar=('********'+aadhar[-4:]) if aadhar else 'Not captured',photoUrl=('/api/visits/'+v['id']+'/photo') if v.get('photo') else None,expectedCheckoutDate=str(v['expected_checkout_date']) if v.get('expected_checkout_date') else '',expectedCheckoutTime=v['expected_checkout_time'].strftime('%H:%M') if v.get('expected_checkout_time') else '')
    d.update(createdAt=v['created_at'].isoformat(),updatedAt=v['updated_at'].isoformat(),checkedInAt=v['checked_in_at'].isoformat() if v['checked_in_at'] else None,checkedOutAt=v['checked_out_at'].isoformat() if v['checked_out_at'] else None)
    return d

def error(message,code=400): return jsonify(error=message),code

def retry_database(operation):
    for attempt in range(2):
        try:
            return operation()
        except (OperationalError,InterfaceError):
            if attempt:
                raise
            app.logger.warning('Database connection was interrupted; retrying once')
            time.sleep(.25)

@app.errorhandler(413)
def upload_too_large(_): return error('The uploaded request is too large. Use a photo of 5 MB or smaller.',413)

@app.errorhandler(Exception)
def unexpected_error(exception):
    if isinstance(exception,HTTPException): return exception
    app.logger.exception('Unhandled request error')
    return error('The server could not complete the request. Please try again.',500)

def valid_photo(data,mimetype):
    expected={'image/jpeg':'JPEG','image/png':'PNG','image/webp':'WEBP'}.get(mimetype)
    if not expected: return False
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            return image.format==expected and image.width>0 and image.height>0 and image.width*image.height<=25_000_000
    except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError): return False

visit_attempts=defaultdict(deque);visit_attempts_lock=threading.Lock()
reset_attempts=defaultdict(deque);reset_attempts_lock=threading.Lock()
def submission_rate_limited():
    client=request.headers.get('CF-Connecting-IP') or request.remote_addr or 'unknown';now=time.monotonic();window=3600
    with visit_attempts_lock:
        attempts=visit_attempts[client]
        while attempts and attempts[0]<now-window: attempts.popleft()
        if len(attempts)>=20: return True
        attempts.append(now);return False

def reset_rate_limited(email):
    client=request.headers.get('CF-Connecting-IP') or request.remote_addr or 'unknown';key=(client,email);now=time.monotonic();window=3600
    with reset_attempts_lock:
        attempts=reset_attempts[key]
        while attempts and attempts[0]<now-window: attempts.popleft()
        if len(attempts)>=5: return True
        attempts.append(now);return False

RESET_EMAILS={
    'prakashr@zopplersystems.com','ganeshkashyap@zopplersystems.com',
    'shridharyb@zopplersystems.com','omrana@zopplersystems.com',
    'maheshn@zopplersystems.com'
}

def email_delivery_configured():
    resend_ready=bool(os.environ.get('RESEND_API_KEY','').strip()) and bool((os.environ.get('SMTP_FROM','') or os.environ.get('EMAIL_FROM','')).strip())
    # Render Free blocks outbound SMTP ports. Only an HTTPS email provider can
    # truthfully be reported as configured in that environment.
    if os.environ.get('RENDER') or os.environ.get('RENDER_EXTERNAL_HOSTNAME'):
        return resend_ready
    return resend_ready or all(os.environ.get(k,'').strip() for k in ('SMTP_HOST','SMTP_FROM','SMTP_USERNAME','SMTP_PASSWORD'))

def send_messages(deliveries):
    resend_key=os.environ.get('RESEND_API_KEY','').strip()
    if resend_key:
        sender=os.environ.get('SMTP_FROM','').strip() or os.environ.get('EMAIL_FROM','').strip()
        if not sender: raise RuntimeError('Set SMTP_FROM or EMAIL_FROM for email delivery')
        failures=[]
        for recipient,subject,body in deliveries:
            payload=json.dumps({'from':sender,'to':[recipient],'subject':subject,'text':body}).encode('utf-8')
            request_message=Request('https://api.resend.com/emails',data=payload,method='POST',headers={'Authorization':'Bearer '+resend_key,'Content-Type':'application/json'})
            try:
                with urlopen(request_message,timeout=15) as response:
                    if response.status not in (200,201,202): raise RuntimeError('Email API returned an unexpected response')
            except Exception:
                app.logger.exception('Email API delivery failed for %s',recipient);failures.append(recipient)
        if failures: raise RuntimeError('Email delivery failed for '+', '.join(failures))
        return
    host=os.environ.get('SMTP_HOST','').strip()
    if not host: raise RuntimeError('Email delivery is not configured')
    port=int(os.environ.get('SMTP_PORT','587'))
    sender=os.environ.get('SMTP_FROM','').strip()
    if not sender: raise RuntimeError('SMTP_FROM is not configured')
    failures=[]
    with smtplib.SMTP(host,port,timeout=15) as smtp:
        if os.environ.get('SMTP_TLS','1')=='1': smtp.starttls()
        user=os.environ.get('SMTP_USERNAME','')
        if user: smtp.login(user,os.environ.get('SMTP_PASSWORD',''))
        for recipient,subject,body in deliveries:
            message=EmailMessage();message['Subject']=subject;message['From']=sender;message['To']=recipient;message.set_content(body)
            try: smtp.send_message(message)
            except Exception:
                app.logger.exception('Email delivery failed for %s',recipient);failures.append(recipient)
    if failures: raise RuntimeError('Email delivery failed for '+', '.join(failures))

def send_email(recipient,subject,body):
    send_messages([(recipient,subject,body)])

def email_visit(v):
    keys=('id','status','name','company','email','mobile','host','department','visit_date','visit_time','expected_checkout_date','expected_checkout_time','purpose')
    return {key:v.get(key) for key in keys}

def queue_email(fn,*args):
    future=email_executor.submit(fn,*args)
    def finished(done):
        try: done.result()
        except Exception: app.logger.exception('Background email job failed')
    future.add_done_callback(finished)

def save_visit_photo(visit_id,photo_data,photo_mime):
    for attempt in range(3):
        try:
            def store():
                with db_connection() as conn:
                    conn.execute('UPDATE visits SET photo=%s,photo_mime=%s WHERE id=%s AND photo IS NULL',(photo_data,photo_mime,visit_id))
            retry_database(store)
            return
        except Exception:
            if attempt==2: raise
            time.sleep(1.5*(attempt+1))

def queue_visit_photo(visit_id,photo_data,photo_mime):
    future=photo_executor.submit(save_visit_photo,visit_id,photo_data,photo_mime)
    def finished(done):
        try: done.result()
        except Exception: app.logger.exception('Background photo storage failed for %s',visit_id)
    future.add_done_callback(finished)

def send_reset_email(recipient,token,otp,base_url):
    link=base_url.rstrip('/')+'/?reset='+token
    send_email(recipient,'Zoppler visitor management password reset','Use either the one-time link or OTP below within 30 minutes to reset your management login ID and password.\n\nReset link:\n'+link+'\n\nOTP: '+otp+'\n\nThe OTP permits a maximum of five attempts. If you did not request this, ignore this email.')

def send_reset_email_job(recipient,token,otp,token_hash,base_url):
    try: send_reset_email(recipient,token,otp,base_url)
    except Exception:
        with db_connection() as conn: conn.execute('DELETE FROM password_resets WHERE token_hash=%s',(token_hash,))
        raise

def send_approval_email(v,base_url):
    link=base_url.rstrip('/')+'/?view=admin'
    body=('A new visitor request is waiting for approval.\n\n'
          f"Reference: {v['id']}\nVisitor: {v['name']}\nCompany: {v['company']}\n"
          f"Meeting: {v['host']} ({v['department']})\nArrival: {v['visit_date']} {v['visit_time'].strftime('%H:%M')}\nExpected checkout: {v['expected_checkout_date']} {v['expected_checkout_time'].strftime('%H:%M')}\n"
          f"Purpose: {v['purpose']}\n\nReview and approve the request here:\n{link}\n")
    send_messages([(recipient,'Visitor approval required: '+v['id'],body) for recipient in sorted(RESET_EMAILS)])

def send_status_email(v):
    visitor_body=f"Your visit request {v['id']} is now {v['status']}.\n\nArrival: {v['visit_date']} {v['visit_time'].strftime('%H:%M')}\nExpected checkout: {v['expected_checkout_date']} {v['expected_checkout_time'].strftime('%H:%M')}\nPerson to meet: {v['host']}\n"
    management_body=(f"Visitor request {v['id']} changed to {v['status']}.\n\n"
                     f"Visitor: {v['name']}\nCompany: {v['company']}\nEmail: {v['email']}\nMobile: {v['mobile']}\n"
                     f"Meeting: {v['host']} ({v['department']})\nArrival: {v['visit_date']} {v['visit_time'].strftime('%H:%M')}\n"
                     f"Expected checkout: {v['expected_checkout_date']} {v['expected_checkout_time'].strftime('%H:%M')}\nPurpose: {v['purpose']}\n")
    deliveries=[(v['email'],'Zoppler visit request '+v['status'],visitor_body)]
    deliveries.extend((recipient,'Visitor status '+v['status']+': '+v['id'],management_body) for recipient in sorted(RESET_EMAILS))
    send_messages(deliveries)

def admin_required(fn):
    @wraps(fn)
    def inner(*args,**kwargs):
        if not session.get('admin_id'): return error('Please sign in',401)
        with db_connection() as conn: admin=conn.execute('SELECT session_version FROM admins WHERE id=%s',(session['admin_id'],)).fetchone()
        if not admin or admin['session_version']!=session.get('session_version'):
            session.clear();return error('Your session has expired. Please sign in again.',401)
        return fn(*args,**kwargs)
    return inner
@app.before_request
def csrf_check():
    if request.method in ('POST','PATCH','PUT','DELETE') and request.path!='/api/visits':
        origin=request.headers.get('Origin')
        if origin:
            origin_host=urlsplit(origin).netloc.lower()
            forwarded_host=request.headers.get('X-Forwarded-Host','').split(',')[0].strip().lower()
            allowed_hosts={request.host.lower(),forwarded_host}
            if not origin_host or origin_host not in allowed_hosts: return error('Invalid origin',403)
        if request.headers.get('Sec-Fetch-Site') not in (None,'same-origin','none'): return error('Invalid origin',403)
        if request.headers.get('X-CSRF-Token')!=session.get('csrf'): return error('Invalid session token',403)
@app.after_request
def headers(r):
    r.headers['X-Content-Type-Options']='nosniff';r.headers['X-Frame-Options']='DENY';r.headers['Referrer-Policy']='no-referrer';r.headers['Cache-Control']='no-store';r.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'"
    if request.is_secure:r.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
    return r
@app.get('/')
def index(): return send_from_directory(ROOT,'index.html')
@app.get('/<path:name>')
def assets(name):
    if name not in ('style.css','app.js','config.js','zopplerlabs_logo.jpg'): return error('Not found',404)
    return send_from_directory(ROOT,name)
@app.get('/api/session')
def session_info():
    authenticated=False
    if session.get('admin_id'):
        def current_admin():
            with db_connection() as conn: return conn.execute('SELECT session_version FROM admins WHERE id=%s',(session['admin_id'],)).fetchone()
        admin=retry_database(current_admin)
        authenticated=bool(admin and admin['session_version']==session.get('session_version'))
        if not authenticated: session.clear()
    if 'csrf' not in session: session['csrf']=secrets.token_urlsafe(32)
    return jsonify(authenticated=authenticated,csrf=session['csrf'],emailConfigured=email_delivery_configured())
@app.get('/api/health')
def health():
    try:
        with db_connection() as conn: conn.execute('SELECT 1').fetchone()
        return jsonify(ok=True)
    except Exception:
        app.logger.exception('Health check failed')
        return error('Service unavailable',503)
@app.post('/api/login')
def login():
    data=request.get_json(silent=True) or {}
    identity=str(data.get('username','')).strip()
    def find_admin():
        with db_connection() as conn: return conn.execute('SELECT id,password_hash,session_version FROM admins WHERE lower(username)=lower(%s) OR lower(email)=lower(%s)',(identity,identity)).fetchone()
    admin=retry_database(find_admin)
    if not admin or not check_password_hash(admin['password_hash'],str(data.get('password',''))): return error('Invalid username or password',401)
    session.clear();session['admin_id']=admin['id'];session['session_version']=admin['session_version'];session['csrf']=secrets.token_urlsafe(32)
    return jsonify(ok=True,csrf=session['csrf'])
@app.post('/api/logout')
def logout(): session.clear();return jsonify(ok=True)
@app.post('/api/change-password')
@admin_required
def change_password():
    data=request.get_json(silent=True) or {}
    current=str(data.get('currentPassword',''));new=str(data.get('newPassword',''))
    if len(new)<12: return error('New password must be at least 12 characters')
    if current==new: return error('New password must be different from the current password')
    with db_connection() as conn:
        admin=conn.execute('SELECT username,email,password_hash FROM admins WHERE id=%s',(session['admin_id'],)).fetchone()
        if not admin or not check_password_hash(admin['password_hash'],current): return error('Current password is incorrect',401)
        if new.casefold() in (admin['username'].casefold(),(admin['email'] or '').casefold()): return error('New password must be different from your login ID and email address')
        conn.execute('UPDATE admins SET password_hash=%s,session_version=session_version+1 WHERE id=%s',(generate_password_hash(new),session['admin_id']))
    session.clear();return jsonify(ok=True)
@app.post('/api/password-reset/request')
def request_password_reset():
    if not email_delivery_configured(): return error('Password reset email is not configured for this cloud server. Add RESEND_API_KEY and a verified sender in Render.',503)
    email=str((request.get_json(silent=True) or {}).get('email','')).strip().lower()
    if reset_rate_limited(email): return error('Too many reset requests. Please try again later.',429)
    reset_job=None
    if email in RESET_EMAILS:
        def create_reset():
            with db_connection() as conn:
                admin=conn.execute('SELECT id FROM admins WHERE lower(email)=%s',(email,)).fetchone()
                if not admin: return None
                token=secrets.token_urlsafe(40);token_hash=hashlib.sha256(token.encode()).hexdigest();otp=f'{secrets.randbelow(1000000):06d}';otp_hash=hashlib.sha256(otp.encode()).hexdigest()
                conn.execute('DELETE FROM password_resets WHERE admin_id=%s OR expires_at<now()',(admin['id'],))
                conn.execute('INSERT INTO password_resets(admin_id,token_hash,otp_hash,expires_at) VALUES (%s,%s,%s,%s)',(admin['id'],token_hash,otp_hash,datetime.now(timezone.utc)+timedelta(minutes=30)))
                return (email,token,otp,token_hash,request.host_url)
        reset_job=retry_database(create_reset)
    if reset_job:
        try: send_reset_email_job(*reset_job)
        except Exception:
            app.logger.exception('Password reset delivery failed')
            return error('The reset email could not be delivered. Check the cloud email configuration and try again.',503)
    return jsonify(message='If this address is authorized, a reset link has been sent.')
@app.post('/api/password-reset/confirm')
def confirm_password_reset():
    data=request.get_json(silent=True) or {};token=str(data.get('token',''));email=str(data.get('email','')).strip().lower();otp=str(data.get('otp','')).strip();username=str(data.get('username','')).strip();password=str(data.get('password',''))
    if not re.fullmatch(r'[A-Za-z0-9._-]{3,64}',username): return error('Login ID must be 3-64 characters and use only letters, numbers, dots, underscores, or hyphens')
    if len(password)<12: return error('Password must be at least 12 characters')
    if password.casefold()==username.casefold(): return error('Password must be different from the login ID')
    with db_connection() as conn:
        if token:
            token_hash=hashlib.sha256(token.encode()).hexdigest()
            reset=conn.execute('SELECT id,admin_id FROM password_resets WHERE token_hash=%s AND used_at IS NULL AND expires_at>now() FOR UPDATE',(token_hash,)).fetchone()
        elif email in RESET_EMAILS and re.fullmatch(r'\d{6}',otp):
            reset=conn.execute('SELECT r.id,r.admin_id,r.otp_hash,r.attempts FROM password_resets r JOIN admins a ON a.id=r.admin_id WHERE lower(a.email)=%s AND r.used_at IS NULL AND r.expires_at>now() ORDER BY r.created_at DESC LIMIT 1 FOR UPDATE OF r',(email,)).fetchone()
            if reset and (reset['attempts']>=5 or not secrets.compare_digest(reset['otp_hash'] or '',hashlib.sha256(otp.encode()).hexdigest())):
                conn.execute('UPDATE password_resets SET attempts=attempts+1 WHERE id=%s',(reset['id'],))
                return error('OTP is invalid, expired, or has exceeded the attempt limit',400)
        else: reset=None
        if not reset: return error('Reset link is invalid or expired',400)
        admin=conn.execute('SELECT email FROM admins WHERE id=%s',(reset['admin_id'],)).fetchone()
        if password.casefold()==(admin['email'] or '').casefold(): return error('Password must be different from the email address')
        if conn.execute('SELECT 1 FROM admins WHERE lower(username)=lower(%s) AND id<>%s',(username,reset['admin_id'])).fetchone(): return error('That login ID is already in use',409)
        conn.execute('UPDATE admins SET username=%s,password_hash=%s,session_version=session_version+1 WHERE id=%s',(username,generate_password_hash(password),reset['admin_id']))
        conn.execute('UPDATE password_resets SET used_at=now() WHERE admin_id=%s AND used_at IS NULL',(reset['admin_id'],))
    session.clear();return jsonify(ok=True)
@app.post('/api/visits')
def create():
    if submission_rate_limited(): return error('Too many visitor requests. Please try again later.',429)
    data=request.form
    fields=['name','mobile','email','company','host','department','date','time','checkoutDate','checkoutTime','purpose','aadhar']
    x={k:str(data.get(k,'')).strip() for k in fields}
    for k,limit in [('name',100),('mobile',20),('email',254),('company',120),('host',100),('department',100),('purpose',500),('aadhar',12)]:
        if len(x[k])>limit: return error('Invalid '+k)
    if not all(x[k] for k in fields): return error('All fields are required')
    if not re.fullmatch(r'[0-9+() -]{8,20}',x['mobile']) or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',x['email']): return error('Invalid contact details')
    x['email']=x['email'].lower()
    if not re.fullmatch(r'\d{12}',x['aadhar']): return error('Aadhaar number must contain exactly 12 digits')
    photo=request.files.get('photo')
    if not photo or not photo.filename: return error('Visitor photo is required')
    photo_data=photo.read(5*1024*1024+1)
    if len(photo_data)>5*1024*1024: return error('Photo must be 5 MB or smaller')
    if photo.mimetype not in ('image/jpeg','image/png','image/webp') or not valid_photo(photo_data,photo.mimetype): return error('Photo content must be a valid JPEG, PNG, or WebP image')
    try:
        day=date.fromisoformat(x['date']);tm=datetime.strptime(x['time'],'%H:%M').time()
        checkout_day=date.fromisoformat(x['checkoutDate']);checkout_tm=datetime.strptime(x['checkoutTime'],'%H:%M').time()
    except ValueError: return error('Invalid date or time')
    if day<date.today(): return error('Visit date cannot be in the past')
    if datetime.combine(checkout_day,checkout_tm)<=datetime.combine(day,tm): return error('Expected checkout must be after expected arrival')
    request_token=str(data.get('requestToken','')).strip()
    if request_token and not re.fullmatch(r'[A-Za-z0-9_-]{20,100}',request_token): return error('Invalid request token')
    vid='ZS-VIS-'+secrets.token_hex(8).upper()
    # Public submissions use a dedicated short-lived connection. This prevents
    # Render background health/photo work from exhausting the small shared pool.
    with connect(os.environ['DATABASE_URL'],row_factory=dict_row,connect_timeout=15) as conn:
        v=conn.execute('INSERT INTO visits (id,name,mobile,email,company,host,department,visit_date,visit_time,expected_checkout_date,expected_checkout_time,purpose,aadhar_number,request_token) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (request_token) WHERE request_token IS NOT NULL DO NOTHING RETURNING *',(vid,x['name'],x['mobile'],x['email'],x['company'],x['host'],x['department'],day,tm,checkout_day,checkout_tm,x['purpose'],x['aadhar'],request_token or None)).fetchone()
        inserted=bool(v)
        if not v: v=conn.execute('SELECT * FROM visits WHERE request_token=%s',(request_token,)).fetchone()
    if not v: raise RuntimeError('Visitor request could not be saved')
    queue_visit_photo(v['id'],photo_data,photo.mimetype)
    if inserted: queue_email(send_approval_email,email_visit(v),request.host_url)
    return (jsonify(public(v)),201) if inserted else jsonify(public(v))
@app.get('/api/visits/<vid>/status')
def status(vid):
    with db_connection() as conn: v=conn.execute('SELECT * FROM visits WHERE id=%s',(vid.strip().upper(),)).fetchone()
    return jsonify(public(v)) if v else error('Request not found',404)
@app.get('/api/visits/<vid>/photo')
@admin_required
def visitor_photo(vid):
    with db_connection() as conn: v=conn.execute('SELECT photo,photo_mime FROM visits WHERE id=%s',(vid,)).fetchone()
    if not v or not v['photo']: return error('Photo not found',404)
    return Response(bytes(v['photo']),mimetype=v['photo_mime'],headers={'Content-Disposition':'inline'})
@app.get('/api/visits')
@admin_required
def list_visits():
    with db_connection() as conn: rows=conn.execute('SELECT * FROM visits ORDER BY created_at DESC LIMIT 1000').fetchall()
    return jsonify([full(v) for v in rows])
@app.patch('/api/visits/<vid>/status')
@admin_required
def update(vid):
    target=str((request.get_json(silent=True) or {}).get('status',''))
    allowed={'Pending':('Approved','Rejected'),'Approved':('Checked In',),'Checked In':('Checked Out',)}
    with db_connection() as conn:
        v=conn.execute('SELECT * FROM visits WHERE id=%s FOR UPDATE',(vid,)).fetchone()
        if not v: return error('Request not found',404)
        if target not in allowed.get(v['status'],()): return error('Invalid status transition',409)
        updated=conn.execute('UPDATE visits SET status=%s,updated_at=now(),approved_by=CASE WHEN %s IN (\'Approved\',\'Rejected\') THEN %s ELSE approved_by END,checked_in_at=CASE WHEN %s=\'Checked In\' THEN now() ELSE checked_in_at END,checked_out_at=CASE WHEN %s=\'Checked Out\' THEN now() ELSE checked_out_at END WHERE id=%s RETURNING *',(target,target,session['admin_id'],target,target,vid)).fetchone()
        conn.execute('INSERT INTO visit_audit (visit_id,admin_id,old_status,new_status) VALUES (%s,%s,%s,%s)',(vid,session['admin_id'],v['status'],target))
    queue_email(send_status_email,email_visit(updated))
    return jsonify(full(updated))
@app.cli.command('create-admin')
def create_admin():
    import getpass
    username=input('Admin username: ').strip();password=getpass.getpass('Admin password (12+ characters): ')
    if not username or len(password)<12: raise SystemExit('Username required; password must be at least 12 characters')
    with db_connection() as conn: conn.execute('INSERT INTO admins(username,password_hash) VALUES (%s,%s) ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash',(username,generate_password_hash(password)))
    print('Admin account saved')
if __name__=='__main__': app.run(host='127.0.0.1',port=5000,debug=False)
