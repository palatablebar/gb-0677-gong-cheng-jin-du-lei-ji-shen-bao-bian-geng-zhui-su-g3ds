const notice = document.getElementById('notice');

function call(path, options) {
  notice.textContent = '处理中…';
  return fetch(path, Object.assign({
    headers: { 'Content-Type': 'application/json' }
  }, options)).then(async (response) => {
    const body = await response.json();
    if (!response.ok) throw new Error(body.message || ('HTTP ' + response.status));
    return body;
  });
}

document.getElementById('print').addEventListener('click', () => window.print());

document.getElementById('load-sample').addEventListener('click', () => {
  call('/api/materials/sample/load', { method: 'POST' })
    .then(() => { notice.textContent = '样例已幂等入账'; window.location.reload(); })
    .catch((error) => { notice.textContent = error.message; });
});

document.getElementById('submit-event').addEventListener('click', () => {
  const payload = {
    events: [{
      event_id: document.getElementById('event-id').value.trim(),
      material: document.getElementById('event-material').value.trim(),
      event_type: document.getElementById('event-type').value,
      period: document.getElementById('event-period').value.trim(),
      quantity: document.getElementById('event-qty').value.trim(),
      unit_price: document.getElementById('event-price').value.trim(),
      ref_id: document.getElementById('event-ref').value.trim(),
      occurred_at: document.getElementById('event-time').value.trim()
    }]
  };
  call('/api/materials/events', { method: 'POST', body: JSON.stringify(payload) })
    .then((data) => {
      const skipped = data.ingest.duplicate_skipped;
      notice.textContent = skipped > 0
        ? '事件已存在，幂等跳过，未重复抵扣'
        : '事件已入账，台账已滚动重算';
      window.location.reload();
    })
    .catch((error) => { notice.textContent = error.message; });
});

document.querySelectorAll('.publish').forEach((button) => {
  button.addEventListener('click', () => {
    call('/api/materials/certificates', {
      method: 'POST',
      body: JSON.stringify({ period: button.dataset.period, base_version: null })
    }).then((cert) => {
      notice.textContent = cert.created === false
        ? '签证内容未变，沿用 V' + cert.version + '（未产生新版本）'
        : '签证已发布：' + cert.cert_id + ' V' + cert.version + '，旧版已保留';
      window.location.reload();
    }).catch((error) => { notice.textContent = '发布失败，旧版保留：' + error.message; });
  });
});

document.querySelectorAll('.sign').forEach((button) => {
  button.addEventListener('click', () => {
    call('/api/materials/certificates/' + button.dataset.cert + '/sign', {
      method: 'POST',
      body: JSON.stringify({ version: Number(button.dataset.version), decision: '已签认' })
    }).then(() => { notice.textContent = '已签认，该周期随即关闭'; window.location.reload(); })
      .catch((error) => { notice.textContent = error.message; });
  });
});
