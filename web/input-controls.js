(() => {
  'use strict';
  const defaults = {keys:{forward:'KeyW',back:'KeyS',left:'KeyA',right:'KeyD',up:'ArrowUp',down:'ArrowDown',panLeft:'ArrowLeft',panRight:'ArrowRight',zoomIn:'Equal',zoomOut:'Minus'},pad:{x:0,y:1,up:12,down:13,left:14,right:15,zoomIn:5,zoomOut:4},axisSign:{x:1,y:1},enabled:false};
  const names = {forward:'底盘前进',back:'底盘后退',left:'底盘左转',right:'底盘右转',up:'云台向上',down:'云台向下',panLeft:'云台向左',panRight:'云台向右',zoomIn:'变焦放大',zoomOut:'变焦缩小'};
  const padNames = {x:'底盘左右轴',y:'底盘前后轴',up:'云台向上',down:'云台向下',left:'云台向左',right:'云台向右',zoomIn:'变焦放大',zoomOut:'变焦缩小'};
  const $ = id => document.getElementById(id);
  const keyPattern = /^(Key[A-Z]|Digit[0-9]|Arrow(Up|Down|Left|Right)|Equal|Minus|Numpad(Add|Subtract))$/;
  let config = structuredClone(defaults);
  function valid(value) {
    return value && typeof value.enabled==='boolean' && value.keys && value.pad
      && Object.keys(defaults.keys).every(k=>typeof value.keys[k]==='string' && keyPattern.test(value.keys[k]))
      && new Set(Object.values(value.keys)).size===10
      && Object.keys(defaults.pad).every(k=>Number.isInteger(value.pad[k]) && value.pad[k]>=0 && value.pad[k]<=31)
      && value.pad.x!==value.pad.y
      && new Set(Object.keys(value.pad).filter(k=>!['x','y'].includes(k)).map(k=>value.pad[k])).size===6
      && (!value.axisSign || [value.axisSign.x,value.axisSign.y].every(x=>x===1||x===-1));
  }
  try { const saved=JSON.parse(localStorage.getItem('hkcamera-inputs-v1')); if(valid(saved)) config={...structuredClone(defaults),...saved}; } catch {}
  const held=new Set();
  let mappedBase=false,mappedPtz=false,padArmed=false,padIdentity=null,padStamp=null,padPermissions=null;
  let draft=null,group='keys',capture=null;
  const dialog=$('mapping-dialog');
  function editing() { return dialog.open || $('password-dialog').open || document.activeElement?.matches('input,textarea,select,[contenteditable="true"]'); }
  function available() { return !document.hidden && document.hasFocus() && !editing(); }
  function baseAllowed() { return operator && baseConnected && !modeSwitching && !motionRecovering && ws?.readyState===WebSocket.OPEN; }
  function ptzAllowed() { return operator && cameraOnline && ptzReady && !modeSwitching && ws?.readyState===WebSocket.OPEN; }
  function apply(base,ptz) {
    if(baseAllowed() && (base.x || base.y || mappedBase)) { baseAxes=base; baseButton=null; paintBase(); sendBase(); }
    if(ptzAllowed() && (ptz.x || ptz.y || ptz.z || mappedPtz)) { ptzAxes=ptz;ptzHeld=null;paintPtz();sendPtzAxes(); }
    mappedBase=baseAllowed() && !!(base.x||base.y);mappedPtz=ptzAllowed() && !!(ptz.x||ptz.y||ptz.z);
  }
  window.resetMappedInputs=()=>{held.clear();mappedBase=false;mappedPtz=false;padArmed=false;padStamp=null;padIdentity=null;padPermissions=null;};
  function sourceUI() {
    $('input-keyboard').setAttribute('aria-pressed',String(!config.enabled));
    $('input-gamepad').setAttribute('aria-pressed',String(config.enabled));
  }
  window.refreshInputPermissions=()=>{
    if(!operator){capture=null;draft=null;if(dialog.open)dialog.close();}
  };
  function save(value) {
    if(!operator)return false;
    try{localStorage.setItem('hkcamera-inputs-v1',JSON.stringify(value));return true;}
    catch{$('mapping-feedback').textContent='无法保存设置';return false;}
  }
  for(const [id,enabled] of [['input-keyboard',false],['input-gamepad',true]]){
    $(id).onclick=()=>{
      if(!ws || ws.readyState!==WebSocket.OPEN)return;
      const next={...config,enabled};
      if(operator && !save(next))return;
      stopAll('input_source_changed');config=next;padIdentity=null;sourceUI();
      log(enabled?'输入来源：手柄（获得操控权后发送）':'输入来源：键盘（获得操控权后发送）');
    };
  }
  function keysChanged() {
    const k=config.keys,on=name=>held.has(k[name])?1:0;
    apply({x:on('right')-on('left'),y:on('back')-on('forward')},{x:on('panRight')-on('panLeft'),y:on('down')-on('up'),z:on('zoomIn')-on('zoomOut')});
  }
  function codeFor(e) {
    if(e.code==='NumpadAdd' && config.keys.zoomIn==='Equal')return 'Equal';
    if(e.code==='NumpadSubtract' && config.keys.zoomOut==='Minus')return 'Minus';
    return e.code;
  }
  const friendlyKey=code=>({ArrowUp:'向上',ArrowDown:'向下',ArrowLeft:'向左',ArrowRight:'向右',Equal:'+ / =',Minus:'−',NumpadAdd:'数字键 +',NumpadSubtract:'数字键 −',Space:'空格'}[code]||code.replace(/^Key|^Digit/,''));
  function friendlyPadButton(index){
    const labels=['A','B','X','Y','LB','RB','LT','RT','返回','开始','左摇杆按压','右摇杆按压','十字键上','十字键下','十字键左','十字键右'];
    return getPad()?.mapping==='standard'&&labels[index]?labels[index]:'按键 '+(index+1);
  }
  function bind(value,sign) {
    if(!operator || !capture || !draft)return;
    const {name,group:kind}=capture;
    const old=draft[kind][name];
    for(const other of Object.keys(draft[kind])){
      const sameFamily=kind==='keys'||(['x','y'].includes(name)===['x','y'].includes(other));
      if(other!==name && sameFamily && draft[kind][other]===value) draft[kind][other]=old;
    }
    draft[kind][name]=value;
    if(sign)draft.axisSign[name]=sign;
    capture=null;fields();$('mapping-feedback').textContent='已录入，保存后生效';
  }
  window.addEventListener('keydown',e=>{
    if(capture && operator && dialog.open){
      e.preventDefault();
      if(e.code==='Escape'){capture=null;fields();$('mapping-feedback').textContent='已取消录入';return;}
      if(capture.group==='keys'){
        if(keyPattern.test(e.code))bind(e.code);
        else $('mapping-feedback').textContent='请选择字母、数字、方向键或加减号；空格保留为停止';
      }
      return;
    }
    const code=codeFor(e);
    if(config.enabled||!available()||e.ctrlKey||e.metaKey||e.altKey||!Object.values(config.keys).includes(code))return;
    if(e.repeat&&!held.has(code))return;
    e.preventDefault();if(!held.has(code)){held.add(code);keysChanged();}
  });
  window.addEventListener('keyup',e=>{const code=codeFor(e);if(held.delete(code)){e.preventDefault();keysChanged();}});
  document.addEventListener('focusin',e=>{if(e.target.matches('input,textarea,select,[contenteditable="true"]')&&(mappedBase||mappedPtz))stopAll('editing');});
  let padIssue='';
  function getPad(){
    const state=$('gamepad-state');
    state.dataset.api=typeof navigator.getGamepads;
    state.dataset.mode=config.enabled?'gamepad':'keyboard';
    state.dataset.secure=String(window.isSecureContext);
    if(typeof navigator.getGamepads!=='function'){
      padIssue=window.isSecureContext===false?'手柄需要安全连接 · 请用 HTTPS 打开网页':'浏览器未提供手柄接口 · 请使用支持手柄的浏览器';state.dataset.error='unsupported';state.dataset.devices='0';return null;
    }
    try{
      const pads=[...navigator.getGamepads()].filter(p=>p?.connected);
      state.dataset.devices=String(pads.length);state.dataset.error='';padIssue='';
      const pad=pads[0];
      if(pad){state.dataset.axes=String(pad.axes.length);state.dataset.buttons=String(pad.buttons.length);state.title=pad.id;}
      return pad;
    }catch(error){
      state.dataset.error=error.name;state.dataset.devices='0';
      padIssue=error.name==='SecurityError'?'浏览器禁止读取手柄 · 检查 HTTPS 和访问权限':'手柄读取失败 · 重新连接后按一下手柄键';return null;
    }
  }
  function fields(){
    if(!draft)return;
    $('mapping-keys').setAttribute('aria-selected',String(group==='keys'));
    $('mapping-pad').setAttribute('aria-selected',String(group==='pad'));
    $('mapping-fields').replaceChildren();
    for(const [name,label] of Object.entries(group==='keys'?names:padNames)){
      const row=document.createElement('div');row.className='mapping-row';
      const text=document.createElement('span');text.textContent=label;
      const button=document.createElement('button');button.type='button';button.dataset.group=group;button.dataset.name=name;
      button.setAttribute('aria-label',label+'映射');
      const listening=capture?.name===name && capture?.group===group;
      button.dataset.listening=String(listening);
      button.textContent=listening?'等待输入…':group==='keys'?friendlyKey(draft.keys[name]):['x','y'].includes(name)?'摇杆轴 '+(draft.pad[name]+1):friendlyPadButton(draft.pad[name]);
      button.onclick=()=>{
        if(!operator)return;
        const pad=getPad();
        capture={name,group,at:performance.now(),axes:pad?[...pad.axes]:[],buttons:pad?pad.buttons.map(b=>b.pressed):[]};
        fields();
        $('mapping-feedback').textContent=group==='keys'?'按下要绑定的键盘按键（Esc 取消）':name==='x'?'向右拨动要绑定的摇杆（Esc 取消）':name==='y'?'向前拨动要绑定的摇杆（Esc 取消）':'按下要绑定的手柄按钮（Esc 取消）';
      };
      row.append(text,button);$('mapping-fields').append(row);
    }
  }
  $('input-settings').onclick=()=>{
    if(!operator)return;
    stopAll('input_settings');draft=structuredClone(config);capture=null;group=config.enabled?'pad':'keys';
    fields();$('mapping-feedback').textContent='';dialog.showModal();
  };
  $('mapping-close').onclick=()=>dialog.close();
  dialog.addEventListener('close',()=>{capture=null;draft=null;});
  for(const name of ['keys','pad']) $('mapping-'+name).onclick=()=>{if(!operator)return;group=name;capture=null;fields();$('mapping-feedback').textContent='';};
  $('mapping-reset').onclick=()=>{if(!operator||!draft)return;draft[group]=structuredClone(defaults[group]);if(group==='pad')draft.axisSign={x:1,y:1};capture=null;fields();};
  $('mapping-form').onsubmit=e=>{
    e.preventDefault();if(!operator||!draft)return;
    if(capture){$('mapping-feedback').textContent='请先完成或取消按键录入';return;}
    if(!valid(draft)){ $('mapping-feedback').textContent='映射无效，请重新录入';return; }
    if(!save(draft))return;
    stopAll('mapping_changed');config=structuredClone(draft);padIdentity=null;sourceUI();dialog.close();log('按键映射已保存');
  };
  setInterval(()=>{
    const pad=getPad();
    if(capture && operator && dialog.open){
      if(performance.now()-capture.at>10000){capture=null;fields();$('mapping-feedback').textContent='录入超时，请重试';return;}
      if(capture.group==='pad'&&pad){
        const name=capture.name;
        if(['x','y'].includes(name)){
          const axis=pad.axes.findIndex((value,i)=>Math.abs(value-(capture.axes[i]||0))>.6);
          if(axis>=0){const sign=Math.sign(pad.axes[axis]-(capture.axes[axis]||0));bind(axis,name==='y'?-sign:sign);}
        }else{
          const button=pad.buttons.findIndex((b,i)=>b.pressed&&!capture.buttons[i]);
          if(button>=0)bind(button);
          else capture.buttons=pad.buttons.map(b=>b.pressed);
        }
      }
    }
    if(!pad){
      $('gamepad-state').textContent=padIssue||(config.enabled?'本浏览器未检测到手柄 · 请按一下手柄键':'未检测到手柄');
      $('gamepad-state').title='手柄必须连接到当前浏览器所在电脑。Windows 上的手柄由 Windows 浏览器读取，不会自动转发给 NX 浏览器。';
      if(padIdentity!==null){if(mappedBase||mappedPtz)stopAll('gamepad_disconnected');padIdentity=null;padArmed=false;}
      return;
    }
    $('gamepad-state').title=pad.id;
    if(!config.enabled){$('gamepad-state').textContent='手柄已连接 · 未选用';return;}
    const identity=pad.index+':'+pad.id;
    if(identity!==padIdentity){padIdentity=identity;padArmed=false;padStamp=null;}
    if(!available()){padArmed=false;$('gamepad-state').textContent=dialog.open?'手柄已连接':'手柄已连接 · 页面未激活';return;}
    const permissions=JSON.stringify([baseAllowed(),ptzAllowed()]);
    if(permissions!==padPermissions){padPermissions=permissions;padArmed=false;padStamp=null;}
    const p=config.pad;
    const validBase=p.x<pad.axes.length&&p.y<pad.axes.length;
    const validPtz=Object.keys(p).filter(k=>!['x','y'].includes(k)).every(k=>p[k]<pad.buttons.length);
    const axis=i=>Number.isFinite(pad.axes[i])?deadzone(Math.max(-1,Math.min(1,pad.axes[i]))):0;
    const button=k=>pad.buttons[p[k]]?.pressed?1:0;
    const base=validBase?{x:axis(p.x)*config.axisSign.x,y:axis(p.y)*config.axisSign.y}:{x:0,y:0};
    const ptz={x:button('right')-button('left'),y:button('down')-button('up'),z:button('zoomIn')-button('zoomOut')};
    const neutral=!base.x&&!base.y&&!ptz.x&&!ptz.y&&!ptz.z;
    if(!padArmed){if(neutral)padArmed=true;else{$('gamepad-state').textContent='松开按键并将摇杆回中';return;}}
    $('gamepad-state').textContent=!operator?'手柄已连接 · 等待操控权':!validBase?'底盘摇杆需重新映射':!validPtz?'手柄已就绪 · 部分按键待映射':'手柄已就绪';
    const stamp=JSON.stringify([base,ptz,baseAllowed(),ptzAllowed()]);
    if(stamp!==padStamp){padStamp=stamp;apply(base,ptz);}
  },40);
  sourceUI();

  let widths={left:250,right:280};
  try{const saved=JSON.parse(localStorage.getItem('hkcamera-widths-v1'));if(saved&&Number.isFinite(saved.left)&&Number.isFinite(saved.right))widths=saved;}catch{}
  function resize(side,value){
    if(innerWidth<=1100)return;
    const mins={left:240,right:264},maxs={left:Math.min(340,innerWidth*.26),right:Math.min(340,innerWidth*.26)};
    const other=side==='left'?'right':'left';
    widths[other]=Math.max(mins[other],Math.min(widths[other],maxs[other]));
    const maximum=Math.min(maxs[side],innerWidth-520-widths[other]);
    widths[side]=Math.max(mins[side],Math.min(value,maximum));
    for(const name of ['left','right']){
      document.body.style.setProperty('--'+name+'-width',widths[name]+'px');
      const handle=$('resize-'+name);handle.setAttribute('aria-valuenow',String(Math.round(widths[name])));
      handle.setAttribute('aria-valuemin',String(mins[name]));handle.setAttribute('aria-valuemax',String(Math.floor(Math.min(maxs[name],innerWidth-520-widths[name==='left'?'right':'left']))));
    }
  }
  resize('left',widths.left);resize('right',widths.right);
  const saveWidths=()=>{try{localStorage.setItem('hkcamera-widths-v1',JSON.stringify(widths));}catch{}};
  for(const side of ['left','right']){
    const handle=$('resize-'+side);
    handle.title='调整栏宽；双击恢复默认';
    handle.addEventListener('pointerdown',e=>{if(e.button!==0)return;stopAll('resize');e.preventDefault();handle.setPointerCapture(e.pointerId);});
    handle.addEventListener('pointermove',e=>{if(handle.hasPointerCapture(e.pointerId))resize(side,side==='left'?e.clientX:innerWidth-e.clientX);});
    handle.addEventListener('pointerup',e=>{if(handle.hasPointerCapture(e.pointerId))handle.releasePointerCapture(e.pointerId);saveWidths();});
    handle.addEventListener('dblclick',()=>{widths={left:250,right:280};resize('left',250);resize('right',280);saveWidths();});
    handle.addEventListener('keydown',e=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.code))return;e.preventDefault();
      resize(side,e.code==='Home'?0:e.code==='End'?innerWidth:widths[side]+(e.code==='ArrowRight'?1:-1)*(side==='left'?1:-1)*10);saveWidths();
    });
  }
  window.addEventListener('resize',()=>{resize('left',widths.left);resize('right',widths.right);});
})();
