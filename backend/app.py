import os,json,hashlib,re,sqlite3,secrets,threading
from pathlib import Path
from datetime import datetime,timezone,timedelta
from urllib.parse import urlparse,urlunparse,parse_qsl,urlencode,unquote
import feedparser,requests
from dotenv import load_dotenv
from flask import Flask,jsonify,request
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
ROOT=Path(__file__).resolve().parent;load_dotenv(ROOT/".env");DB=ROOT/"news.db";SP=ROOT/"sources.json"
app=Flask(__name__)
_frontend_origins=[x.strip() for x in os.getenv("FRONTEND_ORIGINS","http://localhost:5173,http://127.0.0.1:5173").split(",") if x.strip()]
CORS(app,resources={r"/api/*":{"origins":_frontend_origins}}, allow_headers=["Content-Type","Authorization"])
_ADMIN_TOKENS=set();_ADMIN_LOCK=threading.Lock()
def admin_password(): return os.getenv("ADMIN_PASSWORD", "change-this-admin-password").strip()
@app.before_request
def protect_admin_api():
 path=request.path
 if not path.startswith("/api/") or path in ("/api/health","/api/admin/login") or path.startswith("/api/public/") or request.method=="OPTIONS": return None
 auth=request.headers.get("Authorization","")
 token=auth[7:].strip() if auth.lower().startswith("bearer ") else ""
 with _ADMIN_LOCK: valid=bool(token and token in _ADMIN_TOKENS)
 if not valid: return jsonify(error="Admin login required"),401
@app.post("/api/admin/login")
def admin_login():
 data=request.get_json(silent=True) or {};provided=str(data.get("password", ""))
 if not secrets.compare_digest(provided,admin_password()) or admin_password()=="": return jsonify(error="Invalid admin password"),401
 token=secrets.token_urlsafe(32)
 with _ADMIN_LOCK: _ADMIN_TOKENS.add(token)
 return jsonify(token=token,ok=True)
