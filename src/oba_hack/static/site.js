// 시작 페이지(/skt, /vooy) 공용 도우미
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const won = n => (n ?? 0).toLocaleString("ko-KR") + "원";
const sleep = ms => new Promise(r => setTimeout(r, ms));
// 시각은 보는 사람의 브라우저 시간대와 상관없이 한국 시간으로 (서버 기록·엄마 문자와 같은 기준)
const hhmm = ts => new Date(ts * 1000).toLocaleTimeString("en-GB", {timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
// 서버가 주는 시각 문자열은 한국 시간이고 시간대 표기가 없다 → +09:00으로 읽는다
const kstDate = s => new Date(/(Z|[+-]\d\d:?\d\d)$/i.test(s) ? s : s + "+09:00");
// 지금 한국 시각을 ISO 8601(+09:00)로
const kstNowIso = () => new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 19) + "+09:00";

// 계정 토큰 저장 키: 시작 페이지마다 따로 둬서 SKT 체험과 vooy 체험 계정이 섞이지 않게 한다
let ACC_KEY = "dcmm-account";
function useAccountKey(k) { ACC_KEY = k; }
function getAccount() { try { return localStorage.getItem(ACC_KEY); } catch (_) { return null; } }
function setAccount(t) { try { t ? localStorage.setItem(ACC_KEY, t) : localStorage.removeItem(ACC_KEY); } catch (_) {} }

async function api(path, opts = {}) {
  const r = await fetch(path, {...opts, headers: {"Content-Type": "application/json", "X-Account": getAccount() || "", ...(opts.headers || {})}});
  if (!r.ok) {
    let detail = ""; try { detail = (await r.json()).detail; } catch (_) {}
    const e = new Error(typeof detail === "string" && detail ? detail : `HTTP ${r.status}`); e.status = r.status; throw e;
  }
  return r.json();
}

// 저장된 계정이 살아 있으면 그대로, 아니면 체험 계정을 새로 만든다 (audience: skt | vooy)
async function ensureAccount(audience, fresh = false) {
  if (!fresh && getAccount()) {
    try { return await api("/api/state"); } catch (e) { if (e.status !== 401) throw e; }
  }
  const made = await api("/api/demo", {method: "POST", body: JSON.stringify({audience})});
  setAccount(made.dashboard_token);
  return api("/api/state");
}

function absolute(url) { return /^https?:/.test(url) ? url : location.origin + url; }

let toastTimer;
function toast(text) {
  let t = $("#toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.append(t); }
  t.textContent = text; t.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (_) {
    const ta = document.createElement("textarea"); ta.value = text; document.body.append(ta); ta.select();
    try { document.execCommand("copy"); } finally { ta.remove(); }
  }
  toast("복사했어요");
}

// <div class="code"><pre>…</pre></div> 에 복사 버튼을 붙인다
function codeBlock(text, {copy = true} = {}) {
  return `<div class="code"><pre>${esc(text)}</pre>${copy ? `<button class="copy" type="button" data-copytext="${esc(text)}">복사</button>` : ""}</div>`;
}
document.addEventListener("click", e => {
  const b = e.target.closest("[data-copytext]");
  if (b) copyText(b.dataset.copytext);
});

// 통화 텍스트를 말풍선으로: 자녀 쪽 화자는 오른쪽
const CHILD = ["나", "딸", "아들", "자식", "본인"];
function talkBubbles(transcript) {
  return transcript.split("\n").map(l => l.trim()).filter(Boolean).map(line => {
    const m = line.match(/^([^:：]{1,6})\s*[:：]\s*(.*)$/);
    const who = m ? m[1].trim() : "", text = m ? m[2] : line;
    const me = CHILD.includes(who);
    return `<div class="bubble ${me ? "me" : ""}"><span class="who">${esc(who || "통화")}</span>${esc(text)}</div>`;
  }).join("");
}

// 데모 번호처럼 보이는 임의 번호 (실제 배정되지 않는 010-0xxx-xxxx)
function demoNumber() {
  const d = String(Math.floor(Math.random() * 1e7)).padStart(7, "0");
  return `010-0${d.slice(0, 3)}-${d.slice(3)}`;
}
