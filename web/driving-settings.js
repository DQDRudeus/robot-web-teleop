(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const editing = () => document.querySelector('dialog[open]') || document.activeElement?.matches('input,textarea,select,[contenteditable="true"]');
  let quality = null, pendingSpeed = false, speedTimer, speedNotice = '';
  function advanced() { return operator && operatorRole === 'advanced' && ws?.readyState === WebSocket.OPEN; }
  function setSpeed(percent) {
    if (!advanced() || pendingSpeed) return;
    pendingSpeed = send({type:'speed', scale:percent/100});
    speedNotice = pendingSpeed ? `正在申请 ${percent}% 档位…` : '连接中断，未发送调速请求';
    clearTimeout(speedTimer);
    speedTimer = setTimeout(() => {if(pendingSpeed){pendingSpeed=false;speedNotice='调速未获后端确认，请检查 NX 版本与控制连接';stopAll('speed_ack_timeout');refresh();}}, 4000);
    refresh();
  }
  window.confirmDriveSpeed = message => {
    clearTimeout(speedTimer); pendingSpeed=false;
    speedNotice = message.ok ? `后端已确认 ${Math.round(message.scale*100)}% 档位` : '后端拒绝调速，当前档位未改变';
    if (!message.ok) stopAll('speed_denied');
    refresh();
  };
  window.resetDriveRequest = () => { clearTimeout(speedTimer); pendingSpeed=false; speedNotice=''; };
  function refresh() {
    const percent = Math.round(speedScale*100);
    if (document.activeElement !== $('drive-speed')) $('drive-speed').value = percent;
    $('drive-speed').disabled = !advanced() || pendingSpeed;
    $('drive-speed-value').textContent = `${percent<=25?'低速':percent<=50?'中速':'高速'} · ${percent}%${motionTarget==='am3008'?` · 上限 ${(speedCeiling*speedScale).toFixed(2)} m/s`:''}`;
    $('drive-permission').textContent = operator ? operatorRole==='advanced'?'高级控制 · 独占 · 可调速':'test 低速控制 · 高级用户可接管' : 'test：低速试用；高级密码：调速与独占控制';
    if (speedNotice) $('drive-permission').textContent += ' · ' + speedNotice;
    $('video-apply').disabled = !advanced() || quality?.busy === true;
    $('video-refresh').disabled = !operator || quality?.busy === true;
    for (const id of ['video-profile','video-resolution','video-fps','video-kbps','video-auto']) $(id).disabled = !advanced();
    const custom = $('video-profile').value === 'custom';
    for (const id of ['video-resolution','video-fps','video-kbps']) $(id).disabled ||= !custom;
  }
  window.refreshDrivingSettings = refresh;
  window.syncVideoQuality = state => {
    if (!state) return;
    const previous = quality;
    quality = state;
    const stamp = JSON.stringify(state.resolutions);
    if ($('video-resolution').dataset.options !== stamp) {
      const prior = $('video-resolution').value;
      $('video-resolution').replaceChildren(...(state.resolutions||[]).map(size => new Option(`${size.width} × ${size.height}`,`${size.width}x${size.height}`)));
      $('video-resolution').dataset.options = stamp;
      if (prior) $('video-resolution').value = prior;
    }
    if (!previous?.current && state.current) {
      $('video-profile').value = state.profile;
      $('video-auto').checked = state.auto;
      $('video-resolution').value = `${state.current.width}x${state.current.height}`;
      $('video-fps').value = state.current.fps || 15;
      $('video-kbps').value = state.current.kbps;
    }
    const current = state.current;
    $('video-quality-state').textContent = state.busy ? '正在通过 HCNetSDK 调整编码并回读…' : state.error || (current ? `SDK 回读：${current.width}×${current.height} · ${current.fps?current.fps+' fps':'设备全帧率'} · ${current.kbps} kbps${state.auto?' · 自适应开启':''}` : '摄像头 SDK 参数尚未读取；支持情况以设备为准');
    refresh();
  };
  $('drive-speed').addEventListener('change', e => setSpeed(Number(e.target.value)));
  $('drive-speed').addEventListener('input', e => {$('drive-speed-value').textContent=`选择 ${e.target.value}% · 松手应用`;});
  $('video-profile').addEventListener('change', refresh);
  $('video-refresh').onclick = () => send({type:'video_refresh'});
  $('video-apply').onclick = () => {
    const [width,height] = $('video-resolution').value.split('x').map(Number);
    send({type:'video_settings',profile:$('video-profile').value,auto:$('video-auto').checked,
          width,height,fps:Number($('video-fps').value),kbps:Number($('video-kbps').value)});
    $('video-quality-state').textContent='已提交，等待摄像头确认';
  };
  // Capture Space before focused direction buttons can turn it into motion.
  // Text fields/dialogs retain normal editing; their focus already stops motion.
  window.addEventListener('keydown', event => {
    if (event.code === 'Space') {
      if (!event.repeat) stopAll('space');
      // Preserve typing in forms while still issuing STOP. Range sliders and
      // focused movement buttons must not consume Space as a new movement.
      if (editing() && document.activeElement?.type !== 'range') return;
      event.preventDefault(); event.stopImmediatePropagation();
    } else if ((!editing() || document.activeElement?.type === 'range') && !event.ctrlKey && !event.altKey && !event.metaKey && ['ShiftLeft','ShiftRight'].includes(event.code) && !event.repeat) {
      event.preventDefault();
      if (!advanced()) {speedNotice='切档需要高级密码和当前操控权';refresh();return;}
      const percent = speedScale < .5 ? 50 : speedScale < 1 ? 100 : 25;
      setSpeed(percent);
    }
  }, true);
  window.addEventListener('keyup', event => {
    if (event.code === 'Space' && !editing()) {event.preventDefault();event.stopImmediatePropagation();}
  }, true);

  let prior = null, sampling = false, lastFrameAt = performance.now(), lastDecoded = null;
  setInterval(async () => {
    if (sampling || document.hidden || videoEl.paused || motionTarget === 'turtlesim') {prior=null;return;}
    const pc = videoPlayer?.pc;
    if (!pc?.getStats || pc.connectionState !== 'connected') {prior=null;return;}
    sampling = true;
    try {
      const stats = await pc.getStats();
      let inbound = null, rtt = null;
      stats.forEach(stat => {
        if (stat.type === 'inbound-rtp' && (stat.kind === 'video' || stat.mediaType === 'video') && !stat.isRemote) inbound=stat;
        if (stat.type === 'candidate-pair' && stat.state === 'succeeded' && (stat.nominated || stat.selected) && Number.isFinite(stat.currentRoundTripTime)) rtt=stat.currentRoundTripTime;
      });
      if (!inbound || !Number.isFinite(rtt)) {prior=null;return;}
      if (!prior || prior.id!==inbound.id || inbound.timestamp<=prior.timestamp) {prior=inbound;lastFrameAt=performance.now();lastDecoded=inbound.framesDecoded;return;}
      const packets = inbound.packetsReceived-prior.packetsReceived;
      const lost = Math.max(0,inbound.packetsLost-prior.packetsLost);
      if (packets < 0) {prior=inbound;return;}
      const loss = packets+lost ? lost/(packets+lost) : 0;
      const kbps = Math.max(0,8*(inbound.bytesReceived-prior.bytesReceived)/(inbound.timestamp-prior.timestamp));
      if (inbound.framesDecoded!==lastDecoded) {lastFrameAt=performance.now();lastDecoded=inbound.framesDecoded;}
      const stall = Number.isFinite(lastDecoded) && performance.now()-lastFrameAt>3500;
      $('video-network-state').textContent=`接收 ${Math.round(kbps)} kbps · 丢包 ${(loss*100).toFixed(1)}% · RTT ${Math.round(rtt*1000)} ms${stall?' · 画面停滞':''}`;
      if (operator) send({type:'video_metrics',active:true,loss,rtt,stall});
      // Source resolution changes may require a fresh SDP/video track.
      if (stall && performance.now()-lastFrameAt>8000) retryVideo();
      prior=inbound;
    } catch {prior=null;} finally {sampling=false;}
  },2000);
  refresh();
})();