def now():return datetime.now(timezone.utc).isoformat(timespec="seconds")
def retention_cutoff():return (datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(timespec="seconds")
def link_fingerprint(link):return hashlib.sha256(canon(link).encode("utf-8")).hexdigest() if link else ""

DB_URL=os.getenv("DATABASE_URL","").strip()
DB_TYPE=os.getenv("DB_TYPE","mysql" if DB_URL.startswith(("mysql://","mysql+pymysql://")) else "sqlite").strip().lower()

class MySQLCursor:
 def __init__(self, cursor): self._cursor=cursor
 @property
 def lastrowid(self): return self._cursor.lastrowid
 @property
 def rowcount(self): return self._cursor.rowcount
 def fetchone(self): return self._cursor.fetchone()
 def fetchall(self): return self._cursor.fetchall()

class MySQLConnection:
 def __init__(self):
  import pymysql
  from urllib.parse import urlparse, unquote
  if not DB_URL: raise RuntimeError("Set DATABASE_URL in backend/.env for MySQL hosting.")
  u=urlparse(DB_URL.replace("mysql+pymysql://","mysql://",1))
  self.conn=pymysql.connect(host=u.hostname or "localhost",port=u.port or 3306,user=unquote(u.username or ""),password=unquote(u.password or ""),database=(u.path or "/").lstrip("/"),charset="utf8mb4",cursorclass=pymysql.cursors.DictCursor,autocommit=False)
 def execute(self,sql,params=()):
  sql=sql.replace("INSERT OR IGNORE INTO","INSERT IGNORE INTO").replace("?","%s")
  cur=self.conn.cursor()
  cur.execute(sql,params)
  return MySQLCursor(cur)
 def executescript(self,script):
  for statement in script.split(";"):
   if statement.strip(): self.execute(statement)
 def commit(self): self.conn.commit()
 def rollback(self): self.conn.rollback()
 def close(self): self.conn.close()
 def __enter__(self): return self
 def __exit__(self,typ,val,tb):
  if typ is None:self.commit()
  else:self.rollback()
  self.close()

def db():
 if DB_TYPE=="mysql":
  return MySQLConnection()
 c=sqlite3.connect(DB);c.row_factory=sqlite3.Row
 return c

def init():
 if DB_TYPE=="mysql":
  with db() as c:
   c.executescript("""
   CREATE TABLE IF NOT EXISTS news(id BIGINT PRIMARY KEY AUTO_INCREMENT,source TEXT,source_url TEXT,title TEXT,summary TEXT,link VARCHAR(700) UNIQUE,lang VARCHAR(8),category VARCHAR(100),pubdate TEXT,fetched TEXT,hash VARCHAR(64),status VARCHAR(40) DEFAULT 'collected',verification VARCHAR(80) DEFAULT 'needs_review');
   CREATE TABLE IF NOT EXISTS articles(id BIGINT PRIMARY KEY AUTO_INCREMENT,news_id BIGINT UNIQUE,lang VARCHAR(8),title TEXT,slug VARCHAR(255) UNIQUE,summary TEXT,body LONGTEXT,conclusion TEXT,seo_title TEXT,meta TEXT,image_url TEXT,image_credit TEXT,image_source_url TEXT,status VARCHAR(40) DEFAULT 'draft',created VARCHAR(40),published VARCHAR(40) DEFAULT '',FOREIGN KEY(news_id) REFERENCES news(id));
   CREATE TABLE IF NOT EXISTS runs(id BIGINT PRIMARY KEY AUTO_INCREMENT,started TEXT,finished TEXT,status VARCHAR(40),seen INT,added INT,message TEXT);
   CREATE TABLE IF NOT EXISTS expired_news_fingerprints(fingerprint CHAR(64) PRIMARY KEY,expired_at TEXT NOT NULL);
   """)
   cols={r["Field"] for r in c.execute("SHOW COLUMNS FROM articles").fetchall()}
   for name in ("image_url","image_credit","image_source_url"):
    if name not in cols:c.execute(f"ALTER TABLE articles ADD COLUMN {name} TEXT")
 else:
  with db() as c:
   c.executescript("""
   CREATE TABLE IF NOT EXISTS news(id INTEGER PRIMARY KEY,source TEXT,source_url TEXT,title TEXT,summary TEXT,link TEXT UNIQUE,lang TEXT,category TEXT,pubdate TEXT,fetched TEXT,hash TEXT,status TEXT DEFAULT 'collected',verification TEXT DEFAULT 'needs_review');
   CREATE TABLE IF NOT EXISTS articles(id INTEGER PRIMARY KEY,news_id INTEGER UNIQUE,lang TEXT,title TEXT,slug TEXT UNIQUE,summary TEXT,body TEXT,conclusion TEXT,seo_title TEXT,meta TEXT,image_url TEXT DEFAULT '',image_credit TEXT DEFAULT '',image_source_url TEXT DEFAULT '',status TEXT DEFAULT 'draft',created TEXT,published TEXT DEFAULT '',FOREIGN KEY(news_id) REFERENCES news(id));
   CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,started TEXT,finished TEXT,status TEXT,seen INTEGER,added INTEGER,message TEXT);
   CREATE TABLE IF NOT EXISTS expired_news_fingerprints(fingerprint TEXT PRIMARY KEY,expired_at TEXT NOT NULL);
   """)
   cols={r[1] for r in c.execute("PRAGMA table_info(articles)").fetchall()}
   for name in ("image_url","image_credit","image_source_url"):
    if name not in cols:c.execute(f"ALTER TABLE articles ADD COLUMN {name} TEXT DEFAULT ''")
def get_sources():
 try:return json.loads(SP.read_text(encoding="utf-8"))
 except:return []
def clean(s):return re.sub(r"\s+"," ",re.sub(r"<[^>]+>"," ",s or "")).strip()
def canon(u):
 p=urlparse(u);q=[(k,v) for k,v in parse_qsl(p.query) if not k.lower().startswith("utm_") and k.lower() not in ("fbclid","gclid")]
 return urlunparse((p.scheme.lower(),p.netloc.lower(),p.path.rstrip("/") or "/","",urlencode(q),""))
def max_news_age_hours():
 try:
  return max(1, min(168, int(os.getenv("LIVE_NEWS_MAX_AGE_HOURS", "12"))))
 except (TypeError, ValueError):
  return 24

def parse_feed_datetime(entry):
 """Return source publication/update time as a UTC ISO string, or empty."""
 parsed=entry.get("published_parsed") or entry.get("updated_parsed")
 if parsed:
  try:
   return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat(timespec="seconds")
  except (TypeError, ValueError, OverflowError):
   pass
 raw=entry.get("published") or entry.get("updated") or ""
 if raw:
  try:
   dt=datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
   if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
   return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
  except (TypeError, ValueError):
   pass
 return ""

def is_recent_news(pubdate, max_age_hours=None):
 """Strict freshness check; undated stories are not treated as live news."""
 if not pubdate:return False
 try:
  dt=datetime.fromisoformat(str(pubdate).replace("Z", "+00:00"))
  if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
  dt=dt.astimezone(timezone.utc)
 except (TypeError, ValueError):
  return False
 age=datetime.now(timezone.utc)-dt
 return timedelta(minutes=-5) <= age <= timedelta(hours=max_age_hours or max_news_age_hours())

def collect():
 start=now();seen=added=skipped_old=skipped_undated=0;errors=[];max_age=max_news_age_hours()
 with db() as c:rid=c.execute("INSERT INTO runs(started,status,seen,added,message) VALUES(?,?,?,?,?)",(start,"running",0,0,"")).lastrowid
 for s in get_sources():
  if not s.get("enabled",True):continue
  try:
   feed_response=requests.get(s["url"],headers={"User-Agent":"AINewsroom/2.0 (+RSS reader)"},timeout=18)
   feed_response.raise_for_status()
   f=feedparser.parse(feed_response.content)
   if not f.entries:
    errors.append(s["name"]+": feed returned no entries" + (f" (HTTP {feed_response.status_code})" if getattr(feed_response,"status_code",None) else ""));continue
   for e in f.entries[:100]:
    link=canon(e.get("link",""));title=clean(e.get("title",""))
    if not link or not title:continue
    seen+=1
    pubdate=parse_feed_datetime(e)
    if not pubdate:
     skipped_undated+=1;continue
    if not is_recent_news(pubdate,max_age):
     skipped_old+=1;continue
    summary=clean(e.get("summary",e.get("description","")))[:4000];h=hashlib.sha256((title.lower()+link).encode()).hexdigest()
    fingerprint=link_fingerprint(link)
    with db() as c:
     # Keep only a one-way URL fingerprint after expiry; never recreate expired story content.
     if c.execute("SELECT 1 FROM expired_news_fingerprints WHERE fingerprint=?",(fingerprint,)).fetchone():continue
     cur=c.execute("INSERT OR IGNORE INTO news(source,source_url,title,summary,link,lang,category,pubdate,fetched,hash,status,verification) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(s["name"],s["url"],title,summary,link,s.get("language","en"),s.get("category","General"),pubdate,now(),h,"collected","needs_review"))
     if cur.rowcount > 0:
      added += cur.rowcount
     else:
      # A story may appear first in a broad India/World feed and later in a dedicated
      # Politics/Sports feed. Preserve the more specific category instead of letting
      # the first feed permanently determine where the story appears.
      incoming=(s.get("category") or "General").strip()
      existing=c.execute("SELECT id,category FROM news WHERE link=?",(link,)).fetchone()
      specific={"Politics","Sports","Business","Technology","Entertainment","Science","Health","Education","Environment","Lifestyle","Finance"}
      if existing and incoming in specific and (existing["category"] or "General") in {"India","World","General","Top Stories"}:
       c.execute("UPDATE news SET category=?,source=?,source_url=? WHERE id=?",(incoming,s["name"],s["url"],existing["id"]))
  except Exception as ex:errors.append(s["name"]+": "+str(ex)[:140])
 status="completed" if not errors else ("partial" if seen else "failed");msg="; ".join(errors) if errors else "Live RSS collection completed.";msg+=f" Freshness window: {max_age}h; skipped old: {skipped_old}; skipped undated: {skipped_undated}."
 with db() as c:c.execute("UPDATE runs SET finished=?,status=?,seen=?,added=?,message=? WHERE id=?",(now(),status,seen,added,msg,rid))
 return {"run_id":rid,"status":status,"items_seen":seen,"items_added":added,"skipped_old":skipped_old,"skipped_undated":skipped_undated,"freshness_window_hours":max_age,"message":msg}
def slug(s):return re.sub("-+","-",re.sub(r"[^\w-]","",re.sub(r"\s+","-",s.lower())))[:75]+"-"+hashlib.md5(s.encode()).hexdigest()[:6]
def valid_api_key(value):
 value=(value or "").strip()
 return bool(value and not value.upper().startswith(("PASTE_", "YOUR_", "REPLACE_", "<")))
def groq_key():
 return os.getenv("GROQ_API_KEY", "").strip()
def generic_key():
 for name in ("LLM_API_KEY", "OPENAI_API_KEY"):
  value=os.getenv(name, "").strip()
  if valid_api_key(value):return value
 return ""
def ollama_enabled():
 return os.getenv("OLLAMA_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
def ai_provider():
 if valid_api_key(groq_key()):return "Groq API"
 if generic_key():return "Custom OpenAI-compatible API"
 if ollama_enabled():return "Ollama (local)"
 return "Not configured"
def generate(n):
 lang="Hindi" if n["lang"]=="hi" else "English"
 prompt=f"""Write an original cautious news draft in {lang} using only the RSS metadata below. Never invent facts, quotes, numbers or confirmations. Clearly distinguish claims from verified facts. Return a JSON object with title,summary,body,conclusion,seo_title,meta,image_search_query. image_search_query must be a short English search phrase for a genuinely relevant editorial photograph, not a generic news image. Source {n['source']}; URL {n['link']}; headline {n['title']}; summary {n['summary']}. Conclusion must state supported significance and uncertainty."""
 messages=[{"role":"system","content":"Evidence-grounded newsroom assistant. Return valid JSON only, with all requested fields as strings."},{"role":"user","content":prompt}]
 key=groq_key()
 if valid_api_key(key):
  base="https://api.groq.com/openai/v1"
  model=os.getenv("GROQ_MODEL","llama-3.3-70b-versatile").strip()
  response=requests.post(base+"/chat/completions",headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"},json={"model":model,"temperature":0.3,"response_format":{"type":"json_object"},"messages":messages},timeout=90)
  if not response.ok:
   raise RuntimeError(f"Groq API error {response.status_code}: {response.text[:500]}")
  raw=response.json()["choices"][0]["message"]["content"].strip()
  raw=re.sub(r"^```(?:json)?\s*|\s*```$","",raw,flags=re.I)
  d=json.loads(raw)
 elif generic_key():
  key=generic_key()
  base=os.getenv("LLM_BASE_URL","https://api.openai.com/v1").rstrip("/")
  model=os.getenv("LLM_MODEL","gpt-4o-mini")
  response=requests.post(base+"/chat/completions",headers={"Authorization":"Bearer "+key},json={"model":model,"temperature":0.3,"response_format":{"type":"json_object"},"messages":messages},timeout=90)
  if not response.ok:raise RuntimeError(f"Configured LLM API error {response.status_code}: {response.text[:500]}")
  raw=re.sub(r"^```(?:json)?\s*|\s*```$","",response.json()["choices"][0]["message"]["content"].strip(),flags=re.I);d=json.loads(raw)
 elif ollama_enabled():
  response=requests.post(os.getenv("OLLAMA_BASE_URL","http://127.0.0.1:11434").rstrip("/")+"/api/chat",json={"model":os.getenv("OLLAMA_MODEL","llama3.2"),"stream":False,"format":"json","messages":messages},timeout=180)
  response.raise_for_status();d=json.loads(response.json().get("message",{}).get("content","").strip())
 else:
  raise RuntimeError("Groq API key missing. Add GROQ_API_KEY in backend/.env and restart the backend.")
 if not all(isinstance(d.get(k),str) and d[k].strip() for k in ["title","summary","body","conclusion","seo_title","meta"]):raise ValueError("Invalid AI output schema: expected title, summary, body, conclusion, seo_title, and meta strings")
 if not isinstance(d.get("image_search_query"),str) or not d.get("image_search_query","").strip():
  d["image_search_query"]=" ".join((d.get("title",n["title"]) or n["title"]).split()[:8])
 return d

def find_relevant_image(query, title="", category=""):
 """Find a relevant openly licensed Wikimedia Commons image, trying title and category fallbacks."""
 category_terms={
  "India":"India news parliament New Delhi", "World":"world news international diplomacy",
  "Politics":"parliament government politics", "Business":"business economy office",
  "Technology":"technology computer artificial intelligence", "Sports":"sports stadium",
  "Entertainment":"film cinema entertainment", "Science":"science laboratory research",
  "Health":"hospital public health medicine", "Education":"school university education",
  "Environment":"environment climate nature", "Lifestyle":"culture travel lifestyle",
  "Finance":"finance banking economy", "General":"news reporting journalism"
 }
 raw=[query,title,category_terms.get(category,"news journalism")]
 queries=[]
 for item in raw:
  item=clean(item)
  # Wikimedia search works best with concise English terms. Avoid long article bodies.
  words=re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*",item)
  phrase=" ".join(words[:7]).strip()
  if phrase and phrase.lower() not in [x.lower() for x in queries]:queries.append(phrase)
 if not queries:queries=["news journalism"]
 headers={"User-Agent":"AINewsroom/2.0 (Wikimedia Commons image search)"}
 best=None
 for search_query in queries:
  try:
   response=requests.get("https://commons.wikimedia.org/w/api.php",params={
    "action":"query","format":"json","generator":"search","gsrsearch":search_query,
    "gsrnamespace":6,"gsrlimit":12,"prop":"imageinfo","iiprop":"url|extmetadata","iiurlwidth":1400
   },headers=headers,timeout=10)
   response.raise_for_status();pages=response.json().get("query",{}).get("pages",{})
   tokens={t.lower() for t in re.findall(r"[a-zA-Z]{3,}",search_query)}
   candidates=[]
   for page in pages.values():
    title_text=page.get("title","")
    info=(page.get("imageinfo") or [{}])[0]
    url=info.get("thumburl") or info.get("url","")
    if not url or not url.startswith("https://"):continue
    if not re.search(r"\.(jpg|jpeg|png|webp)(?:$|\?)",url,re.I):continue
    meta=info.get("extmetadata") or {}
    license_name=clean((meta.get("LicenseShortName") or {}).get("value",""))
    artist=clean((meta.get("Artist") or {}).get("value",""))
    desc=clean((meta.get("ImageDescription") or {}).get("value",""))
    lic_lower=license_name.lower()
    if not license_name or any(x in lic_lower for x in ("unknown","copyright","fair use","all rights reserved","non-free")):continue
    words={t.lower() for t in re.findall(r"[a-zA-Z]{3,}",title_text+" "+desc)}
    score=len(tokens & words)
    # Prefer permissive licenses and meaningful title/description overlap.
    if any(x in lic_lower for x in ("cc0","public domain","cc by","cc-by")):score+=1
    page_url="https://commons.wikimedia.org/wiki/"+title_text.replace(" ","_")
    candidates.append((score,url,license_name,artist,page_url,title_text))
   if candidates:
    candidates.sort(key=lambda x:x[0],reverse=True)
    candidate=candidates[0]
    # First query is article-specific; only accept weak overlap for category fallback.
    threshold=1 if search_query==queries[0] else 0
    if candidate[0]>=threshold:
     _,url,lic,artist,page_url,filename=candidate
     credit="Wikimedia Commons: "+(artist or filename)+(" · "+lic if lic else "")
     return {"image_url":url,"image_credit":credit,"image_source_url":page_url}
  except Exception as e:
   app.logger.warning("Wikimedia image search failed for %r: %s",search_query,str(e)[:160])
 return {"image_url":"","image_credit":"","image_source_url":""}

def delete_managed_local_image(image_url):
 """Delete only image files inside app-managed uploads/media folders; never remote/shared files."""
 if not image_url:return False
 try:
  parsed=urlparse(str(image_url));host=(parsed.hostname or "").lower()
  if parsed.scheme in ("http","https") and host not in ("localhost","127.0.0.1"):
   return False
  if parsed.scheme not in ("", "http", "https"):return False
  parts=[part for part in unquote(parsed.path if parsed.scheme else str(image_url).split("?",1)[0]).replace("\\","/").split("/") if part and part not in (".","..")]
  roots={
   "uploads":[ROOT/"uploads",ROOT/"static"/"uploads",ROOT.parent/"frontend"/"public"/"uploads"],
   "media":[ROOT/"media",ROOT/"static"/"media",ROOT.parent/"frontend"/"public"/"media"],
  }
  for i,part in enumerate(parts):
   if part not in roots or i+1>=len(parts):continue
   rel=Path(*parts[i+1:])
   for base in roots[part]:
    base_resolved=base.resolve();candidate=(base_resolved/rel).resolve()
    if candidate.is_relative_to(base_resolved) and candidate.is_file():
     candidate.unlink();return True
 except Exception as exc:
  app.logger.warning("Could not delete managed local image: %s",str(exc)[:160])
 return False

def cleanup_expired_articles():
 """Permanently remove articles 24h after their publish timestamp and tombstone their RSS URL."""
 cutoff=retention_cutoff();deleted=0;deleted_images=0
 with db() as c:
  expired=[dict(r) for r in c.execute("""
   SELECT a.id,a.news_id,a.image_url,n.link
   FROM articles a LEFT JOIN news n ON n.id=a.news_id
   WHERE COALESCE(a.published,'')<>'' AND a.published<=?
  """,(cutoff,)).fetchall()]
  for item in expired:
   fingerprint=link_fingerprint(item.get("link") or "")
   if fingerprint:
    c.execute("INSERT OR IGNORE INTO expired_news_fingerprints(fingerprint,expired_at) VALUES(?,?)",(fingerprint,now()))
   if delete_managed_local_image(item.get("image_url")):deleted_images+=1
   c.execute("DELETE FROM articles WHERE id=?",(item["id"],))
   if item.get("news_id") is not None:c.execute("DELETE FROM news WHERE id=?",(item["news_id"],))
   deleted+=1
 return {"deleted_articles":deleted,"deleted_local_images":deleted_images,"cutoff":cutoff}

@app.get("/api/health")
def health():return jsonify(ok=True,time=now())
@app.get("/api/dashboard")
def dashboard():
 with db() as c:
  cutoff=retention_cutoff()
  counts={
   "collected":c.execute("SELECT COUNT(*) FROM news n WHERE NOT EXISTS (SELECT 1 FROM articles a WHERE a.news_id=n.id AND COALESCE(a.published,'')<>'' AND a.published<=?)",(cutoff,)).fetchone()[0],
   "needs_review":c.execute("SELECT COUNT(*) FROM news n WHERE n.verification='needs_review' AND NOT EXISTS (SELECT 1 FROM articles a WHERE a.news_id=n.id AND COALESCE(a.published,'')<>'' AND a.published<=?)",(cutoff,)).fetchone()[0],
   "drafts":c.execute("SELECT COUNT(*) FROM articles WHERE status='draft' AND (COALESCE(published,'')='' OR published>?)",(cutoff,)).fetchone()[0],
   "published":c.execute("SELECT COUNT(*) FROM articles WHERE status='published' AND published>?",(cutoff,)).fetchone()[0],
   "runs":c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
  }
  runs=[dict(x) for x in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8")]
 return jsonify(counts=counts,recent_runs=runs,sources=get_sources())
@app.get("/api/news")
def list_news():
 cutoff=retention_cutoff()
 with db() as c:rows=c.execute("SELECT n.* FROM news n WHERE NOT EXISTS (SELECT 1 FROM articles a WHERE a.news_id=n.id AND COALESCE(a.published,'')<>'' AND a.published<=?) ORDER BY n.id DESC LIMIT 250",(cutoff,)).fetchall()
 return jsonify([dict(x) for x in rows])
@app.post("/api/news/<int:nid>/generate")
def generate_route(nid):
 with db() as c:
  n=c.execute("SELECT * FROM news WHERE id=?",(nid,)).fetchone()
  if not n:return jsonify(error="News not found"),404
  n=dict(n)
 try:
  d=generate(n);img=find_relevant_image(d.get("image_search_query","")+" "+d["title"],d["title"],n.get("category",""))
 except Exception as e:return jsonify(error="AI generation failed: "+str(e)[:200]),502
 with db() as c:
  old=c.execute("SELECT id FROM articles WHERE news_id=?",(nid,)).fetchone();sl=slug(d["title"]+"-"+str(nid))
  if old:
   aid=old["id"];c.execute("UPDATE articles SET title=?,summary=?,body=?,conclusion=?,seo_title=?,meta=?,image_url=?,image_credit=?,image_source_url=?,status='draft' WHERE id=?",(d["title"],d["summary"],d["body"],d["conclusion"],d["seo_title"],d["meta"],img["image_url"],img["image_credit"],img["image_source_url"],aid))
  else:aid=c.execute("INSERT INTO articles(news_id,lang,title,slug,summary,body,conclusion,seo_title,meta,image_url,image_credit,image_source_url,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(nid,n["lang"],d["title"],sl,d["summary"],d["body"],d["conclusion"],d["seo_title"],d["meta"],img["image_url"],img["image_credit"],img["image_source_url"],"draft",now())).lastrowid
  c.execute("UPDATE news SET status='generated' WHERE id=?",(nid,));a=c.execute("SELECT * FROM articles WHERE id=?",(aid,)).fetchone()
 return jsonify(dict(a)),201
@app.get("/api/articles")
def all_articles():
 cutoff=retention_cutoff()
 with db() as c:rows=c.execute("SELECT a.*,n.source,n.link,n.category,n.verification FROM articles a JOIN news n ON n.id=a.news_id WHERE COALESCE(a.published,'')='' OR a.published>? ORDER BY a.id DESC",(cutoff,)).fetchall()
 return jsonify([dict(x) for x in rows])
@app.patch("/api/articles/<int:aid>")
def edit_article(aid):
 d=request.get_json(silent=True) or {};allowed=["title","summary","body","conclusion","seo_title","meta","lang"];f={k:d[k] for k in allowed if isinstance(d.get(k),str)}
 if not f:return jsonify(error="No editable fields"),400
 with db() as c:
  if not c.execute("SELECT id FROM articles WHERE id=?",(aid,)).fetchone():return jsonify(error="Article not found"),404
  c.execute("UPDATE articles SET "+",".join(k+"=?" for k in f)+" WHERE id=?",(*f.values(),aid));a=c.execute("SELECT * FROM articles WHERE id=?",(aid,)).fetchone()
 return jsonify(dict(a))
@app.post("/api/articles/<int:aid>/publish")
def publish(aid):
 with db() as c:
  a=c.execute("SELECT * FROM articles WHERE id=?",(aid,)).fetchone()
  if not a:return jsonify(error="Article not found"),404
  c.execute("UPDATE articles SET status='published',published=? WHERE id=?",(now(),aid));return jsonify(dict(c.execute("SELECT * FROM articles WHERE id=?",(aid,)).fetchone()))
@app.post("/api/articles/<int:aid>/unpublish")
def unpublish(aid):
 # Keep published timestamp: unpublishing must not reset the 24-hour retention clock.
 with db() as c:c.execute("UPDATE articles SET status='draft' WHERE id=?",(aid,))
 return jsonify(ok=True)
@app.get("/api/public/articles")
def public_list():
 cutoff=retention_cutoff()
 with db() as c:rows=c.execute("SELECT a.id,a.lang AS language,a.title,a.slug,a.summary,a.body,a.conclusion,a.seo_title,a.meta,a.image_url,a.image_credit,a.image_source_url,a.published,n.source,n.link,n.category FROM articles a JOIN news n ON n.id=a.news_id WHERE a.status='published' AND a.published>? AND n.pubdate >= ? ORDER BY a.published DESC",(cutoff, (datetime.now(timezone.utc)-timedelta(hours=max_news_age_hours())).isoformat(timespec="seconds"))).fetchall()
 return jsonify([dict(x) for x in rows])
@app.get("/api/public/articles/<slugval>")
def public_one(slugval):
 cutoff=retention_cutoff()
 with db() as c:a=c.execute("SELECT a.id,a.lang AS language,a.title,a.slug,a.summary,a.body,a.conclusion,a.seo_title,a.meta,a.image_url,a.image_credit,a.image_source_url,a.published,n.source,n.link,n.category FROM articles a JOIN news n ON n.id=a.news_id WHERE a.slug=? AND a.status='published' AND a.published>? AND n.pubdate >= ?",(slugval,cutoff,(datetime.now(timezone.utc)-timedelta(hours=max_news_age_hours())).isoformat(timespec="seconds"))).fetchone()
 if not a:return jsonify(error="Article not found"),404
 return jsonify(dict(a))
def ai_configured():
 return ai_provider() != "Not configured"
def auto_pipeline():
 """Clean expired stories, collect RSS, generate missing articles, and auto-publish drafts."""
 retention=cleanup_expired_articles()
 result=collect();generated=published=failed=0;failure_details=[]
 auto_publish=os.getenv("AUTO_PUBLISH","true").strip().lower() in ("1","true","yes","on")
 if not ai_configured():
  message=(result.get("message","")+f" | Retention cleanup: {retention['deleted_articles']} expired articles deleted ({retention['deleted_local_images']} local images). | AUTO PIPELINE PAUSED: no AI provider configured.").strip(" |")
  with db() as c:c.execute("UPDATE runs SET message=? WHERE id=?",(message,result["run_id"]))
  return {**result,"generated":0,"published":0,"failed":0,"auto_publish":auto_publish,"message":message}
 # Backfill images for already-published stories too; old versions only tried drafts.
 with db() as c:
  missing_images=[dict(r) for r in c.execute("SELECT a.id,a.news_id,a.title,a.summary,n.category FROM articles a JOIN news n ON n.id=a.news_id WHERE COALESCE(a.image_url,'')='' ORDER BY CASE WHEN a.status='published' THEN 0 ELSE 1 END,a.id ASC LIMIT 40").fetchall()]
 for item in missing_images:
  try:
   img=find_relevant_image(item["title"]+" "+(item["summary"] or ""),item["title"],item["category"])
   if img["image_url"]:
    with db() as c:c.execute("UPDATE articles SET image_url=?,image_credit=?,image_source_url=? WHERE id=?",(img["image_url"],img["image_credit"],img["image_source_url"],item["id"]))
  except Exception:
   app.logger.exception("Image backfill failed for article id %s",item["id"])
 # Publish drafts that were generated earlier; no manual publish click is required.
 with db() as c:
  drafts=[dict(r) for r in c.execute("SELECT id,news_id FROM articles WHERE status='draft' ORDER BY id ASC LIMIT 100").fetchall()]
 for a in drafts:
  try:
   if auto_publish:
    with db() as c:
     c.execute("UPDATE articles SET status='published',published=? WHERE id=? AND status='draft'",(now(),a["id"]))
     c.execute("UPDATE news SET status='published' WHERE id=?",(a["news_id"],))
    published+=1
  except Exception:
   failed+=1;failure_details.append(f"draft {a['id']}: {str(__import__('sys').exc_info()[1])[:180]}");app.logger.exception("Auto-publish failed for existing draft id %s",a["id"])
 with db() as c:
  # Fair category rotation: a large backlog or repeatedly failing feed must not
  # let India/World stories occupy every generation slot and starve Politics/Sports.
  pending=[dict(r) for r in c.execute("""
   SELECT * FROM (
    SELECT n.*, ROW_NUMBER() OVER (
     PARTITION BY COALESCE(NULLIF(n.category,''),'General')
     ORDER BY n.id DESC
    ) AS category_rank
    FROM news n LEFT JOIN articles a ON a.news_id=n.id
    WHERE a.id IS NULL
   )
   WHERE category_rank <= 8
   ORDER BY id DESC
   LIMIT 100
  """).fetchall()]
 for n in pending:
  try:
   d=generate(n)
   img=find_relevant_image(d.get("image_search_query","")+" "+d["title"],d["title"],n.get("category",""))
   with db() as c:
    existing=c.execute("SELECT id FROM articles WHERE news_id=?",(n["id"],)).fetchone()
    if existing:
     aid=existing["id"]
    else:
     sl=slug(d["title"]+"-"+str(n["id"]))
     aid=c.execute("INSERT INTO articles(news_id,lang,title,slug,summary,body,conclusion,seo_title,meta,image_url,image_credit,image_source_url,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(n["id"],n["lang"],d["title"],sl,d["summary"],d["body"],d["conclusion"],d["seo_title"],d["meta"],img["image_url"],img["image_credit"],img["image_source_url"],"draft",now())).lastrowid
    c.execute("UPDATE news SET status='generated' WHERE id=?",(n["id"],))
    if auto_publish:
     c.execute("UPDATE articles SET status='published',published=? WHERE id=?",(now(),aid));c.execute("UPDATE news SET status='published' WHERE id=?",(n["id"],));published+=1
    generated+=1
  except Exception as ex:
   failed+=1;failure_details.append(f"news {n['id']}: {str(ex)[:180]}");app.logger.exception("Auto pipeline failed for news id %s",n["id"])
 message=f"{result.get('message','')} | Auto pipeline: {generated} generated, {published} published, {failed} failed; image backfill checked {len(missing_images)}; auto_publish={auto_publish}. | Retention cleanup: {retention['deleted_articles']} expired articles deleted ({retention['deleted_local_images']} local images).".strip(" |")
 if failure_details: message += " Failure details: " + " || ".join(failure_details[:5])
 if not auto_publish:message += " Drafts await editorial review."
 with db() as c:c.execute("UPDATE runs SET message=? WHERE id=?",(message,result["run_id"]))
 return {**result,"generated":generated,"published":published,"failed":failed,"auto_publish":auto_publish,"message":message}
def scheduled():
 try:auto_pipeline()
 except Exception:app.logger.exception("Scheduled automation pipeline failed")
@app.get("/api/automation")
def automation_status():
 with db() as c:r=c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
 return jsonify(enabled=True,interval_minutes=max(5,int(os.getenv("COLLECTION_INTERVAL_MINUTES","5"))),live_news_max_age_hours=max_news_age_hours(),retention_hours=24,retention_policy="delete 24 hours after publish",ai_configured=ai_configured(),ai_provider=ai_provider(),ollama_enabled=ollama_enabled(),ollama_model=os.getenv("OLLAMA_MODEL","llama3.2"),last_run=dict(r) if r else None)
@app.post("/api/collect")
def collect_now():
 # The manual button runs the same complete pipeline as the scheduler.
 return jsonify(auto_pipeline())
if __name__=="__main__":
 init();sched=BackgroundScheduler(daemon=True);sched.add_job(scheduled,"date",run_date=datetime.now(timezone.utc),id="startup_pipeline");sched.add_job(scheduled,"interval",minutes=max(5,int(os.getenv("COLLECTION_INTERVAL_MINUTES","5"))),max_instances=1,coalesce=True);sched.start();app.logger.info("Automatic news pipeline enabled: every %s minutes",max(5,int(os.getenv("COLLECTION_INTERVAL_MINUTES","5"))));app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")),debug=False,use_reloader=False)
