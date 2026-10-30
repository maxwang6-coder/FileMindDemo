(() => {
  let sessionToken = null;
  const form = document.querySelector('#session-form');
  const input = document.querySelector('#token');
  const status = document.querySelector('#status');
  const disconnect = document.querySelector('#disconnect');

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    sessionToken = input.value.trim();
    input.value = '';
    try {
      const response = await fetch('/api/status', {
        headers: { Authorization: `Bearer ${sessionToken}` },
        credentials: 'omit', cache: 'no-store', redirect: 'error',
      });
      if (!response.ok) throw new Error('authentication failed');
      status.textContent = '已连接，可以使用本机会话。';
      disconnect.hidden = false;
      form.hidden = true;
    } catch {
      sessionToken = null;
      status.textContent = '连接失败，请确认服务已启动并重新输入终端中的 token。';
    }
  });

  disconnect.addEventListener('click', () => {
    sessionToken = null;
    input.value = '';
    disconnect.hidden = true;
    form.hidden = false;
    status.textContent = '已断开连接';
  });
  window.addEventListener('pagehide', () => { sessionToken = null; input.value = ''; });
})();
