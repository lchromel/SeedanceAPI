LOGIN_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход · Видеостудия</title><style>
*{box-sizing:border-box}body{margin:0;background:#131414;color:#e7e9ed;font:16px system-ui,sans-serif;min-height:100vh;display:grid;place-items:center}
main{background:#1f2022;border-radius:24px;width:min(440px,calc(100% - 32px));padding:32px}h1{font-size:28px;margin:0 0 8px}p{color:#8f949d;line-height:1.5}label{display:block;margin-top:24px;font-size:14px;color:#bbc7d8}
input,button{font:inherit;width:100%;border-radius:14px;border:1px solid transparent;padding:14px 16px;margin-top:8px}input{background:#191a1c;color:#e7e9ed}input:focus{outline:2px solid #bdd0e7}button{background:#bdd0e7;color:#171b20;margin-top:28px;cursor:pointer}button:disabled{opacity:.5}#error{color:#d6c094;min-height:24px;font-size:14px}
</style><script src="/login.js" defer></script></head><body><main><h1>С возвращением</h1><p>Войдите в свою видеостудию</p>
<form id="login"><label>Логин<input name="username" autocomplete="username" maxlength="254" required autofocus></label>
<label>Пароль<input name="password" type="password" autocomplete="current-password" maxlength="1024" required></label>
<button type="submit">Войти</button><p id="error" role="alert" aria-live="polite"></p></form></main></body></html>"""

LOGIN_JS = """const form=document.querySelector('#login');
form.addEventListener('submit',async event=>{
  event.preventDefault();const button=form.querySelector('button');const error=document.querySelector('#error');
  button.disabled=true;error.textContent='';
  try{const response=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:form.elements.username.value,password:form.elements.password.value})});
    const result=await response.json();form.elements.password.value='';
    if(!response.ok)throw new Error(result.error||'Не удалось войти');location.replace('/');
  }catch(e){error.textContent=e.message;}finally{button.disabled=false;}
});"""
